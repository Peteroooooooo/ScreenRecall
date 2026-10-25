"""网页 API（screenrecall.webapi）：用假 winapi + 真实 SQLite/WebP 驱动，覆盖 app.py 页面测试里的关键行为。
不加载任何真实模型：语义 / 画面模式用 test_retrieval 的替身编码器，经 create_app(loader=...) 注入。"""
import ast
import io
import logging
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from screenrecall import control, db, i18n, lifecycle, retrieval, storage, webapi  # noqa: E402
from screenrecall.config import load_config  # noqa: E402
from screenrecall.ocr import OcrBox, OcrWorker  # noqa: E402

from .conftest import SCREEN, make_image  # noqa: E402
from .test_retrieval import FakeImage, Pipeline, fake_clip_text  # noqa: E402
from .test_retry import _setup as _failed_ocr_setup  # noqa: E402

A, B, C = make_image(1), make_image(2), make_image(3)
W = {"X-ScreenRecall": "1"}
EN = {"X-Lang": "en"}
BASE = "http://127.0.0.1:8600"


def _ocr_all(h, texts):
    class E:
        version = "fake"
        current: list[str] = []

        def recognize(self, image):
            return [OcrBox(t, 0.9, [[0, 12 * i], [30, 12 * i], [30, 12 * i + 10], [0, 12 * i + 10]])
                    for i, t in enumerate(self.current)]
    eng = E()
    w = OcrWorker(h.cfg, eng, threading.Event(), threading.Event(), logging.getLogger("t"))
    while (job := w._claim(h.conn)) is not None:
        eng.current = texts[job.frame_id]
        w._process(h.conn, job)


def _client(cfg, tmp_path, **kw) -> TestClient:
    return TestClient(webapi.create_app(cfg, port=8600, dist=tmp_path / "no-dist", **kw), base_url=BASE)


def _seed(h) -> list[str]:
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    for img in (B, C):
        h.show(img)
        h.tick()
        h.tick()
    fids = [f["frame_id"] for f in h.frames()]
    _ocr_all(h, dict(zip(fids, [["alpha invoice"], ["beta invoice"], ["gamma"]])))
    return fids


@pytest.fixture
def api(h, tmp_path):
    fids = _seed(h)
    return _client(h.cfg, tmp_path), fids


def _search(c, q, **kw):
    r = c.post("/api/search", json={"q": q} | kw, headers=W)
    assert r.status_code == 200, r.text
    return r.json()


def test_quoted_search_returns_results_with_lexical_boxes(api):
    c, (fa, fb, fc) = api
    res = _search(c, '"invoice"')
    assert [r["frame_id"] for r in res["results"]] == [fb, fa]  # 时间倒序
    assert res["rank_basis"] == "time" and res["total"] == 2
    box = res["results"][0]["boxes"][0]
    assert box["kind"] == "lex" and len(box["quad"]) == 4
    assert res["results"][0]["image_ok"]


def test_relax_only_when_requested(api):
    c, _ = api
    res = _search(c, '"INVOICE BETA"')
    assert res["results"] == [] and res["can_relax"] and not res["relaxed"]
    assert "没有画面同时包含全部引号原串" in res["message"]
    relaxed = _search(c, '"INVOICE BETA"', relaxed=True)
    assert relaxed["relaxed"] and relaxed["notes"][0] == relaxed["relax_rule"]


def test_detail_has_context_highlights_and_gap_text(api):
    c, (fa, fb, fc) = api
    r = c.post(f"/api/frames/{fb}/detail", json={"q": '"invoice"'}, headers=W).json()
    assert r["lex_lines"] == [0] and r["lines"][0]["text"] == "beta invoice"
    ctx = r["context"]
    assert ctx["index"] == 1 and ctx["total"] == 3
    assert ctx["prev"]["frame_id"] == fa and ctx["next"]["frame_id"] == fc
    assert "画面有变化但未保存" in ctx["gaps_before"] and "画面有变化但未保存" in ctx["gaps_after"]
    assert [s["frame_id"] for s in ctx["strip"]] == [fa, fb, fc]
    assert ctx["session"]["end_label"] == "仍在录制"


def test_images_and_thumbnails_are_served_by_frame_id_only(api):
    c, (fa, _, _) = api
    img = c.get(f"/api/frames/{fa}/image")
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    th = c.get(f"/api/frames/{fa}/thumb?w=200")
    assert th.status_code == 200 and Image.open(io.BytesIO(th.content)).format == "WEBP"
    assert c.get("/api/frames/..%2F..%2Fsecret/image").status_code == 404


def test_favorite_then_delete_frame_requires_current_favorites(api, h):
    c, (fa, fb, _) = api
    occ = c.post(f"/api/frames/{fb}/detail", json={}, headers=W).json()["occurrence"]["occurrence_id"]
    assert c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W).json()["favorited"]
    fav = c.get("/api/favorites").json()["favorites"]
    assert [f["occurrence_id"] for f in fav] == [occ] and fav[0]["width"] > 0 and fav[0]["image_ok"]
    plan = c.get(f"/api/frames/{fb}/delete-plan").json()
    assert plan["favorites"] == [occ] and plan["occurrences"][0]["favorited"]
    stale = c.post(f"/api/frames/{fb}/delete", json={"confirmed_favorites": []}, headers=W)
    assert stale.status_code == 409 and stale.json()["plan"]["favorites"] == [occ]
    ok = c.post(f"/api/frames/{fb}/delete", json={"confirmed_favorites": [occ]}, headers=W).json()
    assert "已删除这幅画面的 1 次出现记录和 1 个收藏" in ok["message"]
    assert c.post(f"/api/frames/{fb}/detail", json={}, headers=W).status_code == 404


def test_delete_single_occurrence(api):
    c, (fa, _, _) = api
    occ = c.post(f"/api/frames/{fa}/detail", json={}, headers=W).json()["occurrence"]["occurrence_id"]
    plan = c.get(f"/api/occurrences/{occ}/delete-plan").json()
    assert plan["other_occurrences"] == 0 and not plan["favorited"]
    res = c.post(f"/api/occurrences/{occ}/delete", json={}, headers=W).json()
    assert "画面内容已一并删除" in res["message"]


def test_range_preview_then_delete(api):
    c, fids = api
    body = {"date_from": "2023-01-01", "date_to": "2100-01-01", "time_from": "00:00", "time_to": "23:59"}
    plan = c.post("/api/range/preview", json=body, headers=W).json()["plan"]
    assert plan["occurrences"] == 3 and plan["frames_purged"] == 3
    res = c.post("/api/range/delete", json=body, headers=W).json()
    assert "已按范围删除 3 次出现记录、3 幅画面" in res["message"]
    bad = c.post("/api/range/preview", json=body | {"date_to": "2022-01-01"}, headers=W)
    assert bad.status_code == 400 and "结束时间必须晚于开始时间" in bad.json()["error"]
    assert lifecycle.plan_range_delete  # 计划 / 确认都走 lifecycle


def test_status_and_windows_and_control(api, h):
    c, _ = api
    st = c.get("/api/status").json()
    assert st["index"]["frames"] == 3 and st["control"]["actual_state"] == "recording"
    wins = c.get("/api/windows").json()["windows"]
    assert wins and wins[0]["hwnd"] == SCREEN.info().hwnd
    r = c.post("/api/control", json={"action": "bogus"}, headers=W)
    assert r.status_code in (400, 409)


def test_import_marks_time_unknown(api):
    c, _ = api
    buf = io.BytesIO()
    Image.fromarray(make_image(9, 64, 48)[:, :, ::-1]).save(buf, "PNG")
    r = c.post("/api/import", files=[("files", ("shot.png", buf.getvalue(), "image/png"))],
               data={"label": "批次"}, headers=W)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] == 1 and j["batch"]["time_known"] is False


def test_english_mode_translates_system_messages_only(api):
    c, (fa, fb, _) = api
    res = c.post("/api/search", json={"q": '"INVOICE BETA"'}, headers=W | {"X-Lang": "en"}).json()
    assert res["message"] == "No screen contains all of the quoted strings." and res["relax_rule"].startswith("Relaxed match")
    r = c.post(f"/api/frames/{fb}/detail", json={"q": '"invoice"'}, headers=W | {"X-Lang": "en"}).json()
    assert r["lines"][0]["text"] == "beta invoice"  # OCR 文字不翻译
    assert r["context"]["gaps_before"].startswith("known gaps in between: screen changed but not saved")
    assert r["context"]["session"]["end_label"] == "still recording"
    meta = c.get("/api/meta", headers={"X-Lang": "en"}).json()
    assert meta["notice"].startswith("This page is local-only") and meta["rules"][0].startswith("Allowed:")
    assert c.get("/api/meta").json()["notice"].startswith("页面只在本机访问")  # 不带头时仍是中文
    bad = c.post("/api/range/preview", json={"date_from": "2023-01-02", "date_to": "2023-01-01"}, headers=W | {"X-Lang": "en"})
    assert bad.json()["error"] == "End time must be later than start time."


def test_writes_need_custom_header_and_same_origin(api):
    c, (fa, _, _) = api
    assert c.post("/api/search", json={"q": '"invoice"'}).status_code == 403
    evil = c.post("/api/search", json={"q": '"invoice"'}, headers=W | {"Origin": "http://evil.example"})
    assert evil.status_code == 403
    assert c.get("/api/meta", headers={"Host": "evil.example"}).status_code == 403
    assert c.get("/api/meta").json()["db_ok"]
    en = c.post("/api/search", json={"q": '"invoice"'}, headers=EN)
    assert en.status_code == 403 and en.json()["error"] == "Missing X-ScreenRecall header"


# ---------------- 录制控制 ----------------

NOW = 1_800_000_000_000


def _daemon(h, monkeypatch, *, alive=True, recording=True):
    """固定"现在"，按需写心跳：alive=False 时心跳停在很久以前（后台没在跑）。"""
    monkeypatch.setattr(control, "utc_ms", lambda: NOW)
    h.conn.execute("UPDATE control SET heartbeat_ms = ?, actual_state = ? WHERE id = 1",
                   (NOW if alive else 0, "recording" if recording else "paused"))


def _ctl(c, action, lang=None, **kw):
    return c.post("/api/control", json={"action": action} | kw, headers=W | (EN if lang == "en" else {}))


def test_control_actions_write_requests_when_daemon_alive(api, h, monkeypatch):
    c, _ = api
    _daemon(h, monkeypatch)
    seq0, snap0 = h.ctrl()["request_seq"], h.ctrl()["snapshot_seq"]
    r = _ctl(c, "start", hwnd=SCREEN.hwnd)
    assert r.status_code == 200 and r.json()["message"] == "请求已发送，等待后台确认"
    ctrl = h.ctrl()
    assert ctrl["request_seq"] == seq0 + 1 and ctrl["desired_state"] == "recording"
    assert ctrl["target_hwnd"] == SCREEN.hwnd and ctrl["target_process"] == SCREEN.process
    assert _ctl(c, "debug").status_code == 200 and h.ctrl()["desired_mode"] == "debug"
    assert h.ctrl()["target_hwnd"] == SCREEN.hwnd  # 切模式沿用当前窗口
    snap = _ctl(c, "snapshot", lang="en")
    assert snap.json()["message"] == "Snapshot request sent" and h.ctrl()["snapshot_seq"] == snap0 + 1
    assert _ctl(c, "pause").status_code == 200 and h.ctrl()["desired_state"] == "paused"
    assert h.ctrl()["request_seq"] == seq0 + 3


def test_control_start_validates_window(api, h, monkeypatch):
    c, _ = api
    _daemon(h, monkeypatch)
    r = _ctl(c, "start")
    assert r.status_code == 400 and r.json()["error"] == "请先选择要录制的窗口"
    r = _ctl(c, "start", hwnd=SCREEN.hwnd + 1)
    assert r.status_code == 409 and "已经关闭或不可录制" in r.json()["error"]
    SCREEN.process = "KeePass.exe"
    r = _ctl(c, "start", lang="en", hwnd=SCREEN.hwnd)
    assert r.status_code == 409 and r.json()["error"] == "Program KeePass.exe is on the exclusion list; cannot record."
    r = _ctl(c, "bogus")
    assert r.status_code == 400 and r.json()["error"] == "未知操作：bogus"


def test_control_refused_when_daemon_down_or_not_recording(api, h, monkeypatch):
    c, _ = api
    _daemon(h, monkeypatch, alive=False)
    seq0 = h.ctrl()["request_seq"]
    for action in ("start", "pause"):
        r = _ctl(c, action, hwnd=SCREEN.hwnd)
        assert r.status_code == 409 and r.json()["error"] == "后台未运行，请求没有发送。"
    assert _ctl(c, "pause", lang="en").json()["error"] == "The daemon is not running; request not sent."
    assert _ctl(c, "debug").json()["error"] == "未在录制，模式没有切换"
    assert _ctl(c, "snapshot").status_code == 409
    _daemon(h, monkeypatch, alive=True, recording=False)  # 后台在跑但已暂停
    r = _ctl(c, "snapshot", lang="en")
    assert r.status_code == 409 and r.json()["error"] == "Not recording; snapshot not sent."
    assert h.ctrl()["request_seq"] == seq0  # 一个请求都没写


def test_window_api_failure_is_a_readable_error(api, h, monkeypatch):
    c, _ = api
    _daemon(h, monkeypatch)

    def boom():
        raise OSError("EnumWindows failed")
    monkeypatch.setattr(sys.modules["screenrecall.winapi"], "list_windows", boom)
    r = _ctl(c, "start", hwnd=SCREEN.hwnd)
    assert r.status_code == 503 and r.json() == {"error": "无法列出窗口：EnumWindows failed"}
    assert _ctl(c, "start", lang="en", hwnd=SCREEN.hwnd).json()["error"] == "Cannot list windows: EnumWindows failed"
    wins = c.get("/api/windows", headers=EN).json()
    assert wins["windows"] == [] and wins["problem"] == "Cannot list windows: EnumWindows failed"


# ---------------- 失败待办 ----------------

def test_failed_jobs_list_and_retry(h, tmp_path):
    _, _, fids = _failed_ocr_setup(h)  # 两条 OCR 失败待办
    h.conn.execute("UPDATE frames SET image_status = 'missing' WHERE frame_id = ?", (fids[0],))
    c = _client(h.cfg, tmp_path)
    j = c.get("/api/failed").json()
    assert j["summary"]["ocr"] == {"name": "文字识别（OCR）", "retryable": 1, "not_retryable": 1,
                                   "errors": [["ocr_error", 2]]}
    assert {x["frame_id"] for x in j["jobs"]} == set(fids) and {x["stage_name"] for x in j["jobs"]} == {"文字识别（OCR）"}
    en = c.get("/api/failed", headers=EN).json()
    assert en["summary"]["ocr"]["name"] == "Text recognition (OCR)"
    assert {x["why_not"] for x in en["jobs"]} == {"", "image file missing"}
    r = c.post("/api/failed/retry", json={}, headers=W).json()
    assert r["requeued"] == 1 and r["message"] == "已改回待处理 1 条，后台会自动重新处理。未改：原图文件缺失 1。"
    again = c.post("/api/failed/retry", json={}, headers=W | EN).json()
    assert again["requeued"] == 0
    assert again["message"] == "Re-queued 0 job(s); the daemon will process them again. Unchanged: image file missing 1."


# ---------------- 数据库不可用 ----------------

def test_missing_database(tmp_path):
    cfg = load_config(tmp_path / "empty")
    c = _client(cfg, tmp_path)
    meta = c.get("/api/meta").json()
    assert not meta["db_ok"] and meta["db_problem"].startswith("还没有找到数据库")
    for r in (c.get("/api/status"), c.post("/api/search", json={"q": '"x"'}, headers=W), c.get("/api/favorites")):
        assert r.status_code == 503 and r.json()["error"].startswith("还没有找到数据库")
    assert c.get("/api/status", headers=EN).json()["error"].startswith("Database not found:")
    assert not cfg.db_path.exists()  # 只读接口不会凭空建库


def test_old_or_newer_schema(h, tmp_path):
    _seed(h)
    c = _client(h.cfg, tmp_path)
    h.conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    meta = c.get("/api/meta").json()
    assert not meta["db_ok"] and meta["db_problem"] == "数据库结构是 v5，代码需要 v6。请重启后台完成升级。"
    for r in (c.get("/api/status"), c.post("/api/search", json={"q": '"invoice"'}, headers=W),
              c.post("/api/range/preview", json={"date_from": "2023-01-01"}, headers=W)):
        assert r.status_code == 503 and r.json()["error"] == meta["db_problem"]
    assert c.get("/api/status", headers=EN).json()["error"] == (
        "Database schema is v5 but the code needs v6. Restart the daemon to upgrade.")
    h.conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    r = c.get("/api/status", headers=EN)
    assert r.status_code == 503 and r.json()["error"].startswith("Database schema v99 is newer than the v6")


def test_empty_or_corrupt_database_file(tmp_path):
    cfg = load_config(tmp_path / "d")
    cfg.data_dir.mkdir(parents=True)
    raw = sqlite3.connect(cfg.db_path)
    raw.execute("CREATE TABLE other (x)")
    raw.close()
    c = _client(cfg, tmp_path)
    assert c.get("/api/meta").json()["db_problem"].startswith("数据库结构是 v0")
    assert c.get("/api/status").status_code == 503
    cfg.db_path.write_bytes(b"this is not a sqlite database" * 200)
    r = c.get("/api/status")
    assert r.status_code == 500 and r.json()["error"].startswith("数据库读写出错：file is not a database")
    en = c.get("/api/meta", headers=EN)
    assert en.status_code == 500 and en.json()["error"].startswith("Database error: file is not a database")


def test_locked_database_is_503(api, h, monkeypatch):
    c, (fa, _, _) = api
    real = db.connect

    def quick(path):  # 等锁 50 ms 而不是 5 秒
        conn = real(path)
        conn.execute("PRAGMA busy_timeout = 50")
        return conn
    monkeypatch.setattr(db, "connect", quick)
    occ = c.post(f"/api/frames/{fa}/detail", json={}, headers=W).json()["occurrence"]["occurrence_id"]
    h.conn.execute("BEGIN IMMEDIATE")  # 模拟后台正在写
    try:
        r = c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W)
        assert r.status_code == 503 and r.json() == {"error": "数据库正忙（后台正在写入），请稍后重试"}
        en = c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W | EN)
        assert en.json()["error"] == "Database busy (the daemon is writing); try again shortly"
    finally:
        h.conn.execute("ROLLBACK")
    assert c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W).status_code == 200


def test_other_database_errors_and_unexpected_exceptions_are_json_500(api, monkeypatch, caplog):
    c, _ = api

    def disk_error(conn):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(db, "index_stats", disk_error)
    r = c.get("/api/status")
    assert r.status_code == 500 and r.json() == {
        "error": "数据库读写出错：disk I/O error。请确认后台已运行并完成升级，详情见网页服务的日志。"}
    assert c.get("/api/status", headers=EN).json()["error"].startswith("Database error: disk I/O error.")

    def bug(conn):
        raise KeyError("secret-internal-detail")
    monkeypatch.setattr(db, "index_stats", bug)
    with caplog.at_level(logging.ERROR, logger="screenrecall.webapi"):
        r = c.get("/api/status")
    assert r.status_code == 500 and r.json() == {"error": "服务器内部错误（KeyError），详情见网页服务的日志。"}
    assert "secret-internal-detail" not in r.text and "Traceback" not in r.text  # 不把堆栈发给前端
    rec = [x for x in caplog.records if x.name == "screenrecall.webapi" and x.exc_info]
    assert rec and "/api/status" in rec[-1].getMessage()  # 服务端日志里有完整堆栈
    assert c.get("/api/status", headers=EN).json()["error"] == (
        "Internal server error (KeyError); see the web server log for details.")


# ---------------- 参数错误 ----------------

def test_bad_parameters_are_400_json(api):
    c, (fa, _, _) = api
    for limit in ("abc", [1], 15):
        r = c.post("/api/search", json={"q": '"invoice"', "limit": limit}, headers=W)
        assert r.status_code == 400 and r.json()["error"] == "数量只能是 10、20 或 50"
    assert c.post("/api/search", json={"q": "x", "limit": "x"}, headers=W | EN).json()["error"] == "Count must be 10, 20 or 50"
    assert c.post("/api/search", json={"q": "x", "mode": "video"}, headers=W | EN).json()["error"] == "mode must be text or image"
    r = c.post("/api/search", json={"q": '"invoice"', "apps": "notepad.exe"}, headers=W | EN)
    assert r.status_code == 400 and r.json()["error"] == "Parameter must be a list"
    r = c.post(f"/api/frames/{fa}/detail", json={"semantic_lines": 3}, headers=W)
    assert r.status_code == 400 and r.json()["error"] == "semantic_lines 应为整数列表"
    assert c.post(f"/api/frames/{fa}/detail", json={"semantic_lines": "0"}, headers=W | EN).json()["error"] == (
        "semantic_lines must be a list of integers")
    ok = c.post(f"/api/frames/{fa}/detail", json={"semantic_lines": [0, 0, True, 99, "1"]}, headers=W).json()
    assert ok["sem_lines"] == [0]
    r = c.post("/api/search", json=[1, 2], headers=W | EN)
    assert r.status_code == 400 and r.json()["error"] == "Request body must be a JSON object"
    r = c.post("/api/search", content=b"{not json", headers=W | {"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "请求体不是合法的 UTF-8 JSON"
    r = c.post("/api/range/preview", json={"date_from": 20230101}, headers=W)
    assert r.status_code == 400 and r.json()["error"].startswith("时间不合法")
    r = c.get(f"/api/frames/{fa}/thumb?w=wide", headers=EN)
    assert r.status_code == 400 and r.json() == {"error": "Invalid parameter"}
    assert c.get("/api/frames/not-an-id/image").json() == {"error": "画面编号不合法"}


def test_date_out_of_range_is_400(api, monkeypatch):
    c, _ = api

    def overflow(a, b):
        raise OverflowError("timestamp out of range for platform time_t")
    monkeypatch.setattr(webapi.timeutil, "local_dates_to_utc_range", overflow)
    r = c.post("/api/search", json={"q": '"invoice"', "date_from": "0001-01-01"}, headers=W | EN)
    assert r.status_code == 400 and r.json()["error"] == "Date out of range: 0001-01-01 – 0001-01-01"


def test_unknown_route_and_method_are_json(api):
    c, _ = api
    r = c.get("/api/nope", headers=EN)
    assert r.status_code == 404 and r.json() == {"error": "No such endpoint"}
    r = c.get("/api/search")
    assert r.status_code == 405 and r.json() == {"error": "不支持这种请求方法"}


def test_every_literal_api_error_message_has_english():
    """webapi.py 里写死的 ApiError 提示都要在 i18n 里有英文（带参数的在各自测试里核对）。"""
    tree = ast.parse(Path(webapi.__file__).read_text(encoding="utf-8"))
    msgs = [n.args[1].value for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ApiError"
            and len(n.args) > 1 and isinstance(n.args[1], ast.Constant)]
    assert len(msgs) > 20
    assert [m for m in msgs if i18n.to_en(m) == m] == []


# ---------------- 搜索筛选、原图状态 ----------------

def test_date_and_app_filters(api):
    c, (fa, fb, _) = api
    app = c.get("/api/meta").json()["apps"][0]
    inside = _search(c, '"invoice"', date_from="2024-12-31", date_to="2023-01-01", apps=[app])  # 顺序颠倒也可以
    assert [r["frame_id"] for r in inside["results"]] == [fb, fa]
    out = _search(c, '"invoice"', date_from="2000-01-01", date_to="2000-01-02")
    assert out["results"] == [] and out["total"] == 0 and out["message"]
    assert _search(c, '"invoice"', apps=["chrome.exe"])["results"] == []
    r = c.post("/api/search", json={"q": '"invoice"', "date_from": "2023-13-01"}, headers=W | EN)
    assert r.status_code == 400 and r.json()["error"] == "Invalid date: 2023-13-01 (expected YYYY-MM-DD)"


def test_expired_or_missing_image_is_404(api, h):
    c, (fa, fb, _) = api
    h.conn.execute("UPDATE frames SET image_status = 'expired' WHERE frame_id = ?", (fa,))
    for kind in ("image", "thumb"):
        r = c.get(f"/api/frames/{fa}/{kind}")
        assert r.status_code == 404 and r.json() == {"error": "原图已不可用"}
    assert c.get(f"/api/frames/{fa}/image", headers=EN).json() == {"error": "Image unavailable"}
    res = {r["frame_id"]: r for r in _search(c, '"invoice"')["results"]}
    assert not res[fa]["image_ok"] and res[fb]["image_ok"]
    assert not c.post(f"/api/frames/{fa}/detail", json={}, headers=W).json()["image_ok"]
    row = h.conn.execute("SELECT image_path FROM frames WHERE frame_id = ?", (fb,)).fetchone()
    storage.resolve(h.cfg.data_dir, row[0]).unlink()
    r = c.get(f"/api/frames/{fb}/thumb")
    assert r.status_code == 404 and r.json() == {"error": "原图文件缺失"}


# ---------------- 收藏、删除 ----------------

def _occ(c, fid):
    return c.post(f"/api/frames/{fid}/detail", json={}, headers=W).json()["occurrence"]["occurrence_id"]


def test_unfavorite_and_missing_record(api):
    c, (fa, _, _) = api
    occ = _occ(c, fa)
    assert c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W).json()["favorited"]
    off = c.post(f"/api/occurrences/{occ}/favorite", json={"on": False}, headers=W | EN).json()
    assert off == {"favorited": False, "message": "Removed from favorites"}
    assert c.get("/api/favorites").json()["favorites"] == []
    assert not c.post(f"/api/frames/{fa}/detail", json={}, headers=W).json()["occurrence"]["favorited"]
    r = c.post("/api/occurrences/nope/favorite", json={"on": True}, headers=W)
    assert r.status_code == 404 and r.json()["error"] == "这条记录已不存在，无法收藏。"


def test_deleting_twice_is_404_not_zero_count_success(api):
    c, (fa, fb, _) = api
    occ = _occ(c, fa)
    assert c.post(f"/api/occurrences/{occ}/delete", json={}, headers=W).status_code == 200
    r = c.post(f"/api/occurrences/{occ}/delete", json={}, headers=W)
    assert r.status_code == 404 and r.json()["error"] == "这条记录已不存在。"
    assert c.post(f"/api/frames/{fb}/delete", json={}, headers=W).status_code == 200
    r = c.post(f"/api/frames/{fb}/delete", json={}, headers=W | EN)
    assert r.status_code == 404 and r.json()["error"] == "This screen has been deleted."
    assert c.get(f"/api/frames/{fb}/delete-plan").status_code == 404


RANGE = {"date_from": "2023-01-01", "date_to": "2100-01-01", "time_from": "00:00", "time_to": "23:59"}


def test_range_delete_keeps_favorites_by_default(api):
    c, (fa, fb, fc) = api
    occ = _occ(c, fb)
    c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W)
    plan = c.post("/api/range/preview", json=RANGE, headers=W).json()["plan"]
    assert plan["occurrences"] == 2 and plan["frames_purged"] == 2 and plan["favorites"] == [occ]
    res = c.post("/api/range/delete", json=RANGE, headers=W).json()
    assert res["message"] == "已按范围删除 2 次出现记录、2 幅画面。"
    assert [f["occurrence_id"] for f in c.get("/api/favorites").json()["favorites"]] == [occ]
    assert c.post(f"/api/frames/{fb}/detail", json={}, headers=W).status_code == 200


def test_range_delete_with_favorites_needs_current_confirmation(api, h):
    c, (fa, fb, fc) = api
    occ = _occ(c, fb)
    c.post(f"/api/occurrences/{occ}/favorite", json={"on": True}, headers=W)
    body = RANGE | {"include_favorites": True}
    plan = c.post("/api/range/preview", json=body, headers=W).json()["plan"]
    assert plan["occurrences"] == 3 and plan["frames_purged"] == 3 and plan["favorites"] == [occ]
    stale = c.post("/api/range/delete", json=body | {"confirmed_favorites": []}, headers=W)
    assert stale.status_code == 409 and stale.json()["error"] == "确认后范围内又有新的收藏，请重新确认删除范围。请重新预览。"
    en = c.post("/api/range/delete", json=body, headers=W | EN).json()["error"]
    assert en == "New favorites appeared in the range after you confirmed; review the deletion again. Preview again."
    assert h.conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0] == 3  # 什么都没删
    ok = c.post("/api/range/delete", json=body | {"confirmed_favorites": [occ]}, headers=W | EN).json()
    assert ok["message"] == "Deleted 3 occurrence(s) and 3 screen(s) in range, plus 1 favorite(s)."
    assert c.get("/api/favorites").json()["favorites"] == []


# ---------------- 导入 ----------------

def _png(seed, w=64, h=48) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(make_image(seed, w, h)[:, :, ::-1]).save(buf, "PNG")
    return buf.getvalue()


def test_import_with_known_time_then_resubmit_same_batch(api, h):
    c, _ = api
    files = [("files", ("b.png", _png(11), "image/png")), ("files", ("a.png", _png(12), "image/png"))]
    form = {"label": "批次", "captured_date": "2024-03-05", "captured_time": "14:30"}
    j = c.post("/api/import", files=files, data=form, headers=W).json()
    assert j["ok"] == 2 and j["batch"]["time_known"] and not j["batch"]["resubmitted"]
    assert [i["name"] for i in j["batch"]["items"]] == ["a.png", "b.png"]  # 按文件名顺序
    want = int(datetime(2024, 3, 5, 14, 30).timestamp() * 1000)
    got = {r[0] for r in h.conn.execute(
        "SELECT captured_at_ms FROM frames WHERE frame_id IN (?, ?)", [i["frame_id"] for i in j["batch"]["items"]])}
    assert got == {want}
    again = c.post("/api/import", files=files, data=form, headers=W | EN).json()
    assert again["batch"]["resubmitted"] and again["ok"] == 0 and again["skipped"] == 2
    assert {i["reason"] for i in again["batch"]["items"]} == {"Already imported in this batch earlier"}


def test_import_errors(api, monkeypatch):
    c, _ = api
    r = c.post("/api/import", files=[("files", ("a.png", _png(13), "image/png"))],
               data={"captured_date": "2024-02-30"}, headers=W)
    assert r.status_code == 400 and r.json()["error"] == "时间不合法：2024-02-30 00:00"
    r = c.post("/api/import", data={"label": "x"}, headers=W | EN)
    assert r.status_code == 400 and r.json()["error"] == "Choose screenshot files first"
    r = c.post("/api/import", content=b"--x--", headers=W | {"Content-Type": "multipart/form-data"})
    assert r.status_code == 400 and r.json()["error"] == "上传内容不是合法的表单（multipart/form-data）"
    monkeypatch.setattr(webapi, "MAX_UPLOAD_FILES", 2)
    three = [("files", (f"{i}.png", _png(20 + i), "image/png")) for i in range(3)]
    r = c.post("/api/import", files=three, headers=W)
    assert r.status_code == 413 and r.json()["error"] == "一次最多导入 2 个文件，请分批上传"
    assert c.post("/api/import", files=three, headers=W | EN).json()["error"] == (
        "At most 2 files per import; upload in smaller batches")


def test_import_oversized_file_is_reported_per_file(api, monkeypatch):
    c, _ = api
    big, small = _png(30, 400, 300), _png(31)
    monkeypatch.setattr(webapi, "MAX_UPLOAD_BYTES", len(small) + 10)
    monkeypatch.setattr(webapi.importer, "MAX_FILE_BYTES", len(small) + 10)
    j = c.post("/api/import", files=[("files", ("big.png", big, "image/png")), ("files", ("small.png", small, "image/png"))],
               headers=W).json()
    items = {i["name"]: i for i in j["batch"]["items"]}
    assert items["big.png"]["status"] == "failed" and items["big.png"]["reason"].startswith("文件超过")
    assert items["small.png"]["status"] == "ok"


# ---------------- 查询模型：状态、预加载、语义 / 画面模式 ----------------

def _loader(p, calls, *, gate=None, fail=None):
    """替身加载：文字模式 = 词袋编码器，画面模式 = 与 FakeImage 对齐的 CLIP 文本替身。"""
    def load(mc, mode):
        calls.append(mode)
        if gate is not None and mode == "text":
            gate.wait(10)
        if fail:
            raise RuntimeError(fail)
        if mode == retrieval.TEXT:
            return p.text.encode, retrieval.TextVectorIndex(p.text.model_id)
        return fake_clip_text, retrieval.ImageVectorIndex(FakeImage.model_id)
    return load


@pytest.fixture
def pipe(h, monkeypatch):
    monkeypatch.setattr(webapi.models, "available", lambda mc: [])  # 当作模型都已下载
    p = Pipeline(h)
    dark = p.add(np.zeros((30, 40, 3), np.uint8) + 10, ["dark terminal"])
    bright = p.add(np.zeros((30, 40, 3), np.uint8) + 245, ["bright page text"])
    return p, dark, bright


def _wait_models(c, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while True:
        m = c.get("/api/models").json()
        if cond(m) or time.monotonic() > end:
            return m
        time.sleep(0.02)


def test_image_mode_search_loads_only_image_encoder(pipe, h, tmp_path):
    p, dark, bright = pipe
    calls: list[str] = []
    c = _client(h.cfg, tmp_path, loader=_loader(p, calls))
    idle = {"state": "idle", "elapsed_ms": None, "reason": None}
    assert c.get("/api/models").json() == {"text": idle, "image": idle} and calls == []  # 查状态不触发加载
    res = _search(c, "dark background", mode="image")
    assert res["mode"] == "image" and res["rank_basis"] == "image"
    assert res["results"][0]["frame_id"] == dark and res["results"][0]["boxes"] == []
    assert res["rank_basis_label"] == "画面描述与整图的相似度" and res["results"][0]["sources"] == ["画面"]
    assert _search(c, "bright page", mode="image")["results"][0]["frame_id"] == bright
    m = c.get("/api/models").json()
    assert m["image"]["state"] == "ready" and isinstance(m["image"]["elapsed_ms"], int) and m["image"]["reason"] is None
    assert m["text"] == idle and calls == ["image"]  # 只加载一次，且没碰文字模型


def test_text_mode_semantic_branch_marks_semantic_boxes(pipe, h, tmp_path):
    p, dark, bright = pipe
    c = _client(h.cfg, tmp_path, loader=_loader(p, []))
    res = _search(c, "page")
    assert res["rank_basis"] == "rrf" and res["notes"] == []
    by = {r["frame_id"]: r for r in res["results"]}
    assert by[bright]["sources"] == ["词法", "语义"] and [b["kind"] for b in by[bright]["boxes"]] == ["lex"]
    assert by[dark]["sources"] == ["语义"] and [b["kind"] for b in by[dark]["boxes"]] == ["sem"]
    d = c.post(f"/api/frames/{dark}/detail", json={"q": "page", "semantic_lines": by[dark]["semantic_lines"]},
               headers=W).json()
    assert d["sem_lines"] == [0] and d["lex_lines"] == []


def test_models_disabled_in_config(api, h, tmp_path):
    cfg = replace(h.cfg, models=replace(h.cfg.models, enabled=False))
    calls: list[str] = []
    c = _client(cfg, tmp_path, loader=lambda mc, mode: calls.append(mode))
    reason = "配置中已关闭向量模型（models.enabled = false）"
    off = {"state": "disabled", "elapsed_ms": None, "reason": reason}
    assert c.get("/api/models").json() == {"text": off, "image": off}
    assert c.get("/api/models", headers=EN).json()["text"]["reason"] == (
        "vector models are disabled in config (models.enabled = false)")
    res = _search(c, "invoice")  # 有词：语义分支不可用，退回词法
    assert len(res["results"]) == 2 and res["notes"] == [f"语义分支不可用：{reason}；当前只按词法排序。"]
    img = _search(c, "invoice", mode="image")
    assert img["results"] == [] and img["message"] == f"画面模式暂不可用：{reason}。不会自动切换到文字模式。"
    assert calls == []


def test_models_not_downloaded(api, h, tmp_path, monkeypatch):
    monkeypatch.setattr(webapi.models, "available", lambda mc: [mc.text.model_id])
    calls: list[str] = []
    c = _client(h.cfg, tmp_path, loader=lambda mc, mode: calls.append(mode))
    m = c.get("/api/models").json()
    assert m["text"]["state"] == "unavailable" and m["text"]["reason"].startswith("模型未下载（") and m["text"]["elapsed_ms"] is None
    assert m["image"]["state"] == "idle"  # 画面模式需要的两套模型都在
    assert c.get("/api/models", headers=EN).json()["text"]["reason"].startswith("models not downloaded (")
    res = c.post("/api/search", json={"q": "invoice"}, headers=W | EN).json()
    assert res["notes"][0].startswith("Semantic branch unavailable: models not downloaded (") and res["results"]
    assert calls == []


def test_preload_runs_in_background_and_does_not_block_other_mode(pipe, h, tmp_path):
    p, dark, _ = pipe
    gate, calls = threading.Event(), []
    app = webapi.create_app(h.cfg, port=8600, dist=tmp_path / "no-dist", preload=True,
                            loader=_loader(p, calls, gate=gate))
    try:
        with TestClient(app, base_url=BASE) as c:  # 进入时执行 lifespan：起预加载线程，不等它
            m = _wait_models(c, lambda m: m["text"]["state"] == "loading")
            assert m["text"]["state"] == "loading" and m["text"]["elapsed_ms"] >= 0 and m["text"]["reason"] is None
            assert m["image"]["state"] == "idle"
            assert c.get("/api/meta").json()["db_ok"]  # 预加载期间服务照常响应
            # 文字模型还在加载：画面模式的查询不被它的锁挡住
            assert _search(c, "dark", mode="image")["results"][0]["frame_id"] == dark
            assert calls == ["text", "image"]
            gate.set()
            m = _wait_models(c, lambda m: m["text"]["state"] == "ready" and m["image"]["state"] == "ready")
            assert m["text"]["state"] == m["image"]["state"] == "ready" and m["text"]["elapsed_ms"] >= 0
            app.app.state.sr.preload_thread.join(5)
            assert calls == ["text", "image"]  # 画面模型已加载过，预加载不重复
    finally:
        gate.set()


def test_preload_failure_is_recorded_not_fatal(pipe, h, tmp_path):
    p, _, bright = pipe
    calls: list[str] = []
    app = webapi.create_app(h.cfg, port=8600, dist=tmp_path / "no-dist", preload=True,
                            loader=_loader(p, calls, fail="boom"))
    with TestClient(app, base_url=BASE) as c:
        app.app.state.sr.preload_thread.join(5)
        bad = {"state": "unavailable", "elapsed_ms": None, "reason": "模型加载失败：RuntimeError: boom"}
        assert c.get("/api/models").json() == {"text": bad, "image": bad}
        assert c.get("/api/models", headers=EN).json()["image"]["reason"] == "model failed to load: RuntimeError: boom"
        res = _search(c, "page")  # 查询时会再试一次，仍失败则退回词法
        assert res["results"][0]["frame_id"] == bright
        assert res["notes"] == ["语义分支不可用：模型加载失败：RuntimeError: boom；当前只按词法排序。"]
    assert calls == ["text", "image", "text"]


def test_preload_skipped_when_disabled_and_off_by_default(api, h, tmp_path):
    calls: list[str] = []
    cfg = replace(h.cfg, models=replace(h.cfg.models, enabled=False))
    app = webapi.create_app(cfg, port=8600, dist=tmp_path / "no-dist", preload=True, loader=lambda mc, m: calls.append(m))
    with TestClient(app, base_url=BASE) as c:
        app.app.state.sr.preload_thread.join(5)
        assert c.get("/api/models").json()["text"]["state"] == "disabled"
    plain = webapi.create_app(h.cfg, port=8600, dist=tmp_path / "no-dist", loader=lambda mc, m: calls.append(m))
    with TestClient(plain, base_url=BASE):
        assert plain.app.state.sr.preload_thread is None  # create_app 默认不预加载（测试不会拉起真实模型）
    assert calls == []


def test_cli_preload_flag(tmp_path, monkeypatch):
    import uvicorn

    seen = []
    monkeypatch.setattr(webapi, "create_app", lambda cfg, **kw: seen.append(kw["preload"]) or object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    assert webapi.main(["--data-dir", str(tmp_path / "d"), "--port", "8621"]) == 0
    assert webapi.main(["--data-dir", str(tmp_path / "d"), "--port", "8621", "--no-preload"]) == 0
    assert seen == [True, False]
