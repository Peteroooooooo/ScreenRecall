"""/api/v2：登录与 X-ScreenRecall 头、每个接口的返回结构、真实写线程写入的合成数据、隐私删除真删。"""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import numpy as np
import pytest
from starlette.testclient import TestClient

from screenrecall.app import auth, paths
from screenrecall.store import schema
from screenrecall.store.gate import PROBES
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import LineIn
from screenrecall.timeutil import utc_ms
from screenrecall.v2 import config as v2config
from screenrecall.v2 import web
from screenrecall.v2.searchsvc import SearchService, text_vectors_dir
from tests import test_v2_web_fixtures as F


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = F.build_env(tmp_path, monkeypatch)
    yield e
    e.stack.close()


@pytest.fixture
def client(env):
    with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
        F.login(c, env)
        yield c


def keys(d: dict) -> set:
    return set(d)


# ---------------- 登录与请求头 ----------------

def test_every_route_needs_login(env):
    with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
        for path in ("/api/v2/status", "/api/v2/day", "/api/v2/search?q=x", "/api/v2/settings", "/api/v2/storage",
                     f"/api/v2/frames/{env.ids['frame']}/image", "/api/v2/windows", "/api/v2/apps"):
            r = c.get(path)
            assert r.status_code == 401 and "error" in r.json(), path
        assert c.post("/api/v2/resume", json={}, headers=F.POST_H).status_code == 401
        assert c.get("/api/v2/status", headers={"Authorization": f"Bearer {F.TOKEN}"}).status_code == 200


def test_mutations_need_header(client):
    for path, body in [("/api/v2/pause", {"minutes": 5}), ("/api/v2/resume", {}), ("/api/v2/settings", {}),
                       ("/api/v2/delete", {"recent_minutes": 5}), ("/api/v2/agent/reset-token", {}),
                       ("/api/v2/settings/llm", {"base_url": "http://x", "model": "m"})]:
        r = client.post(path, json=body)
        assert r.status_code == 403 and r.json() == {"error": "缺少 X-ScreenRecall 请求头"}, path
    assert client.post("/api/v2/pause", content=b"{nope", headers=F.POST_H).status_code == 400
    assert client.post("/api/v2/pause", json=[1], headers=F.POST_H).status_code == 400


def test_unknown_v2_path_is_json_404(client):
    r = client.get("/api/v2/nope")
    assert r.status_code == 404 and r.json()["error"]


# ---------------- 状态与暂停 ----------------

STATUS_KEYS = {"recording", "pause_until_ms", "recorder_alive", "current", "pending_ocr", "idle_work", "queue",
               "agent_enabled", "revision"}


def test_status_pause_resume(client, env):
    s = client.get("/api/v2/status").json()
    assert keys(s) == STATUS_KEYS
    assert s["recording"] == "stopped" and s["recorder_alive"] is False and s["current"] is None
    assert s["pending_ocr"] == 1 and s["idle_work"] == "idle" and s["agent_enabled"] is False
    assert isinstance(s["revision"], int) and s["revision"] > 0

    t = utc_ms()
    s = client.post("/api/v2/pause", json={"minutes": 15}, headers=F.POST_H).json()
    assert s["recording"] == "paused" and abs(s["pause_until_ms"] - (t + 15 * 60_000)) < 5_000
    s = client.post("/api/v2/pause", json={"until": "tomorrow"}, headers=F.POST_H).json()
    tomorrow = datetime.fromtimestamp(s["pause_until_ms"] / 1000)
    assert (tomorrow.hour, tomorrow.minute) == (0, 0) and tomorrow.date() > datetime.now().date()
    assert client.post("/api/v2/pause", json={"until": "never"}, headers=F.POST_H).status_code == 400
    assert client.post("/api/v2/pause", json={"minutes": 0}, headers=F.POST_H).status_code == 400
    assert client.post("/api/v2/pause", json={}, headers=F.POST_H).status_code == 400

    s = client.post("/api/v2/resume", json={}, headers=F.POST_H).json()
    assert s["recording"] == "stopped" and s["pause_until_ms"] is None
    ctrl = env.stack.queries.control()
    assert ctrl["desired_state"] == "recording" and ctrl["pause_until_ms"] is None

    # 录制进程心跳 → 正在录制，显示当前窗口；刚加的排除规则命中它时按排除的前台说（不显示进程名和标题）
    env.stack.writer.heartbeat(recorder_state="recording", current_process="tk.exe", current_title="合成窗口").result(5)
    env.stack.control.invalidate()
    s = client.get("/api/v2/status").json()
    assert s["recording"] == "recording" and s["current"] == {"app": "tk.exe", "title": "合成窗口", "excluded": False}
    client.post("/api/v2/settings", json={"excluded_title_keywords": ["合成"]}, headers=F.POST_H)
    assert client.get("/api/v2/status").json()["current"] == {"app": None, "title": None, "excluded": True}
    client.post("/api/v2/settings", json={"excluded_title_keywords": [], "excluded_processes": ["TK.EXE"]},
                headers=F.POST_H)
    assert client.get("/api/v2/status").json()["current"] == {"app": None, "title": None, "excluded": True}
    # 前台是排除的窗口：录制进程不写进程名，只写占位标题
    from screenrecall.v2.rec_sink import EXCLUDED_TITLE

    env.stack.writer.heartbeat(recorder_state="recording", current_process=None, current_title=EXCLUDED_TITLE).result(5)
    env.stack.control.invalidate()
    assert client.get("/api/v2/status").json()["current"] == {"app": None, "title": None, "excluded": True}


def test_expired_pause_counts_as_recording(env):
    from screenrecall.v2.adapters import effective_state

    now = utc_ms()
    assert effective_state({"desired_state": "paused", "pause_until_ms": now - 1, "recorder_alive": True}, now) == \
        ("recording", None)
    assert effective_state({"desired_state": "paused", "pause_until_ms": None, "recorder_alive": False}, now) == \
        ("paused", None)
    # 录制进程正常退出时最后一次心跳写 stopped：心跳还新鲜（15 秒内）也不能算在录
    assert effective_state({"desired_state": "recording", "recorder_alive": True, "recorder_state": "stopped"},
                           now) == ("stopped", None)


def test_status_after_recorder_said_stopped(client, env):
    env.stack.writer.heartbeat(recorder_state="recording", current_process="tk.exe", current_title="合成").result(5)
    env.stack.control.invalidate()
    assert client.get("/api/v2/status").json()["recorder_alive"] is True
    env.stack.writer.heartbeat(recorder_state="stopped").result(5)
    env.stack.control.invalidate()
    s = client.get("/api/v2/status").json()
    # 正常停下：心跳还新鲜 → recorder_alive 仍为真、recording = stopped（页面显示"没在录制"，不是"没有响应"）
    assert (s["recording"], s["recorder_alive"], s["current"]) == ("stopped", True, None)


# ---------------- 一天、片段 ----------------

def test_day(client, env):
    d = client.get("/api/v2/day").json()
    assert keys(d) == {"date", "overview", "summary", "summary_state", "summary_refresh", "items"}
    assert d["summary_state"] == "none" and d["summary_refresh"] is False  # 没配大模型、没有 SummaryRunner
    assert d["date"] == datetime.now().date().isoformat()
    assert keys(d["overview"]) == {"active_ms", "apps", "lines_new", "frames"}
    assert d["overview"]["apps"] == [{"app": F.APP, "ms": 10 * 60_000}] and d["overview"]["active_ms"] == 600_000
    assert d["overview"]["lines_new"] == 4 and d["overview"]["frames"] == 1
    assert d["summary"] is None
    pieces = [x for x in d["items"] if x["kind"] == "piece"]
    gaps = [x for x in d["items"] if x["kind"] == "gap"]
    assert pieces == [{"kind": "piece", "piece_id": env.ids["piece"], "start_ms": pieces[0]["start_ms"],
                       "end_ms": pieces[0]["end_ms"], "app": F.APP, "title": F.TITLE, "task": "写报告",
                       "summary_line": "整理季度报告", "pending_ocr": True}]
    # 两条重叠的锁屏缺口合并成一条；片段里面的最小化缺口不算
    base = F.day_base()
    assert gaps == [{"kind": "gap", "start_ms": base - 30 * 60_000, "end_ms": base - 15 * 60_000,
                     "reason": "locked", "note": None}]
    starts = [x["start_ms"] for x in d["items"]]
    assert starts == sorted(starts, reverse=True)  # 新的在前

    other = client.get("/api/v2/day?date=2001-01-01").json()
    assert other["items"] == [] and other["overview"]["frames"] == 0
    assert client.get("/api/v2/day?date=bad").status_code == 400


def test_piece_detail(client, env):
    r = client.get(f"/api/v2/pieces/{env.ids['piece']}").json()
    assert keys(r) == {"piece", "summary", "summary_state", "blocks", "unjoined", "frames"}
    assert r["piece"] == {"piece_id": env.ids["piece"], "start_ms": r["piece"]["start_ms"],
                          "end_ms": r["piece"]["end_ms"], "app": F.APP, "title": F.TITLE, "task": "写报告"}
    assert r["summary"] == {"text": "整理季度报告\n第二行细节", "status": "ok", "model": "test-model"}
    assert r["unjoined"] is True and len(r["blocks"]) == 2
    b1, b2 = r["blocks"]
    assert b1["block_id"] == env.ids["blocks"][0] and b2["block_id"] == env.ids["blocks"][1]
    kinds = [x["kind"] for x in b1["items"]]
    assert kinds == ["line", "line", "line", "pending", "skim"]  # 按块内位置：行 y30–130，待 OCR y300，划过 y400
    line = b1["items"][0]
    assert line == {"kind": "line", "line_id": env.ids["lines"][0], "text": "Quarterly REPORT draft",
                    "source": "uia", "first_seen_ms": F.day_base(), "frame_id": env.ids["frame"]}
    pend = b1["items"][3]
    assert keys(pend) == {"kind", "region_id", "frame_id"} and pend["frame_id"] == env.ids["frame"]
    assert b1["items"][4] == {"kind": "skim", "start_ms": F.day_base() + 2000, "end_ms": F.day_base() + 3000,
                              "distance_px": 1200}
    assert b2["items"][0]["text"] == "another block line" and b2["items"][0]["frame_id"] is None
    assert r["frames"] == [{"frame_id": env.ids["frame"], "captured_ms": F.day_base(), "width": 640, "height": 400}]
    assert client.get("/api/v2/pieces/999999").status_code == 404
    assert client.get("/api/v2/pieces/abc").status_code == 400


def test_summarize_queues_the_job_the_worker_reads(client, env):
    """重新生成：已有 ok 概括的片段 → 概括标 stale + 待办 (summary_piece, 片段编号)，空闲整理进程的
    LlmStep.candidates() 立即能领到（不等片段结束）。"""
    from screenrecall.v2.work_common import Ctx, StopFlag
    from screenrecall.v2.work_llm import PIECE_JOB, LlmStep

    pid = env.ids["piece"]
    assert client.get(f"/api/v2/pieces/{pid}").json()["summary_state"] == "ok"
    r = client.post(f"/api/v2/pieces/{pid}/summarize", json={}, headers=F.POST_H)
    assert r.json() == {"queued": True, "summary_state": "queued"}
    conn = env.stack.queries.conn()
    assert [tuple(x) for x in conn.execute("SELECT kind, target, status FROM jobs")] == [(PIECE_JOB, str(pid), "pending")]
    assert conn.execute("SELECT status FROM summaries WHERE level = 'piece' AND key = ?", (str(pid),)).fetchone()[0] \
        == "stale"
    assert client.get(f"/api/v2/pieces/{pid}").json()["summary_state"] == "queued"
    ctx = Ctx(env.stack.cfg, env.stack.writer, StopFlag(), clock=lambda: env.ids["piece_start"] + 60_000)
    try:
        assert [int(c["piece_id"]) for c in LlmStep(ctx, None).candidates()] == [pid]  # 片段还没结束也领
    finally:
        ctx.close()
    assert client.post("/api/v2/pieces/424242/summarize", json={}, headers=F.POST_H).status_code == 404


def test_private_rules_follow_the_control_lists_only(env):
    """默认排除规则在第一次启动时写进 control；用户从名单里删掉后，不再拦它的概括。"""
    from screenrecall.v2.work_common import private_rules

    ctrl = env.stack.queries.control()
    assert "KeePass.exe" in ctrl["excluded_processes"]
    assert private_rules(ctrl).match("KeePass.exe", "x")
    env.stack.set_control(excluded_processes=[])
    assert not private_rules(env.stack.queries.control()).match("KeePass.exe", "x")
    env.stack.set_control(hidden_from_agents={"apps": [], "title_keywords": ["工资"]})
    assert private_rules(env.stack.queries.control()).match("excel.exe", "十月工资表")


def test_summarize_refuses_private_piece(client, env):
    pid = env.ids["piece"]
    env.stack.set_control(hidden_from_agents={"apps": [F.APP], "title_keywords": []})
    assert client.get(f"/api/v2/pieces/{pid}").json()["summary_state"] == "private"
    assert client.post(f"/api/v2/pieces/{pid}/summarize", json={}, headers=F.POST_H).status_code == 409
    assert env.stack.queries.conn().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_day_gap_reasons_match_what_the_recorder_writes(client, env):
    from screenrecall.v2.rec_sink import NOT_RUNNING_DETAIL, STOPPED_DETAIL

    w = env.stack.writer
    b = F.day_base() + 3 * 3600_000  # 13:00 起，和合成数据（10:00 前后）不重叠
    m = 60_000
    w.gap(b, b + 5 * m, "closed", detail=STOPPED_DETAIL).result(5)               # 正常退出 → stopped
    w.gap(b + 10 * m, b + 15 * m, "error", detail=NOT_RUNNING_DETAIL).result(5)   # 崩溃 → stopped
    w.gap(b + 20 * m, b + 25 * m, "closed", detail="window closed").result(5)     # 窗口关了：不是空档
    w.gap(b + 30 * m, b + 35 * m, "minimised", detail="cloaked", hwnd=5, process="chat.exe").result(5)
    w.gap(b + 40 * m, b + 45 * m, "minimised", hwnd=5, process="chat.exe").result(5)
    w.gap(b + 50 * m, b + 55 * m, "excluded", hwnd=6, process="KeePass.exe").result(5)
    # 固定的聊天窗口最小化了一小时，期间用户一直在别的窗口工作：只有没在录别的窗口的那 10 分钟算空档
    w.gap(b + 60 * m, b + 120 * m, "minimised", hwnd=5, process="chat.exe").result(5)
    w.activity_start(b + 60 * m, hwnd=7, pid=1, process="code.exe", title="main.py").result(5)
    w.activity_start(b + 100 * m, hwnd=6, pid=2, process="KeePass.exe", title="[已排除]", excluded=True).result(5)
    w.activity_start(b + 110 * m, hwnd=7, pid=1, process="code.exe", title="main.py").result(5)
    w.activity_end(b + 119 * m + 30_000).result(5)  # 最后 30 秒：不到一分钟，不显示
    gaps = [(x["start_ms"] - b) // m for x in client.get("/api/v2/day").json()["items"]
            if x["kind"] == "gap" and x["start_ms"] >= b]
    rows = {(x["start_ms"] - b) // m: (x["reason"], x["note"]) for x in client.get("/api/v2/day").json()["items"]
            if x["kind"] == "gap" and x["start_ms"] >= b}
    assert rows == {0: ("stopped", None), 10: ("stopped", None), 30: ("cloaked", None), 40: ("minimized", None),
                    50: ("excluded", "KeePass.exe"), 100: ("minimized", None)}
    assert len(gaps) == len(rows)


class FakeClock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> int:
        return self.t


def test_today_shows_activity_before_the_idle_worker(tmp_path, monkeypatch):
    """主进程每 30 秒切片段（LivePieces）："今天"页面不等空闲整理进程；进行中的片段延长到现在；
    整理进程之后处理同一批记录，片段编号不变、不会多出一份。"""
    from screenrecall.store.writer import Writer
    from screenrecall.v2.work_common import Ctx, FileLock, StopFlag
    from screenrecall.v2.work_pieces import PIECES_LOCK, PieceStep

    clock = FakeClock(F.day_base() + 2 * 3600_000)
    e = F.build_env(tmp_path, monkeypatch, fill_data=False, clock=clock)
    try:
        w = e.stack.writer
        t0 = clock.t - 10 * 60_000
        w.activity_start(t0, hwnd=1, pid=1, process="code.exe", title="main.py").result(5)
        w.heartbeat(recorder_state="recording").result(5)
        with TestClient(e.guard, base_url=F.BASE, follow_redirects=False) as c:
            F.login(c, e)
            assert [x for x in c.get("/api/v2/day").json()["items"] if x["kind"] == "piece"] == []
            e.stack.pieces.run()
            items = [x for x in c.get("/api/v2/day").json()["items"] if x["kind"] == "piece"]
            assert [(x["app"], x["title"], x["start_ms"], x["end_ms"]) for x in items] == \
                [("code.exe", "main.py", t0, clock.t)]
            pid = items[0]["piece_id"]
            assert c.get(f"/api/v2/pieces/{pid}").json()["summary_state"] == "waiting"

            clock.t += 40_000  # 还在用同一个窗口：片段延长到现在，编号不变
            w.heartbeat(recorder_state="recording").result(5)
            e.stack.pieces.run()
            items = [x for x in c.get("/api/v2/day").json()["items"] if x["kind"] == "piece"]
            assert [(x["piece_id"], x["end_ms"]) for x in items] == [(pid, clock.t)]

            # 另一个进程拿着锁（整理进程正在切）：这次不切
            lock = FileLock(e.data / PIECES_LOCK)
            assert lock.acquire()
            try:
                clock.t += 30_000
                w.activity_start(clock.t, hwnd=2, pid=2, process="tk.exe", title="合成").result(5)
                assert e.stack.pieces.run() == 0
            finally:
                lock.release()

            # 空闲整理进程（自己的写线程）接着切：同一个片段，只多出新窗口的片段
            with Writer(e.data, e.stack.cfg.writer, clock=clock) as w2:
                ctx = Ctx(e.stack.cfg, w2, StopFlag(), clock=clock)
                try:
                    assert PieceStep(ctx).run() == 1
                finally:
                    ctx.close()
            items = [x for x in c.get("/api/v2/day").json()["items"] if x["kind"] == "piece"]
            assert sorted((x["app"], x["piece_id"] == pid) for x in items) == [("code.exe", True), ("tk.exe", False)]
            assert e.stack.queries.conn().execute("SELECT COUNT(*) FROM pieces").fetchone()[0] == 2
    finally:
        e.stack.close()


def test_piece_put_does_not_bring_back_purged_time(tmp_path):
    """切片段时先读记录、再写：中间发生的隐私删除不能让删掉的时段作为新片段回来。"""
    from screenrecall.store.writer import Writer

    with Writer(tmp_path) as w:
        w.call(lambda c: c.execute("INSERT INTO purges (time_from, time_to, app, created_ms) VALUES (0, 5000, NULL, 1)"))
        assert w.piece_put(1000, 4000, app="a.exe", title="t", segment_ids=[]).result(5) is None
        pid = w.piece_put(1000, 9000, app="a.exe", title="t", segment_ids=[]).result(5)
        conn = schema.connect_ro(schema.db_path(tmp_path))
        try:
            assert tuple(conn.execute("SELECT start_ms, end_ms FROM pieces WHERE piece_id = ?", (pid,)).fetchone()) \
                == (5000, 9000)
        finally:
            conn.close()


# ---------------- 检索 ----------------

def test_spans_are_case_and_width_insensitive():
    assert web.match_spans("Quarterly REPORT draft", ["report"]) == [[10, 16]]
    assert web.match_spans("ＦＵＬＬＷＩＤＴＨ Text", ["fullwidth"]) == [[0, 9]]
    assert web.match_spans("组会 安排", ["会安"]) == [[1, 4]]  # 跨空白
    assert web.match_spans("abc abc", ["abc"]) == [[0, 3], [4, 7]]
    assert web.match_spans("aaa", ["aa"]) == [[0, 3]]  # 重叠合并
    assert web.match_spans("末尾是组", ["组会"]) == [[3, 4]]  # 折到下一行的命中标出本行那一截
    assert web.match_spans("nothing", ["zzz"]) == []


def test_identical_hits_in_a_group_are_shown_once():
    lines = [{"line_id": 3, "text": "Typed note: zebra"}, {"line_id": 2, "text": "typed  NOTE: zebra"},
             {"line_id": 1, "text": "Typed note: zeb"}]
    assert [ln["line_id"] for ln in web.unique_lines(lines)] == [3, 1]


def test_short_word_terms_only_highlight_word_starts():
    """字面检索里 a、to 这样的短词是"以它开头的词"：高亮也只认词首，不把 rearranged 里的 a 都标出来。"""
    text = "The guard rearranged a telescope to the tower"
    assert web.match_spans(text, [("a", True)]) == [[21, 22]]
    assert web.match_spans(text, [("to", True)]) == [[33, 35], [40, 42]]
    assert web.match_spans(text, [("a", False)])[:2] == [[6, 7], [12, 13]]  # 长词（三字组）照旧按子串
    assert web.match_spans("say to", [("ab", True)]) == []  # 行尾 "a" 不在词首：不当折行命中
    assert web.match_spans("a b ab", [("abc", True)]) == [[4, 6]]
    assert web.match_spans("组会 to", [("会", False), ("to", True)]) == [[1, 2], [3, 5]]


def test_search_shape_and_spans(client, env):
    r = client.get("/api/v2/search", params={"q": "report"}).json()
    assert keys(r) == {"query", "sort", "offset", "total_groups", "groups", "has_more", "next_offset", "semantic"}
    assert r["query"] == "report" and r["total_groups"] == 1 and r["semantic"] is False
    assert r["sort"] == "relevant" and r["offset"] == 0 and r["has_more"] is False and r["next_offset"] is None
    g = r["groups"][0]
    assert keys(g) == {"segment_id", "app", "title", "first_seen_ms", "last_seen_ms", "hits_total", "piece_id",
                       "match", "hits"}
    assert g["match"] == "literal"
    assert g["segment_id"] == env.ids["segment"] and g["app"] == F.APP and g["piece_id"] == env.ids["piece"]
    assert g["hits_total"] == 1 and g["first_seen_ms"] == F.day_base()
    assert g["hits"] == [{"line_id": env.ids["lines"][0], "text": "Quarterly REPORT draft", "spans": [[10, 16]],
                          "last_seen_ms": F.day_base(), "frame_id": env.ids["frame"]}]
    r = client.get("/api/v2/search", params={"q": "fullwidth 全角"}).json()
    assert r["groups"][0]["hits"][0]["spans"] == [[0, 9], [15, 17]]
    assert client.get("/api/v2/search", params={"q": "report", "app": "other.exe"}).json()["groups"] == []
    assert client.get("/api/v2/search", params={"q": "report", "task": "写报告"}).json()["total_groups"] == 1
    assert client.get("/api/v2/search", params={"q": "report", "from": F.day_base() + 1}).json()["groups"] == []
    assert client.get("/api/v2/search", params={"q": ""}).status_code == 400
    assert client.get("/api/v2/search", params={"q": '"unpaired'}).status_code == 400
    assert client.get("/api/v2/search", params={"q": "x", "limit": 0}).status_code == 400
    assert client.get("/api/v2/search", params={"q": "x", "from": 5, "to": 1}).status_code == 400
    assert client.get("/api/v2/search", params={"q": "x", "sort": "best"}).status_code == 400
    assert client.get("/api/v2/search", params={"q": "x", "offset": -1}).status_code == 400
    # 任务筛选：大小写、全半角、空白不敏感的包含匹配（任务标签是"写报告"）
    assert client.get("/api/v2/search", params={"q": "report", "task": "报告"}).json()["total_groups"] == 1
    assert client.get("/api/v2/search", params={"q": "report", "task": "写 报告"}).json()["total_groups"] == 1
    assert client.get("/api/v2/search", params={"q": "report", "task": "开会"}).json()["groups"] == []


def test_search_paging_and_sort(client, env):
    w = env.stack.writer
    sids = []
    for i in range(5):
        t = F.day_base() + (i + 1) * 3_600_000
        sid = w.segment_for("tk.exe", f"合成文档 {i}", t).result(5)
        w.record_lines(w.block_new(sid, t).result(5), t, (0, 100), [LineIn(f"page REPORT {i}", 0, 10, 300, 24)]).result(5)
        sids.append(sid)
    want = list(reversed(sids)) + [env.ids["segment"]]  # 没有语义分支：相关度一样，按最近
    for sort in ("relevant", "recent"):
        got, offset = [], 0
        while offset is not None:
            r = client.get("/api/v2/search", params={"q": "report", "limit": 2, "offset": offset, "sort": sort}).json()
            assert r["sort"] == sort and r["offset"] == offset and len(r["groups"]) <= 2
            got += [g["segment_id"] for g in r["groups"]]
            offset = r["next_offset"]
            assert (offset is not None) == r["has_more"]
        assert got == want
    assert r["total_groups"] == 6


def test_tasks_endpoint(client, env):
    w = env.stack.writer
    sid = env.ids["segment"]
    t = F.day_base()
    w.piece_put(t + 20 * 60_000, t + 30 * 60_000, app=F.APP, title="b", segment_ids=[sid], task_label="写报告").result(5)
    w.piece_put(t + 40 * 60_000, t + 50 * 60_000, app=F.APP, title="c", segment_ids=[sid], task_label="Ｒｅｖｉｅｗ").result(5)
    r = client.get("/api/v2/tasks").json()
    assert r == {"tasks": [{"task": "写报告", "n": 2}, {"task": "Ｒｅｖｉｅｗ", "n": 1}]}
    r = client.get("/api/v2/tasks", params={"from": t + 35 * 60_000}).json()
    assert r == {"tasks": [{"task": "Ｒｅｖｉｅｗ", "n": 1}]}
    assert client.get("/api/v2/tasks", params={"from": 5, "to": 1}).status_code == 400


def test_search_warm_without_semantic(client, env):
    assert client.get("/api/v2/search/warm").json() == {"semantic": False}


def test_search_groups_by_most_recent_hit(client, env):
    w = env.stack.writer
    t = F.day_base() + 3_600_000
    sid = w.segment_for("tk.exe", "合成文档", t).result(5)
    b = w.block_new(sid, t).result(5)
    w.record_lines(b, t, (0, 100), [LineIn("another REPORT here", 0, 10, 300, 24)]).result(5)
    r = client.get("/api/v2/search", params={"q": "report"}).json()
    assert [g["segment_id"] for g in r["groups"]] == [sid, env.ids["segment"]]
    assert r["groups"][0]["piece_id"] is None and r["groups"][0]["hits"][0]["frame_id"] is None


DIM = 8


class FakeEncoder:
    loaded = True

    def __init__(self, vec):
        self.vec = vec
        self.calls = 0

    def encode(self, texts):
        texts = list(texts)
        if texts == list(PROBES):  # 命中判定的泛泛查询：放在测试向量都不用的方向上（不计入查询次数）
            return np.stack([np.eye(DIM, dtype=np.float32)[DIM - 1] for _ in texts])
        self.calls += 1
        return np.stack([self.vec for _ in texts])

    def unload(self):
        pass


def test_hits_without_their_own_frame_fall_back_to_a_frame_of_the_same_block(client, env):
    """命中的行没有登记在任何证据帧上（UIA 读到字的那一屏没存成证据帧）：用同一块里画面范围盖住这一行的帧；
    盖不住时用这行出现之后同一块的第一张帧；同一块一张帧都没有时仍是 null（页面不显示缩略图）。"""
    w = env.stack.writer
    t0 = F.day_base()
    b1 = env.ids["blocks"][0]
    sid = env.ids["segment"]
    w.record_lines(b1, t0 + 1_000, (140, 170), [LineIn("fallback alpha covered", 20, 150, 600, 164)]).result(5)
    w.record_lines(b1, t0 + 3_000, (1190, 1220), [LineIn("fallback bravo below", 20, 1200, 600, 1214)]).result(5)
    f2 = w.save_frame(F.synthetic_frame(seed=2), captured_ms=t0 + 4_000, segment_id=sid, block_id=b1, pos_y=600,
                      app=F.APP, title=F.TITLE)
    w.record_lines(b1, t0 + 5_000, (690, 720), [LineIn("fallback charlie second", 20, 700, 600, 714)]).result(5)
    w.flush()
    r = client.get("/api/v2/search", params={"q": "fallback"}).json()
    hits = {h["text"]: h["frame_id"] for g in r["groups"] for h in g["hits"]}
    assert hits == {"fallback alpha covered": env.ids["frame"], "fallback bravo below": f2,
                    "fallback charlie second": f2}
    # 片段阅读视图用同一套
    items = [x for b in client.get(f"/api/v2/pieces/{env.ids['piece']}").json()["blocks"] for x in b["items"]
             if x["kind"] == "line"]
    by = {x["text"]: x["frame_id"] for x in items}
    assert by["fallback alpha covered"] == env.ids["frame"] and by["another block line"] is None


def test_search_semantic_branch(tmp_path, monkeypatch):
    e = np.eye(DIM, dtype=np.float32)
    enc = FakeEncoder(e[0])
    # 先建一个空环境拿到数据目录，再放向量库
    env = F.build_env(tmp_path, monkeypatch)
    try:
        vs = VectorStore(text_vectors_dir(env.data), DIM)
        lid = env.ids["line2"]
        vs.append([lid], e[0][None, :], [F.day_base() + 5000], [env.ids["segment"]])
        svc = SearchService(env.data, encoder=enc, model_check=lambda: True)
        env.stack.search = svc
        with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
            F.login(c, env)
            r = c.get("/api/v2/search", params={"q": "完全不相关的词"}).json()
            assert r["semantic"] is True and enc.calls == 1
            g = r["groups"][0]
            assert g["hits"][0]["line_id"] == lid and g["hits"][0]["spans"] == [] and g["hits_total"] == 1
            assert g["match"] == "semantic"
            # 预热：语义检索能用时返回 True（注入的模型已经"加载"，不再编码）
            assert c.get("/api/v2/search/warm").json() == {"semantic": True} and enc.calls == 1
            # 低于阈值的纯语义命中不要
            enc.vec = e[1]
            assert c.get("/api/v2/search", params={"q": "完全不相关的词"}).json()["groups"] == []
        svc.close()
    finally:
        env.stack.close()


def test_vector_only_hits_get_no_tail_spans(tmp_path, monkeypatch):
    # 纯语义命中的行不是字面命中：行尾碰巧是检索词的第一个字（"line" 的 e / "exotic" 的 e）不能标高亮
    e = np.eye(DIM, dtype=np.float32)
    env = F.build_env(tmp_path, monkeypatch)
    try:
        vs = VectorStore(text_vectors_dir(env.data), DIM)
        vs.append([env.ids["line2"]], e[0][None, :], [F.day_base() + 5000], [env.ids["segment"]])
        env.stack.search = SearchService(env.data, encoder=FakeEncoder(e[0]), model_check=lambda: True)
        with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
            F.login(c, env)
            r = c.get("/api/v2/search", params={"q": "exotic"}).json()
            assert r["semantic"] is True
            hit = r["groups"][0]["hits"][0]
            assert hit["text"] == "another block line" and hit["spans"] == []
        assert web.match_spans("another block line", ["exotic"], tails=False) == []
        assert web.match_spans("another block line", ["exotic"]) == [[17, 18]]  # 字面命中的行仍标折行的那一截
        env.stack.search.close()
    finally:
        env.stack.close()


def test_vector_score_floor_applies_before_limit(tmp_path, monkeypatch):
    # 低分的纯语义组不能占掉 limit 的名额：字面命中的两组都要在结果里
    e = np.eye(DIM, dtype=np.float32)
    low = (0.1 * e[0] + np.sqrt(0.99) * e[1]).astype(np.float32)  # 与查询向量的余弦 0.1 < 0.45
    env = F.build_env(tmp_path, monkeypatch)
    try:
        w = env.stack.writer
        t = F.day_base() + 3_600_000
        lex_b = w.segment_for("tk.exe", "合成文档 B", t).result(5)
        w.record_lines(w.block_new(lex_b, t).result(5), t, (0, 100),
                       [LineIn("another REPORT here", 0, 10, 300, 24)]).result(5)
        vec_ids, vec_segs = [], []
        for i, title in enumerate(("合成 C", "合成 D")):
            tv = t + 3_600_000 + i * 1000  # 比字面命中更新：同分时排在前面
            sid = w.segment_for("tk.exe", title, tv).result(5)
            ids = w.record_lines(w.block_new(sid, tv).result(5), tv, (0, 100),
                                 [LineIn(f"unrelated zzz {i}", 0, 10, 300, 24)]).result(5)
            vec_ids += ids
            vec_segs.append(sid)
        vs = VectorStore(text_vectors_dir(env.data), DIM)
        vs.append(vec_ids, np.stack([low, low]), [t, t], vec_segs)
        env.stack.search = SearchService(env.data, encoder=FakeEncoder(e[0]), model_check=lambda: True)
        with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
            F.login(c, env)
            r = c.get("/api/v2/search", params={"q": "report", "limit": 2}).json()
            assert r["semantic"] is True
            assert sorted(g["segment_id"] for g in r["groups"]) == sorted([env.ids["segment"], lex_b])
        env.stack.search.close()
    finally:
        env.stack.close()


def test_semantic_off_without_model_or_vectors(tmp_path):
    svc = SearchService(tmp_path, model_check=lambda: True, encoder=FakeEncoder(np.ones(DIM, np.float32)))
    schema.connect(schema.db_path(tmp_path)).close()
    assert svc.search("x")["semantic"] is False  # 没有向量库
    svc2 = SearchService(tmp_path, model_check=lambda: False)
    assert svc2.search("x")["semantic"] is False
    svc.close()
    svc2.close()


# ---------------- 证据帧 ----------------

def test_frame_detail_image_thumb(client, env):
    fid = env.ids["frame"]
    r = client.get(f"/api/v2/frames/{fid}").json()
    assert keys(r) == {"frame_id", "captured_ms", "width", "height", "app", "title", "piece_id", "lines"}
    assert (r["width"], r["height"], r["app"], r["title"], r["piece_id"]) == (640, 400, F.APP, F.TITLE,
                                                                              env.ids["piece"])
    assert r["lines"][0] == {"line_id": env.ids["lines"][0], "text": "Quarterly REPORT draft", "x0": 20, "y0": 30,
                             "x1": 600, "y1": 44}
    img = client.get(f"/api/v2/frames/{fid}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    assert img.content[:4] == b"RIFF" and "private" in img.headers["cache-control"]
    # 删除后图片不能还留在浏览器磁盘缓存里
    assert "no-store" in img.headers["cache-control"] and "max-age" not in img.headers["cache-control"]
    th = client.get(f"/api/v2/frames/{fid}/thumb?w=160")
    assert th.status_code == 200 and th.headers["content-type"] == "image/jpeg" and th.content[:2] == b"\xff\xd8"
    assert "no-store" in th.headers["cache-control"]
    from PIL import Image
    import io

    assert Image.open(io.BytesIO(th.content)).size == (160, 100)
    assert client.get(f"/api/v2/frames/{fid}/thumb?w=5").status_code == 400
    for bad in ("0" * 32, "../../etc", "x"):
        assert client.get(f"/api/v2/frames/{bad}").status_code == 404
        assert client.get(f"/api/v2/frames/{bad}/image").status_code == 404


def test_apps(client, env):
    assert client.get("/api/v2/apps").json() == {"apps": [{"app": F.APP, "n": 1}]}
    assert client.get("/api/v2/apps", params={"from": F.day_base() + 3_600_000}).json() == {"apps": []}


# ---------------- 设置 ----------------

SETTINGS_KEYS = {"pinned", "excluded_processes", "excluded_title_keywords", "hidden_from_agents", "agent_enabled",
                 "idle_minutes", "ocr_model", "autostart", "llm", "data_dir", "image_days", "text_days",
                 "storage_cap_gb"}


def test_settings_roundtrip(client, env):
    s = client.get("/api/v2/settings").json()
    assert keys(s) == SETTINGS_KEYS
    assert s["agent_enabled"] is False and s["idle_minutes"] == 5 and s["ocr_model"] == "tiny"
    assert s["autostart"] is False and s["data_dir"] == str(env.data)
    assert s["llm"] == {"configured": False, "model": None, "host": None}
    assert s["hidden_from_agents"] == {"apps": [], "title_keywords": []}

    body = {"pinned": [{"process": "tk.exe", "title": "合成窗口", "hwnd": 11}],
            "excluded_processes": ["KeePass.exe", " ", "KeePass.exe"], "excluded_title_keywords": ["InPrivate"],
            "hidden_from_agents": {"apps": ["bank.exe"]}, "agent_enabled": True, "idle_minutes": 7.5,
            "ocr_model": "small", "autostart": True}
    s = client.post("/api/v2/settings", json=body, headers=F.POST_H).json()
    # hwnd 原样带回（标题变了也能按句柄找到）；found：现在能找到这个窗口
    assert s["pinned"] == [{"process": "tk.exe", "title": "合成窗口", "hwnd": 11, "found": True}]
    assert s["excluded_processes"] == ["KeePass.exe"] and s["excluded_title_keywords"] == ["InPrivate"]
    assert s["hidden_from_agents"] == {"apps": ["bank.exe"], "title_keywords": []}
    assert s["agent_enabled"] is True and s["idle_minutes"] == 7.5 and s["ocr_model"] == "small"
    assert s["autostart"] is True and env.ctx.autostart.enabled
    ctrl = env.stack.queries.control()
    assert ctrl["pinned_windows"] == [{"process": "tk.exe", "title": "合成窗口", "hwnd": 11}]
    assert env.settings_seen[-1]["idle_minutes"] == 7.5
    # 空闲阈值 / OCR 模型写在数据目录里，录制进程和空闲整理进程用 load_v2 读到
    cfg = v2config.load_v2(env.data, home=env.home)
    assert cfg.idle.threshold_s == 450 and cfg.ocr.model == "small"
    assert paths.verify_private(v2config.user_settings_path(env.data)) == []
    # 只改一部分：其他不变
    s = client.post("/api/v2/settings", json={"hidden_from_agents": {"title_keywords": ["网银"]}},
                    headers=F.POST_H).json()
    assert s["hidden_from_agents"] == {"apps": ["bank.exe"], "title_keywords": ["网银"]}
    assert client.post("/api/v2/settings", json={"autostart": False}, headers=F.POST_H).json()["autostart"] is False
    s = client.post("/api/v2/settings", json={"pinned": [{"process": "x.exe", "title": "已经关了的窗口"}]},
                    headers=F.POST_H).json()
    assert s["pinned"] == [{"process": "x.exe", "title": "已经关了的窗口", "hwnd": None, "found": False}]

    for bad in [{"nope": 1}, {"pinned": [{"process": "a"}] * 3}, {"agent_enabled": "yes"}, {"idle_minutes": 0},
                {"ocr_model": "huge"}, {"excluded_processes": "x"}, {"excluded_processes": ["a\nb"]},
                {"hidden_from_agents": {"apps": ["a"], "x": []}}, {"autostart": 1}]:
        r = client.post("/api/v2/settings", json=bad, headers=F.POST_H)
        assert r.status_code == 400 and r.json()["error"], bad
    # 校验失败时什么都没写
    assert client.post("/api/v2/settings", json={"agent_enabled": False, "ocr_model": "huge"},
                       headers=F.POST_H).status_code == 400
    assert client.get("/api/v2/settings").json()["agent_enabled"] is True


def test_llm_settings_never_echo_key(client, env):
    key = "sk-test-" + "k" * 30
    body = {"base_url": "https://llm.example.test/v1", "model": "demo-model", "api_key": key}
    r = client.post("/api/v2/settings/llm", json=body, headers=F.POST_H)
    assert r.status_code == 200 and key not in r.text
    assert r.json()["llm"] == {"configured": True, "model": "demo-model", "host": "llm.example.test"}
    env_file = env.home / ".env"
    assert paths.verify_private(env_file) == []
    assert f"LLM_API_KEY={key}" in env_file.read_text(encoding="utf-8")
    # 换了服务器（或 https → http）却不给 api_key：拒绝，旧密钥不能发给新地址；原配置不动
    for other in ["https://other.example.test/v1", "http://llm.example.test/v1", "https://llm.example.test:8443/v1"]:
        r = client.post("/api/v2/settings/llm", json={"base_url": other, "model": "m2"}, headers=F.POST_H)
        assert r.status_code == 400 and "密钥" in r.json()["error"], other
    text = env_file.read_text(encoding="utf-8")
    assert "LLM_BASE_URL=https://llm.example.test/v1" in text and "m2" not in text
    # 同一服务器只换路径 / 模型，不给 api_key = 保留原来的
    client.post("/api/v2/settings/llm", json={"base_url": "https://LLM.example.test/v2", "model": "m2"},
                headers=F.POST_H)
    text = env_file.read_text(encoding="utf-8")
    assert f"LLM_API_KEY={key}" in text and "m2" in text
    assert key not in client.get("/api/v2/settings").text
    for bad in [{"base_url": "ftp://x", "model": "m"}, {"base_url": "https://x", "model": ""},
                {"base_url": "https://x", "model": "m", "api_key": "a b"}, {"model": "m"}]:
        assert client.post("/api/v2/settings/llm", json=bad, headers=F.POST_H).status_code == 400, bad

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == f"Bearer {key}"
        return httpx.Response(200, json={"model": "m2", "choices": [{"message": {"content": "OK"}}]})

    env.ctx.llm_transport = httpx.MockTransport(handler)
    r = client.post("/api/v2/settings/llm/test", json={}, headers=F.POST_H).json()
    assert r["ok"] is True and r["model"] == "m2" and isinstance(r["latency_ms"], int)
    assert seen[0]["messages"] == [{"role": "user", "content": web.LLM_TEST_PROMPT}]  # 只发固定的合成提示

    env.ctx.llm_transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": {"message": "bad"}}))
    r = client.post("/api/v2/settings/llm/test", json={}, headers=F.POST_H)
    assert r.json()["ok"] is False and "401" in r.json()["error"] and key not in r.text


def test_llm_test_without_config(client):
    r = client.post("/api/v2/settings/llm/test", json={}, headers=F.POST_H).json()
    assert r["ok"] is False and r["model"] is None and "没有配置" in r["error"]


def test_windows_and_storage(client, env):
    assert client.get("/api/v2/windows").json() == {"windows": [{"hwnd": 11, "process": "tk.exe",
                                                                  "title": "Synthetic window"}]}
    s = client.get("/api/v2/storage").json()
    assert keys(s) == {"data_dir", "db_bytes", "frames_bytes", "vectors_bytes", "frames", "lines", "oldest_ms"}
    assert s["db_bytes"] > 0 and s["frames_bytes"] > 0 and s["vectors_bytes"] == 0
    assert (s["frames"], s["lines"], s["oldest_ms"]) == (1, 4, F.day_base())


# ---------------- 隐私删除 ----------------

def _raw_db_bytes(data_dir) -> bytes:
    p = schema.db_path(data_dir)
    out = p.read_bytes()
    wal = p.with_name(p.name + "-wal")
    if wal.exists():
        out += wal.read_bytes()
    return out


def test_delete_recent_really_removes_sentinel(client, env):
    w = env.stack.writer
    t = utc_ms() - 60_000
    sid = w.segment_for("tk.exe", "sentinel doc", t).result(5)
    b = w.block_new(sid, t).result(5)
    ids = w.record_lines(b, t, (0, 100), [LineIn(F.SENTINEL, 0, 10, 300, 24)]).result(5)
    fid = w.save_frame(F.synthetic_frame(seed=7), captured_ms=t, segment_id=sid, block_id=b, pos_y=0, app="tk.exe",
                       title="sentinel doc", line_ids=ids, line_rects=[(0, 10, 300, 24)])
    w.flush()
    vs = VectorStore(text_vectors_dir(env.data), DIM)
    vs.append(ids, np.eye(DIM, dtype=np.float32)[:1], [t], [sid])
    frame_file = env.data / env.stack.queries.frame(fid)["rel_path"]
    assert frame_file.exists()
    assert client.get("/api/v2/search", params={"q": F.SENTINEL}).json()["total_groups"] == 1

    r = client.post("/api/v2/delete", json={"recent_minutes": 5}, headers=F.POST_H)
    assert r.status_code == 200
    d = r.json()["deleted"]
    assert d["lines"] == 1 and d["frames"] == 1 and d["text_vectors"] == 1 and d["vectors_pending"] == 0
    assert {"lines", "frames", "segments"} <= set(d)
    assert client.get("/api/v2/search", params={"q": F.SENTINEL}).json()["groups"] == []
    assert not frame_file.exists()
    assert client.get(f"/api/v2/frames/{fid}").status_code == 404
    assert F.SENTINEL.encode() not in _raw_db_bytes(env.data)
    vs2 = VectorStore(text_vectors_dir(env.data), DIM, readonly=True)
    assert len(vs2) == 0
    # 今天 10:00 的旧内容不受影响
    assert client.get("/api/v2/search", params={"q": "report"}).json()["total_groups"] == 1


def test_delete_removes_paragraph_vectors_built_from_deleted_lines(tmp_path, monkeypatch):
    """文字向量按段落算（id = 段落第一行，或重组后的负数编号）：删掉段落里不是第一行的那一行，
    段落向量也要删掉（它是用被删的字算的），语义检索不能再用它把剩下的行排上来。"""
    e = np.eye(DIM, dtype=np.float32)
    env = F.build_env(tmp_path, monkeypatch, fill_data=False)
    try:
        w = env.stack.writer
        t = utc_ms() - 3 * 3600_000
        sid = w.segment_for("tk.exe", "para", t).result(5)
        b = w.block_new(sid, t).result(5)
        ids = []
        for i, text in enumerate(["first line", "secret middle", "last line"]):
            ids += w.record_lines(b, t + i * 600_000, (0, 1000), [LineIn(text, 0, 10 + i * 30, 300, 24 + i * 30)]
                                  ).result(5)
        neg = -7  # 重组过的段落
        w.call(lambda c: c.executemany("INSERT INTO text_chunks (line_id, chunk_id) VALUES (?, ?)",
                                       [(ids[0], ids[0]), (ids[1], ids[0])])).result(5)
        w.call(lambda c: c.execute("INSERT INTO text_chunks (line_id, chunk_id) VALUES (?, ?)", (ids[2], neg))).result(5)
        vs = VectorStore(text_vectors_dir(env.data), DIM)
        vs.append([ids[0], neg], e[:2], [t, t + 1_200_000], [sid, sid])
        vs.close()
        env.stack.search = SearchService(env.data, encoder=FakeEncoder(e[0]), model_check=lambda: True)
        with TestClient(env.guard, base_url=F.BASE, follow_redirects=False) as c:
            F.login(c, env)
            r = c.get("/api/v2/search", params={"q": "zzzz"}).json()
            assert sorted(h["line_id"] for g in r["groups"] for h in g["hits"]) == ids[:2]  # 段落向量命中
            # 只删中间那一行（时间范围只盖住它）
            d = c.post("/api/v2/delete", json={"from_ms": t + 500_000, "to_ms": t + 700_000}, headers=F.POST_H).json()
            assert d["deleted"]["lines"] == 1 and d["deleted"]["text_vectors"] == 1
            assert c.get("/api/v2/search", params={"q": "zzzz"}).json()["groups"] == []
        vs2 = VectorStore(text_vectors_dir(env.data), DIM, readonly=True)
        assert len(vs2) == 1  # 只剩没碰到的那个段落
        vs2.close()
        assert not env.stack.deleter.pending_path.exists()
        env.stack.search.close()
    finally:
        env.stack.close()


def test_delete_bumps_revision_and_retries_busy_checkpoint(client, env, monkeypatch):
    from screenrecall.store import delete as store_delete

    conn = env.stack.queries.conn()
    rev = lambda: int(conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])  # noqa: E731
    before = rev()
    real = store_delete.checkpoint
    monkeypatch.setattr(store_delete, "checkpoint", lambda c, tries=5: True)  # 有读者占着
    r = client.post("/api/v2/delete", json={"recent_minutes": 5}, headers=F.POST_H).json()
    assert r["deleted"]["checkpoint_busy"] is True
    assert rev() > before  # 开着的"今天"页面据此刷新
    dl = env.stack.deleter
    assert json.loads(dl.pending_path.read_text(encoding="utf-8"))["checkpoint"] is True
    dl.retry()
    assert dl.pending_path.exists()  # 还是忙
    monkeypatch.setattr(store_delete, "checkpoint", real)
    dl.retry()
    assert not dl.pending_path.exists()


def test_delete_by_app_and_range(client, env):
    base = F.day_base()
    r = client.post("/api/v2/delete", json={"from_ms": base + 4_000, "to_ms": base + 6_000}, headers=F.POST_H)
    assert r.json()["deleted"]["lines"] == 1  # 只有第二块那一行
    assert client.get("/api/v2/search", params={"q": "another"}).json()["groups"] == []
    r = client.post("/api/v2/delete", json={"app": F.APP}, headers=F.POST_H)
    assert r.json()["deleted"]["lines"] == 3 and r.json()["deleted"]["frames"] == 1
    assert client.get("/api/v2/search", params={"q": "report"}).json()["groups"] == []
    assert client.get("/api/v2/day").json()["overview"]["apps"] == []
    for bad in [{}, {"recent_minutes": 0}, {"recent_minutes": 5, "app": "x"}, {"from_ms": 1},
                {"from_ms": 5, "to_ms": 1}, {"nope": 1}, {"app": ""}]:
        assert client.post("/api/v2/delete", json=bad, headers=F.POST_H).status_code == 400, bad


# ---------------- 令牌 ----------------

def test_reset_token_reissues_cookie(client, env):
    old = client.cookies.get(auth.cookie_name(F.PORT))
    r = client.post("/api/v2/agent/reset-token", json={}, headers=F.POST_H)
    assert r.json() == {"ok": True} and "HttpOnly" in r.headers["set-cookie"]
    assert client.get("/api/v2/status").status_code == 200  # 这个浏览器拿到了新 Cookie
    assert client.cookies.get(auth.cookie_name(F.PORT)) != old
    with TestClient(env.guard, base_url=F.BASE) as other:
        other.cookies.set(auth.cookie_name(F.PORT), old)
        assert other.get("/api/v2/status").status_code == 401  # 旧 Cookie 失效
        assert other.get("/api/v2/status", headers={"Authorization": f"Bearer {F.TOKEN}"}).status_code == 401


def test_legacy_notice_parameter(tmp_path):
    from screenrecall import webapi
    from screenrecall.config import load_config

    app = webapi.create_app(load_config(tmp_path), port=18734, notice="自定义说明")
    with TestClient(app, base_url="http://127.0.0.1:18734") as c:
        assert c.get("/api/meta").json()["notice"] == "自定义说明"
    app = webapi.create_app(load_config(tmp_path), port=18734)
    with TestClient(app, base_url="http://127.0.0.1:18734") as c:
        assert c.get("/api/meta").json()["notice"] == webapi.NO_AUTH_NOTICE


def test_legacy_dist_is_served_at_root_without_login(tmp_path, env):
    from screenrecall import webapi
    from screenrecall.app import server
    from screenrecall.config import load_config

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>v2 ui</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    legacy = webapi.create_app(load_config(tmp_path / "legacy"), port=F.PORT, dist=dist)
    guard = server.build_app(port=F.PORT, token=F.TOKEN, nonces=env.nonces, legacy=legacy,
                             extra_routes=web.routes(env.ctx))
    with TestClient(guard, base_url=F.BASE) as c:
        assert "v2 ui" in c.get("/").text and c.get("/assets/app.js").status_code == 200
        assert c.get("/api/v2/status").status_code == 401
        assert c.get("/api/meta").status_code == 401
