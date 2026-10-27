"""MCP 适配层（StoreQueriesAdapter + agent_policy）走 app.mcp_server 的真实工具；托盘 StoreController；
第一次启动写默认规则；页面设置文件；AuthGuard 换令牌。"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

import anyio
import numpy as np
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ImageContent, TextContent
from PIL import Image

from screenrecall.app import auth
from screenrecall.app import mcp_server as ms
from screenrecall.store.writer import LineIn
from screenrecall.timeutil import utc_ms
from screenrecall.v2 import adapters
from screenrecall.v2 import config as v2config
from tests import test_v2_web_fixtures as F


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = F.build_env(tmp_path, monkeypatch)
    yield e
    e.stack.close()


def make_mcp(env, **kw):
    q = adapters.StoreQueriesAdapter(env.stack.queries, env.stack.search, **kw)
    return q, ms.build_mcp(q, adapters.agent_policy(env.stack.control), port=18612)


def call(mcp, name, args):
    async def go():
        return await mcp.call_tool(name, args)
    return anyio.run(go)


def text_of(blocks) -> str:
    return "\n".join(b.text for b in blocks if isinstance(b, TextContent))


def enable(env, **fields):
    env.stack.set_control(agent_enabled=True, **fields)


def test_agent_disabled_by_default(env):
    _, mcp = make_mcp(env)
    with pytest.raises(ToolError, match="已关闭 Agent 访问"):
        call(mcp, "search", {"query": "report"})


def test_search_timeline_summary_frame(env):
    enable(env)
    _, mcp = make_mcp(env)
    out = text_of(call(mcp, "search", {"query": "report", "time_from": "today"}))
    assert "    > Quarterly REPORT draft" in out and f"frame={env.ids['frame']}" in out
    assert f"piece={env.ids['piece']}" in out and "task=写报告" in out and ms.UNTRUSTED_NOTE in out
    assert "1 条" in out

    tl = text_of(call(mcp, "timeline", {"time_from": "today"}))
    assert f"piece={env.ids['piece']}" in tl and "    > 整理季度报告" in tl and "任务 写报告" in tl

    s = text_of(call(mcp, "summary", {"level": "piece", "key": str(env.ids["piece"])}))
    assert "> 整理季度报告" in s
    assert "没有可提供的概括" in text_of(call(mcp, "summary", {"level": "piece", "key": "not-a-number"}))

    blocks = call(mcp, "get_frame", {"frame_id": env.ids["frame"], "highlight_line_id": env.ids["lines"][0]})
    img = next(b for b in blocks if isinstance(b, ImageContent))
    import base64

    png = base64.b64decode(img.data)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and Image.open(io.BytesIO(png)).size == (640, 400)
    t = text_of(blocks)
    assert f"line={env.ids['lines'][0]} (20,30,580,14) ← 高亮" in t


def test_hits_are_newest_first_and_limited(env):
    enable(env)
    w = env.stack.writer
    t = F.day_base() + 3_600_000
    sid = w.segment_for("tk.exe", "合成文档", t).result(5)
    b = w.block_new(sid, t).result(5)
    w.record_lines(b, t, (0, 100), [LineIn("later report line", 0, 10, 300, 24)]).result(5)
    q, _ = make_mcp(env)
    hits = q.search("report", start_ms=None, end_ms=None, app=None, task=None, limit=5, exclude=ms.Exclusion())
    assert [h.text for h in hits] == ["later report line", "Quarterly REPORT draft"]
    assert hits[0].piece_id is None and hits[1].piece_id == str(env.ids["piece"]) and hits[1].task == "写报告"
    assert len(q.search("report", start_ms=None, end_ms=None, app=None, task=None, limit=1,
                        exclude=ms.Exclusion())) == 1


def test_hidden_from_agents_applies_to_every_tool(env):
    enable(env, hidden_from_agents={"apps": ["NOTEPAD.EXE"], "title_keywords": []})
    q, mcp = make_mcp(env)
    assert "没有找到" in text_of(call(mcp, "search", {"query": "report"}))
    assert "这段时间没有记录" in text_of(call(mcp, "timeline", {"time_from": "today"}))
    assert "没有可提供的概括" in text_of(call(mcp, "summary", {"level": "piece", "key": str(env.ids["piece"])}))
    with pytest.raises(ToolError, match="找不到这幅画面"):
        call(mcp, "get_frame", {"frame_id": env.ids["frame"]})
    # 标题关键词按 NFKC + casefold 比较：全角大写也能拦住
    env.stack.set_control(hidden_from_agents={"apps": [], "title_keywords": ["ＲＥＰＯＲＴ.TXT"]})
    assert "没有找到" in text_of(call(mcp, "search", {"query": "report"}))
    assert q.get_frame(env.ids["frame"], exclude=ms.Exclusion((), ("ＲＥＰＯＲＴ.TXT",))) is None
    # 名单清空后立即恢复（策略每次调用现读，1 秒缓存在 set_control 时作废）
    env.stack.set_control(hidden_from_agents={"apps": [], "title_keywords": []})
    assert "Quarterly REPORT draft" in text_of(call(mcp, "search", {"query": "report"}))


def test_day_summary_convention_for_hidden_lists(env):
    enable(env)
    w = env.stack.writer
    day = datetime.now().date().isoformat()
    w.summary_put("day", day, "上午整理季度报告。", refs=[{"piece": env.ids["piece"]}], model="m").result(5)
    _, mcp = make_mcp(env)
    out = text_of(call(mcp, "summary", {"level": "day", "key": "today"}))
    assert "> 上午整理季度报告。" in out and F.APP in out  # 涉及的应用来自引用的片段
    # 隐藏名单非空、概括没说明排除过 → 不给
    env.stack.set_control(hidden_from_agents={"apps": ["bank.exe"], "title_keywords": []})
    assert "没有可提供的概括" in text_of(call(mcp, "summary", {"level": "day", "key": "today"}))
    # 生成时已排除（名单覆盖当前名单，大小写不敏感）→ 给
    w.summary_put("day", day, "上午整理季度报告。", model="m",
                  refs=[{"piece": env.ids["piece"]}, {"hidden": {"apps": ["BANK.exe", "x.exe"],
                                                                 "title_keywords": []}}]).result(5)
    assert "> 上午整理季度报告。" in text_of(call(mcp, "summary", {"level": "day", "key": "today"}))
    # 当前名单多了一个关键词，生成时没排除它 → 不给
    env.stack.set_control(hidden_from_agents={"apps": ["bank.exe"], "title_keywords": ["网银"]})
    assert "没有可提供的概括" in text_of(call(mcp, "summary", {"level": "day", "key": "today"}))
    # 概括引用了被隐藏的应用 → mcp_server 自己也会拦
    env.stack.set_control(hidden_from_agents={"apps": [F.APP], "title_keywords": []})
    w.summary_put("day", day, "x", model="m",
                  refs=[{"piece": env.ids["piece"]}, {"hidden": {"apps": [F.APP], "title_keywords": []}}]).result(5)
    assert "没有可提供的概括" in text_of(call(mcp, "summary", {"level": "day", "key": "today"}))


def test_get_frame_downscales_and_scales_rects(env):
    enable(env)
    q, _ = make_mcp(env, max_image_edge=320)
    fr = q.get_frame(env.ids["frame"], exclude=ms.Exclusion())
    assert (fr.width, fr.height) == (320, 200) and Image.open(io.BytesIO(fr.image)).size == (320, 200)
    ln = next(x for x in fr.lines if x.line_id == env.ids["lines"][0])
    assert (ln.x, ln.y, ln.w, ln.h) == (10, 15, 290, 7)
    assert fr.app == F.APP and fr.title == F.TITLE and fr.seen_ms == F.day_base()
    assert q.get_frame("0" * 32, exclude=ms.Exclusion()) is None


# ---------------- 托盘 Controller ----------------

def test_controller_pause_resume_status(env):
    c = adapters.StoreController(env.stack, cache_s=0)
    assert c.status().text == "录制进程没有运行"
    until = datetime.now() + timedelta(hours=1)
    c.pause_until(until)
    st = c.status()
    assert st.paused and st.text == f"已暂停到 {until:%m-%d %H:%M}"
    c.pause_until(None)
    assert c.status().text == "已暂停" and env.stack.queries.control()["pause_until_ms"] is None
    c.resume()
    env.stack.writer.heartbeat(recorder_state="recording", current_process="tk.exe").result(5)
    env.stack.control.invalidate()
    assert c.status() == ms_status("正在录制：tk.exe")


def ms_status(text):
    from screenrecall.app.tray import TrayStatus
    return TrayStatus(text)


def test_controller_status_is_cached(env):
    c = adapters.StoreController(env.stack, cache_s=60)
    first = c.status()
    env.stack.writer.heartbeat(recorder_state="recording", current_process="tk.exe").result(5)
    env.stack.control.invalidate()
    assert c.status() is first  # 托盘每 5 秒问一次，不必每次读库


def test_controller_tick_ends_expired_pause(env):
    c = adapters.StoreController(env.stack, cache_s=0)
    env.stack.set_control(desired_state="paused", pause_until_ms=utc_ms() - 1000)
    c.tick()
    ctrl = env.stack.queries.control()
    assert ctrl["desired_state"] == "recording" and ctrl["pause_until_ms"] is None
    env.stack.set_control(desired_state="paused", pause_until_ms=None)
    c.tick()
    assert env.stack.queries.control()["desired_state"] == "paused"  # 一直暂停的不动


def test_controller_delete_recent_is_real(env):
    w = env.stack.writer
    t = utc_ms() - 60_000
    sid = w.segment_for("tk.exe", "sentinel", t).result(5)
    b = w.block_new(sid, t).result(5)
    w.record_lines(b, t, (0, 100), [LineIn(F.SENTINEL, 0, 10, 300, 24)]).result(5)
    adapters.StoreController(env.stack).delete_recent(5)
    res = env.stack.search.search(F.SENTINEL)
    assert res["groups"] == []
    assert env.stack.queries.conn().execute("SELECT COUNT(*) FROM lines WHERE text = ?",
                                            (F.SENTINEL,)).fetchone()[0] == 0
    assert env.stack.search.search("report")["total_groups"] == 1


def test_vector_delete_retried_later(env, monkeypatch):
    from screenrecall.store.vectors import VectorStore
    from screenrecall.v2.searchsvc import text_vectors_dir

    vs = VectorStore(text_vectors_dir(env.data), 8)
    vs.append([env.ids["line2"]], np.eye(8, dtype=np.float32)[:1], [F.day_base() + 5000], [env.ids["segment"]])
    real = VectorStore.delete
    calls = {"n": 0}

    def flaky(self, ids):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise TimeoutError("向量库被占用")
        return real(self, ids)

    monkeypatch.setattr(VectorStore, "delete", flaky)
    env.stack.deleter.retry_sleep_s = 0
    base = F.day_base()
    rep = env.stack.deleter.delete(time_from=base + 4_000, time_to=base + 6_000)
    assert rep["lines"] == 1 and rep["text_vectors"] == 0 and rep["vectors_pending"] == 1
    assert env.stack.deleter.retry() == 0
    assert len(VectorStore(text_vectors_dir(env.data), 8, readonly=True)) == 0


def test_pending_vector_deletes_survive_restart(env, monkeypatch):
    # 向量没删掉时主进程退出：待补删的 id 记在数据目录里，新的 Deleter（下次启动）接着删
    from screenrecall.app import paths
    from screenrecall.store.vectors import VectorStore
    from screenrecall.v2.searchsvc import text_vectors_dir

    vs = VectorStore(text_vectors_dir(env.data), 8)
    vs.append([env.ids["line2"]], np.eye(8, dtype=np.float32)[:1], [F.day_base() + 5000], [env.ids["segment"]])
    vs.close()
    real = VectorStore.delete

    def busy(self, ids):
        raise TimeoutError("向量库被占用")

    monkeypatch.setattr(VectorStore, "delete", busy)
    env.stack.deleter.retry_sleep_s = 0
    base = F.day_base()
    rep = env.stack.deleter.delete(time_from=base + 4_000, time_to=base + 6_000)
    assert rep["lines"] == 1 and rep["vectors_pending"] == 1
    pending = env.data / adapters.PENDING_VECTOR_DELETES
    assert json.loads(pending.read_text(encoding="utf-8")) == {"text": [env.ids["line2"]], "image": []}
    assert paths.verify_private(pending) == []

    monkeypatch.setattr(VectorStore, "delete", real)
    fresh = adapters.Deleter(env.stack.writer, env.data, retry_sleep_s=0)  # 模拟重启后的主进程
    assert fresh.retry() == 0
    assert len(VectorStore(text_vectors_dir(env.data), 8, readonly=True)) == 0
    assert not pending.exists()


def test_piece_summary_withheld_when_a_segment_is_hidden(env):
    # 片段本身（notepad.exe / report.txt）没命中，但它关联的另一个内容源标题含隐藏关键词：
    # 片段概括是按整段内容写的，不能给 Agent（时间线照样列出片段，只是不带概括）
    enable(env)
    w = env.stack.writer
    t = F.day_base() + 2 * 60_000
    secret = w.segment_for(F.APP, "网银 对账单.txt - 记事本", t).result(5)
    w.piece_put(F.day_base() - 1_000, F.day_base() + 10 * 60_000, app=F.APP, title=F.TITLE,
                segment_ids=[env.ids["segment"], secret], task_label="写报告", piece_id=env.ids["piece"]).result(5)
    _, mcp = make_mcp(env)
    key = str(env.ids["piece"])
    assert "> 整理季度报告" in text_of(call(mcp, "summary", {"level": "piece", "key": key}))
    env.stack.set_control(hidden_from_agents={"apps": [], "title_keywords": ["网银"]})
    assert "没有可提供的概括" in text_of(call(mcp, "summary", {"level": "piece", "key": key}))
    tl = text_of(call(mcp, "timeline", {"time_from": "today"}))
    assert f"piece={env.ids['piece']}" in tl and "整理季度报告" not in tl


def test_search_hit_does_not_name_a_hidden_piece(env):
    # 内容源没命中隐藏名单、它所属的片段命中了：命中照常给，但不报片段号和任务名
    w = env.stack.writer
    w.piece_put(F.day_base() - 1_000, F.day_base() + 10 * 60_000, app=F.APP, title="机密项目 周报",
                segment_ids=[env.ids["segment"]], task_label="机密项目", piece_id=env.ids["piece"]).result(5)
    q, _ = make_mcp(env)
    kw = dict(start_ms=None, end_ms=None, app=None, task=None, limit=5)
    hit = q.search("report", exclude=ms.Exclusion(), **kw)[0]
    assert hit.piece_id == str(env.ids["piece"]) and hit.task == "机密项目"
    hit = q.search("report", exclude=ms.Exclusion((), ("机密",)), **kw)[0]
    assert hit.text == "Quarterly REPORT draft" and hit.piece_id is None and hit.task is None


# ---------------- 第一次启动、设置文件、令牌 ----------------

def test_seed_control_only_once(tmp_path, monkeypatch):
    e = F.build_env(tmp_path, monkeypatch, fill_data=False)  # build_env 已经调过一次 seed_control
    try:
        ctrl = e.stack.queries.control()
        assert ctrl["desired_state"] == "recording" and ctrl["request_seq"] == 1
        assert ctrl["excluded_processes"] == list(e.stack.cfg.default_excluded_processes)
        e.stack.set_control(desired_state="paused")
        assert e.stack.seed_control() is False
        assert e.stack.queries.control()["desired_state"] == "paused"
    finally:
        e.stack.close()


def test_user_settings_file(tmp_path):
    assert v2config.read_user_settings(tmp_path) == {}
    assert v2config.write_user_settings(tmp_path, {"idle_minutes": 2}) == {"idle_minutes": 2.0}
    assert v2config.write_user_settings(tmp_path, {"ocr_model": "small"}) == {"idle_minutes": 2.0,
                                                                              "ocr_model": "small"}
    for bad in ({"idle_minutes": 0}, {"idle_minutes": True}, {"ocr_model": "x"}, {"other": 1}):
        with pytest.raises(ValueError):
            v2config.write_user_settings(tmp_path, bad)
    v2config.user_settings_path(tmp_path).write_text(json.dumps({"idle_minutes": -3, "ocr_model": "x"}),
                                                     encoding="utf-8")
    assert v2config.read_user_settings(tmp_path) == {}  # 手改坏了的值不生效
    v2config.user_settings_path(tmp_path).write_text("{bad", encoding="utf-8")
    assert v2config.read_user_settings(tmp_path) == {}
    cfg = v2config.load_v2(tmp_path, home=tmp_path / "h")
    assert cfg.idle.threshold_s == 300.0 and cfg.ocr.model == "tiny"


def test_authguard_set_token():
    from starlette.responses import PlainTextResponse
    from starlette.testclient import TestClient

    async def app(scope, receive, send):
        await PlainTextResponse("ok")(scope, receive, send)

    g = auth.AuthGuard(app, port=18735, token="a" * 40, nonces=auth.NonceStore())
    c = TestClient(g, base_url="http://127.0.0.1:18735")  # 不进 with：这个小应用不处理 lifespan
    assert c.get("/x", headers={"Authorization": "Bearer " + "a" * 40}).status_code == 200
    g.set_token("b" * 40)
    assert c.get("/x", headers={"Authorization": "Bearer " + "a" * 40}).status_code == 401
    assert c.get("/x", headers={"Authorization": "Bearer " + "b" * 40}).status_code == 200
    c.cookies.set(auth.cookie_name(18735), auth.cookie_value("b" * 40))
    assert c.get("/x").status_code == 200
    with pytest.raises(ValueError):
        g.set_token("short")
    cookie = auth.session_cookie(18735, "b" * 40)
    assert cookie.startswith("sr_session_18735=") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
