"""/api/v2 的整理与存储：队列数量、"现在就整理"、打开数据文件夹、保留期设置、马上写概括（片段 / 当天）、
删除后当天概括标为待更新。合成数据，大模型用替身。"""
from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from screenrecall.app.llm import LlmError
from screenrecall.store.writer import LineIn
from screenrecall.v2 import config as v2config
from screenrecall.v2.summarize_now import SummaryRunner
from screenrecall.v2.work_llm import SYSTEM_PIECE
from tests import test_v2_web_fixtures as F
from tests._v2_worker_fakes import FakeLlm

LLM_ENV = {"LLM_BASE_URL": "https://llm.example.test/v1", "LLM_API_KEY": "k" * 20, "LLM_MODEL": "demo-model"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = F.build_env(tmp_path, monkeypatch)
    yield e
    if e.ctx.summaries is not None:
        e.ctx.summaries.close()
    e.stack.close()


@pytest.fixture
def client(env):
    with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
        F.login(c, env)
        yield c


def with_llm(env, llm) -> SummaryRunner:
    env.ctx.llm_environ = dict(LLM_ENV)
    r = SummaryRunner(env.stack.cfg, env.stack.writer, lambda stop: llm, clock=env.stack.clock)
    env.ctx.summaries = r
    return r


# ---------------- 状态里的队列 ----------------

def test_status_queue_counts(client, env):
    q = client.get("/api/v2/status").json()["queue"]
    # 合成数据：1 个待识别区域；4 行还没算向量；唯一的片段已有概括
    assert q == {"ocr": 1, "vectors": 4, "summaries": 0}
    w = env.stack.writer
    t = F.day_base() - 3 * 3600_000
    w.piece_put(t, t + 60_000, app="other.exe", title="合成文档", segment_ids=[]).result(5)
    w.piece_put(t, t + 60_000, app="bank.exe", title="合成账户", segment_ids=[]).result(5)
    client.post("/api/v2/settings", json={"hidden_from_agents": {"apps": ["bank.exe"]}}, headers=F.POST_H)
    q = client.get("/api/v2/status").json()["queue"]
    assert q["summaries"] == 1  # 对 Agent 隐藏的不发给大模型，不算
    w.call(lambda c: c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('worker.text_line_id', '3')")).result(5)
    assert client.get("/api/v2/status").json()["queue"]["vectors"] == 1


# ---------------- 现在就整理、打开文件夹 ----------------

def test_idle_run_now(client, env):
    assert client.post("/api/v2/idle/run", json={}, headers=F.POST_H).status_code == 503  # 没接主进程
    started = []
    running = {"v": False}

    def run():
        started.append(1)
        running["v"] = True
        return True

    env.ctx.run_idle = run
    env.ctx.idle_running = lambda: running["v"]
    assert client.post("/api/v2/idle/run", json={}).status_code == 403  # 要 X-ScreenRecall 头
    s = client.post("/api/v2/idle/run", json={}, headers=F.POST_H).json()
    assert started == [1] and s["idle_work"] == "running" and "queue" in s
    env.ctx.run_idle = lambda: False
    assert client.post("/api/v2/idle/run", json={}, headers=F.POST_H).status_code == 503


def test_open_data_folder(client, env):
    opened = []
    env.ctx.open_folder = opened.append
    assert client.post("/api/v2/storage/open", json={}).status_code == 403
    assert client.post("/api/v2/storage/open", json={"path": "C:\\"}, headers=F.POST_H).json() == {"ok": True}
    assert opened == [env.data]  # 只开数据文件夹，不认请求里的路径
    with TestClient(env.guard, base_url=F.BASE) as anon:
        assert anon.post("/api/v2/storage/open", json={}, headers=F.POST_H).status_code == 401
    assert opened == [env.data]


# ---------------- 保留期 ----------------

def test_retention_settings(client, env):
    s = client.get("/api/v2/settings").json()
    assert (s["image_days"], s["text_days"], s["storage_cap_gb"]) == (30, 90, 0.0)
    s = client.post("/api/v2/settings", json={"image_days": 90, "text_days": 0, "storage_cap_gb": 20},
                    headers=F.POST_H).json()
    assert (s["image_days"], s["text_days"], s["storage_cap_gb"]) == (90, 0, 20.0)
    cfg = v2config.load_v2(env.data, home=env.home)  # 空闲整理进程这样读到
    assert (cfg.retention.image_days, cfg.retention.text_days, cfg.retention.storage_cap_gb) == (90, 0, 20.0)
    for bad in [{"image_days": -1}, {"text_days": 1.5}, {"text_days": "90"}, {"image_days": True},
                {"storage_cap_gb": -2}, {"text_days": 99999}]:
        r = client.post("/api/v2/settings", json=bad, headers=F.POST_H)
        assert r.status_code == 400 and r.json()["error"], bad
    assert client.get("/api/v2/settings").json()["image_days"] == 90


def test_autostart_command_carries_a_custom_home(client, env):
    client.post("/api/v2/settings", json={"autostart": True}, headers=F.POST_H)
    cmd = env.ctx.autostart.commands[-1]
    # 测试用的应用目录不是默认位置：注册表里的命令没有环境变量，要带上 --home
    assert cmd is not None and "--autostart" in cmd and "--home" in cmd and str(env.home) in cmd
    assert "--data-dir" in cmd and str(env.data) in cmd  # 这里数据目录也不在 <应用目录>\data
    from screenrecall.app import autostart

    assert autostart.location_args(env.home, env.home / "data") == ("--home", str(env.home))


# ---------------- 马上写概括 ----------------

def test_regenerate_writes_the_piece_summary_now(client, env):
    llm = FakeLlm(delay=0.3)
    runner = with_llm(env, llm)
    pid = env.ids["piece"]
    r = client.post(f"/api/v2/pieces/{pid}/summarize", json={}, headers=F.POST_H).json()
    assert r == {"queued": True, "summary_state": "generating"}
    assert client.get(f"/api/v2/pieces/{pid}").json()["summary_state"] == "generating"
    rev0 = client.get("/api/v2/status").json()["revision"]
    assert runner.wait_idle(10)
    p = client.get(f"/api/v2/pieces/{pid}").json()
    assert p["summary"]["text"] == "在写周报" and p["summary_state"] == "ok" and p["piece"]["task"] == "写周报"
    assert client.get("/api/v2/status").json()["revision"] > rev0  # 页面据此刷新
    # 和空闲整理同样的提示词：屏幕上的字整体编码成 JSON，当数据不当指令
    msgs = llm.calls[0]
    assert msgs[0]["content"] == SYSTEM_PIECE and "Quarterly REPORT draft" in msgs[1]["content"]
    job = env.stack.queries.conn().execute("SELECT status FROM jobs WHERE kind = 'summary_piece'").fetchone()
    assert job[0] == "done"


def test_only_one_summary_runs_at_a_time(client, env):
    active, peak = [0], [0]
    lock = threading.Lock()

    class Counting(FakeLlm):
        def chat(self, messages, **kw):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(0.15)
                return super().chat(messages, **kw)
            finally:
                with lock:
                    active[0] -= 1

    runner = with_llm(env, Counting())
    w = env.stack.writer
    t = F.day_base()
    sid = env.ids["segment"]
    other = w.piece_put(t + 20 * 60_000, t + 25 * 60_000, app=F.APP, title=F.TITLE, segment_ids=[sid]).result(5)
    for pid in (env.ids["piece"], other, env.ids["piece"]):
        client.post(f"/api/v2/pieces/{pid}/summarize", json={}, headers=F.POST_H)
    assert runner.wait_idle(10) and peak[0] == 1


def test_failed_regenerate_says_failed(client, env):
    runner = with_llm(env, FakeLlm(fail=LlmError("boom")))
    pid = env.ids["piece"]
    client.post(f"/api/v2/pieces/{pid}/summarize", json={}, headers=F.POST_H)
    assert runner.wait_idle(10)
    assert client.get(f"/api/v2/pieces/{pid}").json()["summary_state"] == "failed"


def test_without_llm_regenerate_only_queues(client, env):
    env.ctx.summaries = SummaryRunner(env.stack.cfg, env.stack.writer, lambda s: pytest.fail("不该调用"),
                                      clock=env.stack.clock)
    r = client.post(f"/api/v2/pieces/{env.ids['piece']}/summarize", json={}, headers=F.POST_H).json()
    assert r["summary_state"] == "queued"
    assert client.post("/api/v2/day/summarize", json={}, headers=F.POST_H).status_code == 400


def test_day_refresh_and_stale_after_delete(client, env):
    runner = with_llm(env, FakeLlm())
    d = client.get("/api/v2/day").json()
    assert d["summary_state"] == "none" and d["summary_refresh"] is True
    assert client.post("/api/v2/day/summarize", json={"date": "2999-01-01"}, headers=F.POST_H).status_code == 400
    r = client.post("/api/v2/day/summarize", json={}, headers=F.POST_H).json()
    assert r["summary_state"] == "generating"
    assert runner.wait_idle(10)
    d = client.get("/api/v2/day").json()
    assert d["summary"]["text"] == "今天主要在写周报。" and d["summary_state"] == "ok"

    # 删掉片段里的一段：片段概括和当天概括都清空、标为待更新
    base = F.day_base()
    client.post("/api/v2/delete", json={"from_ms": base + 4_000, "to_ms": base + 6_000}, headers=F.POST_H)
    d = client.get("/api/v2/day").json()
    assert d["summary_state"] == "stale" and d["summary"]["text"] == ""
    # 刷新：先重写被删过的片段概括，再写当天概括
    client.post("/api/v2/day/summarize", json={}, headers=F.POST_H)
    assert runner.wait_idle(10)
    d = client.get("/api/v2/day").json()
    assert d["summary_state"] == "ok" and d["summary"]["text"]
    assert [x for x in d["items"] if x["kind"] == "piece"][0]["summary_line"] == "在写周报"


def test_day_refresh_with_nothing_left_drops_the_empty_summary(client, env):
    runner = with_llm(env, FakeLlm())
    client.post("/api/v2/day/summarize", json={}, headers=F.POST_H)
    assert runner.wait_idle(10)
    # 整个片段删掉：当天没有能写的了
    p = env.ids["piece_start"]
    client.post("/api/v2/delete", json={"from_ms": p - 1, "to_ms": p + 11 * 60_000}, headers=F.POST_H)
    assert client.get("/api/v2/day").json()["summary_state"] == "stale"
    client.post("/api/v2/day/summarize", json={}, headers=F.POST_H)
    assert runner.wait_idle(10)
    d = client.get("/api/v2/day").json()
    assert d["summary"] is None and d["summary_state"] == "none"


def test_private_piece_is_not_sent_by_the_day_refresh(client, env):
    llm = FakeLlm()
    runner = with_llm(env, llm)
    w = env.stack.writer
    t = F.day_base() + 30 * 60_000
    sid = w.segment_for("bank.exe", "合成账户", t).result(5)
    b = w.block_new(sid, t).result(5)
    w.record_lines(b, t, (0, 100), [LineIn("PRIVATE-LINE", 0, 10, 100, 30)]).result(5)
    w.piece_put(t, t + 60_000, app="bank.exe", title="合成账户", segment_ids=[sid]).result(5)
    client.post("/api/v2/settings", json={"hidden_from_agents": {"apps": ["bank.exe"]}}, headers=F.POST_H)
    client.post("/api/v2/day/summarize", json={}, headers=F.POST_H)
    assert runner.wait_idle(10)
    assert llm.calls and all("PRIVATE-LINE" not in m[1]["content"] and "bank.exe" not in m[1]["content"]
                             for m in llm.calls)
