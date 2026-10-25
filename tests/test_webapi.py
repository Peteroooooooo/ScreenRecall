"""网页 API（screenrecall.webapi）：用假 winapi + 真实 SQLite/WebP 驱动，覆盖 app.py 页面测试里的关键行为。
只用引号原串查询，不加载任何模型。"""
import io
import logging
import threading

import pytest
from PIL import Image

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from screenrecall import lifecycle, webapi  # noqa: E402
from screenrecall.ocr import OcrBox, OcrWorker  # noqa: E402

from .conftest import SCREEN, make_image  # noqa: E402

A, B, C = make_image(1), make_image(2), make_image(3)
W = {"X-ScreenRecall": "1"}


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


@pytest.fixture
def api(h, tmp_path):
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    for img in (B, C):
        h.show(img)
        h.tick()
        h.tick()
    fids = [f["frame_id"] for f in h.frames()]
    _ocr_all(h, dict(zip(fids, [["alpha invoice"], ["beta invoice"], ["gamma"]])))
    app = webapi.create_app(h.cfg, port=8600, dist=tmp_path / "no-dist")
    return TestClient(app, base_url="http://127.0.0.1:8600"), fids


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
