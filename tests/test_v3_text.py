"""v3 ② 读字：UIA 正文够不够、OCR 补读、合并去重、坐标换算、出错不抛（全部用假的 UIA / OCR，不碰真实窗口）。"""

from __future__ import annotations

import numpy as np
import pytest

from screenrecall.rec.frames import Frame, FrameMap
from screenrecall.text.ocr2 import OcrLine, OcrModelsMissing
from screenrecall.text.uia import UiaLine, UiaResult
from screenrecall.v3 import config
from screenrecall.v3.read import TextReader, merge_lines
from screenrecall.v3.types import TextLine

HWND = 0x7FFF0010  # 不存在的窗口句柄：标题栏高度取不到，按 0 算
FMAP = FrameMap(100, 200, 800, 600)  # 帧左上角在屏幕 (100, 200)
WIN = (100, 200, 900, 800)


@pytest.fixture
def cfg(tmp_path):
    return config.build({}, data_dir=tmp_path / "data", home=tmp_path / "home")


def _frame(fmap=FMAP):
    return Frame.from_bgr(HWND, 0, np.full((fmap.height, fmap.width, 3), 255, np.uint8), fmap=fmap)


def _ul(text, rect, role="text"):
    return UiaLine(text, rect, "Text", "name", role)


def _body(n_lines, chars=30, y0=300):
    """n 行合成正文（每行 chars 个字左右），屏幕坐标。"""
    return [_ul(f"合成正文第{i}行" + "字" * (chars - 6), (120, y0 + 30 * i, 800, y0 + 30 * i + 24)) for i in range(n_lines)]


def _ok(lines, **kw):
    kw.setdefault("window_rect", WIN)
    return UiaResult("ok", lines, True, 3.0, element_count=len(lines), **kw)


class FakeUia:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def read(self, hwnd, timeout_s=None):
        self.calls.append((hwnd, timeout_s))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


def _ol(text, x0, y0, x1, y1):
    return OcrLine(text, 0.9, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], x0, y0, x1, y1)


class FakeOcr:
    def __init__(self, lines=()):
        self.lines = list(lines)
        self.calls = 0

    def recognize(self, bgr):
        self.calls += 1
        assert bgr.ndim == 3 and bgr.shape[2] == 3
        return list(self.lines)


def test_enough_body_text_does_not_ocr(cfg):
    ocr = FakeOcr([_ol("不该出现的识别结果", 0, 0, 100, 20)])
    uia = FakeUia(_ok(_body(5), doc_url="https://synthetic.test/a#x"))
    r = TextReader(cfg, uia=uia, ocr=ocr)
    res = r.read_text(HWND, _frame())
    assert ocr.calls == 0
    assert res.via == "uia" and res.status == "ok" and len(res.lines) == 5
    assert res.body_chars >= cfg.ocr_min_chars and res.uia_chars == res.body_chars
    assert res.url == "https://synthetic.test/a#x"  # 原样交出，清理是出处的事
    assert uia.calls == [(HWND, cfg.uia_timeout_ms / 1000.0)]


def test_little_body_text_runs_ocr_and_merges(cfg):
    ocr = FakeOcr([_ol("图片里识别出的一行字", 20, 400, 300, 420)])
    r = TextReader(cfg, uia=FakeUia(_ok(_body(1))), ocr=ocr)
    res = r.read_text(HWND, _frame())
    assert ocr.calls == 1
    assert res.via == "uia+ocr" and [ln.via for ln in res.lines] == ["uia", "ocr"]
    assert res.lines[1].box == (20, 400, 280, 20)


def test_mostly_menu_text_runs_ocr(cfg):
    """正文 120 字够了"至少 100 字"，但读到的字大多是菜单按钮（正文不到 30%）→ 也 OCR。"""
    ui = [_ul("合成菜单按钮" * 10, (120, 210 + 10 * i, 800, 218 + 10 * i), role="ui") for i in range(10)]
    ocr = FakeOcr()
    r = TextReader(cfg, uia=FakeUia(_ok(_body(4) + ui)), ocr=ocr)
    res = r.read_text(HWND, _frame())
    assert ocr.calls == 1 and res.body_chars >= 100 and res.body_chars < 0.3 * res.uia_chars
    assert all(ln.via == "uia" for ln in res.lines) and len(res.lines) == 4  # 菜单按钮不当正文
    assert res.via == "uia"


def test_merge_dedupes_identical_and_contained_lines():
    u = [TextLine("Hello World 合成", (10, 10, 200, 20), "uia"), TextLine("第二行 UIA 文字", (10, 40, 200, 20), "uia")]
    o = [
        TextLine("hello  world合成", (12, 11, 190, 18), "ocr"),       # norm 相同 → 去掉
        TextLine("第二行", (15, 42, 60, 16), "ocr"),                    # 被 UIA 行包含、框重叠 → 去掉
        TextLine("第二行 UIA 文字 多识别的尾巴", (10, 40, 260, 20), "ocr"),  # 包含 UIA 行、框重叠 → 去掉（UIA 优先）
        TextLine("第二行", (500, 300, 60, 16), "ocr"),                  # 不重叠，但只是别的行开头的一截（截断的预览）→ 去掉
        TextLine("别处的另一行字", (500, 340, 90, 16), "ocr"),          # 不重叠的别的字 → 留
        TextLine("OCR 半截", (10, 100, 80, 20), "ocr"),
        TextLine("OCR 半截 整行", (10, 100, 150, 20), "ocr"),           # 包含前一条 OCR 行、重叠 → 换成更完整的
        TextLine("x", (10, 200, 10, 10), "ocr"),                        # 少于 2 个字 → 去掉
        TextLine("在最上面的 OCR 行", (300, 0, 100, 8), "ocr"),
    ]
    got = merge_lines(u, o)
    assert [ln.text for ln in got] == ["在最上面的 OCR 行", "Hello World 合成", "第二行 UIA 文字", "OCR 半截 整行",
                                       "别处的另一行字"]
    # UIA 行之间 norm 相同只留一行
    dup = merge_lines([TextLine("同一行", (0, 0, 5, 5), "uia"), TextLine("同 一 行", (0, 50, 5, 5), "uia")], [])
    assert len(dup) == 1


def test_merge_drops_near_duplicate_ocr_copy_of_uia_line():
    """OCR 认错一两个字、标点不同的同一句：框重叠就只留 UIA 那份；不重叠的相似行（别处的另一句）留着。"""
    u = [TextLine("The quick brown fox jumps over the lazy dog.", (10, 10, 400, 20), "uia")]
    o = [TextLine("The quick brovvn fox jumps over the lazy dog", (12, 11, 398, 20), "ocr"),
         TextLine("合成的另一句完全不同的话", (10, 40, 300, 20), "ocr"),
         TextLine("The quick brown fox jumps over the lazy cat.", (10, 500, 400, 20), "ocr")]
    got = merge_lines(u, o)
    assert [(ln.text, ln.via) for ln in got] == [("The quick brown fox jumps over the lazy dog.", "uia"),
                                                 ("合成的另一句完全不同的话", "ocr"),
                                                 ("The quick brown fox jumps over the lazy cat.", "ocr")]


def test_ocr_lines_above_chrome_bottom_are_dropped(cfg):
    """标签页条 / 工具栏到屏幕 y=320（帧 y=120）为止：中心在它上面的 OCR 行不要。"""
    ocr = FakeOcr([_ol("合成标签页标题", 10, 10, 200, 40), _ol("合成地址栏网址", 10, 80, 300, 110),
                   _ol("页面里图片上的字", 10, 200, 300, 230)])
    r = TextReader(cfg, uia=FakeUia(_ok([], chrome_bottom=320)), ocr=ocr)
    res = r.read_text(HWND, _frame())
    assert [ln.text for ln in res.lines] == ["页面里图片上的字"] and res.via == "ocr"


def test_uia_rects_are_converted_to_frame_coords(cfg):
    lines = [_ul("合成正文" + "字" * 120, (150, 260, 300, 280)), _ul("帧外的字不要", (2000, 260, 2100, 280))]
    res = TextReader(cfg, uia=FakeUia(_ok(lines)), ocr=False).read_text(HWND, _frame())
    assert [ln.box for ln in res.lines] == [(50, 60, 150, 20)]
    # 截到这一帧之后窗口被拖到 (300, 250)（大小没变）：按读字时的位置换算
    line = [_ul("合成正文" + "字" * 120, (350, 300, 500, 320))]
    res = TextReader(cfg, uia=FakeUia(_ok(line)), ocr=False).read_text(HWND, _frame())
    assert res.lines[0].box == (250, 100, 150, 20)
    moved = _ok(line, window_rect=(300, 250, 1100, 850))
    res = TextReader(cfg, uia=FakeUia(moved), ocr=False).read_text(HWND, _frame())
    assert res.lines[0].box == (50, 50, 150, 20)


def test_uia_error_and_ocr_missing_is_error_not_exception(cfg):
    built = []

    def factory():
        built.append(1)
        raise OcrModelsMissing("合成：缺模型")

    r = TextReader(cfg, uia=FakeUia(UiaResult("timeout", [], False, 250.0, window_rect=WIN)), ocr=factory)
    for _ in range(2):
        res = r.read_text(HWND, _frame())
        assert res.status == "error" and res.lines == () and res.via == "none" and res.url is None
    assert built == [1]  # 缺模型只试一次


def test_uia_raising_still_ocrs(cfg):
    ocr = FakeOcr([_ol("只靠识别读到的字", 10, 300, 300, 330)])
    res = TextReader(cfg, uia=FakeUia(RuntimeError("合成")), ocr=ocr).read_text(HWND, _frame())
    assert res.status == "ok" and res.via == "ocr" and len(res.lines) == 1


def test_broken_frame_never_raises(cfg):
    class NoPixels:
        fmap = FMAP

        @property
        def bgr(self):
            raise ValueError("这一帧没有原图")

    res = TextReader(cfg, uia=FakeUia(_ok([])), ocr=FakeOcr()).read_text(HWND, NoPixels())
    assert res.status == "empty" and res.lines == ()


def test_ocr_factory_is_built_lazily_once(cfg):
    built = []
    engine = FakeOcr([_ol("识别出的字", 10, 300, 200, 320)])

    def factory():
        built.append(1)
        return engine

    r = TextReader(cfg, uia=FakeUia(_ok(_body(5)), _ok([]), _ok([])), ocr=factory)
    r.read_text(HWND, _frame())
    assert built == []  # 系统文字够：不建 OCR
    r.read_text(HWND, _frame())
    r.read_text(HWND, _frame())
    assert built == [1] and engine.calls == 2


def test_no_ocr_flag_and_empty(cfg):
    res = TextReader(cfg, uia=FakeUia(_ok([])), ocr=False).read_text(HWND, _frame())
    assert res.status == "empty" and res.via == "none"
    res = TextReader(cfg, uia=FakeUia(UiaResult("error", [], False, 1.0)), ocr=False).read_text(HWND, _frame())
    assert res.status == "error"


def test_first_touch_rereads_until_the_tree_is_built(cfg, monkeypatch):
    """第一次读一个浏览器窗口只拿到外框：隔一会儿再读，直到读得出字、元素数不再变多（实测 Edge 0.2 秒不够）。"""
    import screenrecall.v3.read as read_mod

    monkeypatch.setattr(read_mod.time, "sleep", lambda s: None)
    frame_only = UiaResult("ok", [], False, 3.0, element_count=2, window_rect=WIN, first_touch=True)
    still_frame = UiaResult("ok", [], False, 3.0, element_count=2, window_rect=WIN)
    full = _ok(_body(8), doc_url="https://example.com/a")
    uia = FakeUia(frame_only, still_frame, full, full, full)
    res = TextReader(cfg, uia=uia, ocr=False).read_text(HWND, _frame())
    assert res.url == "https://example.com/a" and res.via == "uia" and len(res.lines) == 8
    assert len(uia.calls) == 4  # 外框、还是外框、读出来了、不再变多 → 停


def test_timeout_keeps_url_and_ocrs_with_it(cfg, monkeypatch):
    """大页面正文读超时：读字前单独找到的网址和网页标题照样交出，正文交给 OCR；超时不再重读（再读也是超时）。"""
    import screenrecall.v3.read as read_mod

    monkeypatch.setattr(read_mod.time, "sleep", lambda s: None)
    slow = UiaResult("timeout", [], False, 1500.0, window_rect=WIN, doc_url="https://synthetic.test/big",
                     doc_name="合成长文")
    uia = FakeUia(slow, slow, slow)
    ocr = FakeOcr([_ol("OCR 读出的正文", 20, 400, 300, 420)])
    r = TextReader(cfg, uia=uia, ocr=ocr, title_of=lambda h: "合成长文 - 浏览器")
    res = r.read_text(HWND, _frame())
    assert len(uia.calls) == 1 and ocr.calls == 1
    assert (res.url, res.page_title, res.window_title) == ("https://synthetic.test/big", "合成长文", "合成长文 - 浏览器")
    assert res.via == "ocr" and res.status == "ok" and r.stats["uia_timeouts"] == 1 and r.stats["ocr"] == 1
    assert r.stats["ocr_ms"] >= 0


def test_want_ocr_false_skips_ocr(cfg):
    """字不够、本该 OCR：先问调用方（带着网址、窗口标题），说不存就不 OCR。"""
    asked = []
    ocr = FakeOcr([_ol("不会读到", 20, 400, 300, 420)])
    r = TextReader(cfg, uia=FakeUia(_ok([], doc_url="edge://settings")), ocr=ocr, title_of=lambda h: "设置")
    res = r.read_text(HWND, _frame(), want_ocr=lambda part: asked.append((part.url, part.window_title)) or False)
    assert asked == [("edge://settings", "设置")] and ocr.calls == 0
    assert res.ocr_skipped and res.lines == () and res.url == "edge://settings" and r.stats["ocr_skipped"] == 1
    # 系统文字够了：不用问
    r = TextReader(cfg, uia=FakeUia(_ok(_body(5))), ocr=ocr, title_of=lambda h: "")
    r.read_text(HWND, _frame(), want_ocr=lambda part: asked.append("again") or False)
    assert len(asked) == 1


def test_merge_drops_time_only_and_truncated_ocr_previews():
    from screenrecall.v3.read import merge_lines

    ocr = [TextLine(t, (10, 40 * i, 200, 20), "ocr") for i, t in enumerate(
        ["14:05", "昨天 12:30", "2026/10/28", "10月28日", "星期三", "小王：明天下午三点在", "小王：明天下午三点在会议室开组会",
         "3.14", "第 2 章", "共 128 条"])]
    uia = [TextLine("下午 9:30", (300, 0, 100, 20), "uia"), TextLine("合成正文一行", (300, 40, 100, 20), "uia")]
    kept = [ln.text for ln in merge_lines(uia, ocr)]
    assert kept == ["合成正文一行", "小王：明天下午三点在会议室开组会", "3.14", "第 2 章", "共 128 条"]


def test_timeout_ocr_drops_lines_above_document_top(cfg):
    slow = UiaResult("timeout", [], False, 1500.0, window_rect=WIN, doc_url="https://synthetic.test/big",
                     chrome_bottom=320)
    ocr = FakeOcr([_ol("标签页条里的字", 20, 10, 300, 30), _ol("网页正文的字", 20, 400, 300, 420)])
    res = TextReader(cfg, uia=FakeUia(slow), ocr=ocr, title_of=lambda h: "").read_text(HWND, _frame())
    assert [ln.text for ln in res.lines] == ["网页正文的字"]


def test_window_names_reach_result_and_want_ocr(cfg):
    """辅助功能里的窗口名字（无痕标记可能只在这里）交给调用方：要 OCR 之前问的那一份也带着，调用方可以不 OCR 就跳过。"""
    names = ("合成页 - 浏览器", "合成页 - 浏览器（无痕）")
    asked = []
    ocr = FakeOcr([_ol("不会读到", 20, 400, 300, 420)])
    uia = FakeUia(_ok([], doc_url="https://synthetic.test/a", doc_name="合成页", window_names=names))
    r = TextReader(cfg, uia=uia, ocr=ocr, title_of=lambda h: "合成页 - 浏览器")
    res = r.read_text(HWND, _frame(), want_ocr=lambda part: asked.append(part.window_names) or False)
    assert asked == [names] and ocr.calls == 0 and res.window_names == names
    r = TextReader(cfg, uia=FakeUia(_ok(_body(5))), ocr=ocr, title_of=lambda h: "")
    assert r.read_text(HWND, _frame()).window_names == ()
