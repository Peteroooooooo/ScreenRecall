"""页面 AppTest：在 Mac 上用假 winapi + 真实 SQLite/WebP 驱动 app.py，检查证据浏览（方案 6.1）。

只用引号原串查询，不加载任何模型。这只验证页面逻辑；Windows 浏览器里的实际排版仍需实机查看。
"""

import logging
import threading

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from screenrecall.ocr import OcrBox, OcrWorker  # noqa: E402

from .conftest import ROOT, make_image  # noqa: E402

A, B, C = make_image(1), make_image(2), make_image(3)


def _ocr_all(h, texts: dict[str, list[str]]) -> None:
    """用替身 OCR 引擎跑完所有待办；texts 按 frame_id 给出各行文字。"""

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
def page(h, monkeypatch):
    """录制段：A →（等稳缺口）B →（等稳缺口）C；A、B 含 invoice。"""
    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    for img in (B, C):
        h.show(img)
        h.tick()
        h.tick()
    fids = [f["frame_id"] for f in h.frames()]
    _ocr_all(h, dict(zip(fids, [["alpha invoice"], ["beta invoice"], ["gamma"]])))
    monkeypatch.setenv("SCREENRECALL_DATA_DIR", str(h.cfg.data_dir))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    assert not at.exception
    at.text_input(key="q_input").input('"invoice"')
    at.button(key="search_submit").click().run()
    assert not at.exception
    return at, fids


def _texts(at, kind):
    return [e.value for e in getattr(at, kind)]


def test_open_result_shows_context_and_gap_warnings(page):
    at, (fa, fb, fc) = page
    at.button(key=f"open-{fb}").click().run()  # 时间倒序：B 在前
    assert not at.exception
    assert at.session_state.selected["frame_id"] == fb
    captions = " ".join(_texts(at, "caption"))
    assert "第 2 / 3 次出现" in captions
    warns = " ".join(_texts(at, "warning"))
    assert "与上一条之间" in warns and "与下一条之间" in warns and "画面有变化但未保存" in warns
    assert at.button(key="ctx-prev") and at.button(key="ctx-next")


def test_step_to_neighbour_and_back_to_search_result(page):
    at, (fa, fb, fc) = page
    at.button(key=f"open-{fb}").click().run()
    at.button(key="ctx-next").click().run()
    assert not at.exception
    sel = at.session_state.selected
    assert sel["frame_id"] == fc and sel["origin"]["frame_id"] == fb
    captions = " ".join(_texts(at, "caption"))
    assert "第 3 / 3 次出现" in captions and "已是本录制段的最后一条（仍在录制）" in captions
    at.button(key="ctx-back").click().run()
    assert at.session_state.selected["frame_id"] == fb


def test_side_by_side_with_previous(page):
    at, (fa, fb, fc) = page
    at.button(key=f"open-{fb}").click().run()
    at.radio(key="compare_with").set_value("上一条").run()
    assert not at.exception
    captions = " ".join(_texts(at, "caption"))
    assert "并排只放两张原图" in captions and "画面有变化但未保存" in captions


def test_first_step_has_no_prev_button(page):
    at, (fa, fb, fc) = page
    at.button(key=f"open-{fa}").click().run()
    assert not at.exception
    assert "已是本录制段的第一条" in " ".join(_texts(at, "caption"))
    assert not [b for b in at.button if b.key == "ctx-prev"]


# ---------- 放宽匹配按钮（方案 5.2、A06） ----------

def _search(at, raw):
    at.text_input(key="q_input").input(raw)
    at.button(key="search_submit").click().run()
    assert not at.exception


def test_relax_button_only_after_quote_miss_and_only_on_click(page):
    at, (fa, fb, fc) = page
    assert not [b for b in at.button if b.key == "relax"]  # 有匹配时不出现
    _search(at, '"INVOICE"')
    assert not [b for b in at.button if b.key == "relax"]  # 默认忽略大小写，仍有匹配
    _search(at, '"alphainvoice"')
    assert "没有画面同时包含全部引号原串" in " ".join(_texts(at, "info"))
    assert at.session_state.query.get("relaxed") is not True  # 不自动放宽
    at.button(key="relax").click().run()
    assert not at.exception
    assert at.session_state.query["relaxed"] is True
    assert "放宽匹配：忽略大小写和全部空白" in " ".join(_texts(at, "warning"))
    assert [b.key for b in at.button if b.key == f"open-{fa}"]
    at.button(key="strict_again").click().run()
    assert at.session_state.query["relaxed"] is False
    _search(at, '"gamma"')
    assert at.session_state.query.get("relaxed") is None  # 新查询从严格规则开始


# ---------- 导入的截图（方案 6.8） ----------

def test_imported_screenshot_shows_time_unknown_and_batch_order(h, monkeypatch):
    import io

    from PIL import Image

    from screenrecall import importer

    def png(seed):
        buf = io.BytesIO()
        Image.fromarray(make_image(seed, 64, 48)[:, :, ::-1]).save(buf, "PNG")
        return buf.getvalue()

    res = importer.import_batch(
        h.conn, h.cfg, [importer.ImportFile("one.png", png(1)), importer.ImportFile("two.png", png(2))],
        label="旧截图", free_bytes=lambda p: 100 * 1024**3,
    )
    _ocr_all(h, {res.items[0].frame_id: ["imported invoice"], res.items[1].frame_id: ["second page"]})
    monkeypatch.setenv("SCREENRECALL_DATA_DIR", str(h.cfg.data_dir))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    _search(at, '"imported"')
    assert "捕获时间未知" in " ".join(m.value for m in at.markdown)
    at.button(key=f"open-{res.items[0].frame_id}").click().run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(_texts(at, "caption"))
    assert "捕获时间未知" in text and "导入批次" in text and "不是连续的屏幕历史" in text
    assert "第 1 / 2 张" in text


# ---------- 收藏与删除（方案 5.2、5.4） ----------

def test_favorite_then_delete_frame_requires_confirmation_naming_favorites(page):
    at, (fa, fb, fc) = page
    at.button(key=f"open-{fb}").click().run()
    occ = at.session_state.selected["occurrence_id"]
    at.button(key=f"fav-{occ}").click().run()
    assert not at.exception
    assert [b for b in at.button if b.key == f"unfav-{occ}"]
    assert "★ 已收藏" in " ".join(_texts(at, "caption"))

    at.button(key=f"del-frame-{fb}").click().run()
    assert not at.exception
    err = " ".join(_texts(at, "error"))
    assert "全部 1 次出现记录" in err and "1 个收藏" in err
    assert at.session_state.selected["frame_id"] == fb  # 只是展示影响范围，尚未删除
    at.button(key=f"yes-confirm-frame-{fb}").click().run()
    assert not at.exception
    assert "selected" not in at.session_state
    assert "已删除这幅画面" in " ".join(_texts(at, "success"))
    assert not [b for b in at.button if b.key == f"open-{fb}"]
    assert [b for b in at.button if b.key == f"open-{fa}"]


def test_delete_occurrence_cancel_keeps_record(page):
    at, (fa, fb, fc) = page
    at.button(key=f"open-{fa}").click().run()
    occ = at.session_state.selected["occurrence_id"]
    at.button(key=f"del-occ-{occ}").click().run()
    assert "唯一一次出现" in " ".join(_texts(at, "warning"))
    at.button(key=f"no-confirm-occ-{occ}").click().run()
    assert not at.exception
    assert at.session_state.selected["frame_id"] == fa
    assert [b for b in at.button if b.key == f"open-{fa}"]


def test_favorites_panel_opens_detail_without_a_query(h, monkeypatch):
    from screenrecall import lifecycle

    h.show(A)
    h.start()
    h.rec.sample(h.conn)
    fid = h.frames()[0]["frame_id"]
    _ocr_all(h, {fid: ["kept receipt"]})
    occ = h.occurrences()[0]["occurrence_id"]
    lifecycle.add_favorite(h.conn, occ)
    monkeypatch.setenv("SCREENRECALL_DATA_DIR", str(h.cfg.data_dir))
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=30)
    at.run()
    at.button(key=f"favopen-{occ}").click().run()
    assert not at.exception
    assert at.session_state.selected["occurrence_id"] == occ
    assert [b for b in at.button if b.key == f"unfav-{occ}"]
