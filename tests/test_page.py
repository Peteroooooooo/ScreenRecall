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
