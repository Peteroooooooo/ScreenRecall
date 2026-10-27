"""UIA 读字的纯逻辑与线程行为：可见过滤、去重、超时放弃、提示缓存（不碰真实窗口）。"""

from __future__ import annotations

import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from screenrecall.text import uia
from screenrecall.text.uia import Elem, TpLines, UiaConfig, UiaLine, UiaReader, collect_lines

WIN = (100, 100, 900, 700)


def _texts(lines):
    return [ln.text for ln in lines]


def test_clean_text_strips_format_chars():
    assert uia.clean_text("‎2026/10/27‏ 10:00 ") == "2026/10/27 10:00"
    assert uia.clean_text("a\tb") == "a b"
    assert uia.clean_text("") == ""


def test_filters_offscreen_outside_password_empty():
    elems = [
        Elem(-1, "Window", name="窗口标题", rect=WIN),
        Elem(0, "Text", name="看得见的正文", rect=(120, 120, 400, 140)),
        Elem(0, "Text", name="屏外的段落", rect=(120, 2000, 400, 2020), offscreen=True),
        Elem(0, "Text", name="窗口外的元素", rect=(1000, 120, 1200, 140)),
        Elem(0, "Edit", name="密码", value="hunter2", rect=(120, 200, 300, 220), password=True),
        Elem(0, "Text", name="   ", rect=(120, 240, 300, 260)),
        Elem(0, "Text", name="空矩形", rect=(120, 240, 120, 260)),
    ]
    got = _texts(collect_lines(elems, [], WIN))
    assert "看得见的正文" in got and "窗口标题" in got
    for bad in ("屏外的段落", "窗口外的元素", "密码", "hunter2", "空矩形"):
        assert bad not in got


def test_roles_and_value():
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Button", name="保存", rect=(110, 110, 150, 130)),
        Elem(0, "Edit", name="地址和搜索栏", value="example.test/page", rect=(200, 110, 600, 130)),
    ]
    lines = collect_lines(elems, [], WIN)
    by = {ln.text: ln for ln in lines}
    assert by["保存"].role == "ui"
    assert by["example.test/page"].source_detail == "value" and by["example.test/page"].role == "text"
    assert uia.content_chars(lines) == len("地址和搜索栏") + len("example.test/page")


def test_textpattern_lines_preferred_and_whole_doc_value_dropped():
    doc_value = "\n".join(f"第{i}行" for i in range(300))  # 整篇全文（含滚走的部分）
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Document", name="文档", value=doc_value, rect=(100, 130, 900, 700), has_tp=True),
        Elem(1, "Text", name="第一行 visible", rect=(110, 140, 500, 160)),  # 已被可见行覆盖
        Elem(1, "Text", name="只在子元素里的字", rect=(110, 400, 500, 420)),
    ]
    tps = [TpLines(1, [("第一行 visible\r", [(110, 140, 300, 160), (300, 140, 500, 160)]),
                       ("第二行\r\n", [(110, 160, 2000, 180)])])]
    lines = collect_lines(elems, tps, WIN)
    got = _texts(lines)
    assert got.count("第一行 visible") == 1
    assert "第二行" in got and "只在子元素里的字" in got
    assert "文档" not in got and not any("第299行" in t for t in got)
    first = next(ln for ln in lines if ln.text == "第一行 visible")
    assert first.rect == (110, 140, 500, 160) and first.source_detail == "textpattern:Document"
    # 控制台的行矩形会延伸到缓冲区右边：裁到窗口内
    second = next(ln for ln in lines if ln.text == "第二行")
    assert second.rect[2] == WIN[2]


def test_wrapped_paragraph_name_is_not_stored_next_to_its_visible_lines():
    """网页的 <p> 在 UIA 里是 Text 元素，Name 是整段；TextPattern 按显示的行给出折行后的几行。
    整段不能再收一遍（不然每段文字存三次）；只露出一半的段落，屏幕外那半也不能经 Name 收进来。"""
    para = "A patient locksmith weighed forty paper cranes next to the bakery oven during week 72, then wrote 24 notes."
    half = "The radio operator counted a box of glass marbles under the stone bridge during week 73, then wrote 37."
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Document", name="页面", rect=(100, 130, 900, 700), has_tp=True),
        Elem(1, "Text", name=para, rect=(110, 140, 880, 200)),
        Elem(1, "Text", name=half, rect=(110, 660, 880, 720)),
        Elem(1, "Image", name="合成图片的说明文字", rect=(110, 300, 400, 400)),
    ]
    tps = [TpLines(1, [("A patient locksmith weighed forty paper cranes next to the bakery oven during week", [(110, 140, 880, 170)]),
                       ("72, then wrote 24 notes.", [(110, 170, 400, 200)]),
                       ("The radio operator counted a box of glass marbles under the stone bridge during", [(110, 660, 880, 690)])])]
    got = _texts(collect_lines(elems, tps, WIN))
    assert para not in got and half not in got
    assert len(got) == 4 and "合成图片的说明文字" in got  # 文档里不在可见行中的字照收


def test_textpattern_without_lines_falls_back_to_short_value():
    elems = [Elem(-1, "Window", rect=WIN), Elem(0, "Edit", name="", value="单行短值", rect=(110, 110, 300, 130), has_tp=True)]
    got = _texts(collect_lines(elems, [TpLines(1, [])], WIN))
    assert got == ["单行短值"]


def test_outermost_tp_and_dedupe_sort():
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Document", rect=WIN, has_tp=True),
        Elem(1, "Edit", rect=WIN, has_tp=True),
        Elem(0, "Edit", rect=WIN, has_tp=True, password=True),
        Elem(0, "Text", name="下面", rect=(110, 500, 200, 520)),
        Elem(0, "Text", name="上面", rect=(110, 200, 200, 220)),
        Elem(0, "Text", name="上面", rect=(110, 200, 200, 220)),
    ]
    assert uia.outermost_tp(elems) == [1]
    got = _texts(collect_lines(elems, [], WIN))
    assert got == ["上面", "下面"]


def test_multiline_name_split():
    elems = [Elem(-1, "Window", rect=WIN), Elem(0, "Text", name="甲\r\n乙\n", rect=(110, 110, 300, 150))]
    assert _texts(collect_lines(elems, [], WIN)) == ["甲", "乙"]


def test_to_frame_rect():
    assert uia.to_frame_rect((150, 260, 300, 280), (100, 200)) == (50, 60, 200, 80)


# ---------- 没读到字的区域与待 OCR ----------

def _ln(t, b, text="正文", role="text"):
    return UiaLine(text, (110, t, 800, b), "Text", "name", role)


def test_uncovered_bands_chrome_only_and_container_ignored():
    # 浏览器第一次被访问：只有顶部工具栏的字，外加一个整窗大小的窗口名
    lines = [_ln(100, 130, "标签页标题"), _ln(135, 160, "example.test/page"),
             UiaLine("窗口标题", WIN, "Window", "name", "ui")]
    assert uia.uncovered_bands(lines, WIN, min_gap_px=48) == [(100, 160, 900, 700)]
    # 段落间距小于 min_gap_px 不算；中间一大段空着算
    lines = [_ln(100, 130), _ln(150, 180), _ln(400, 430), _ln(660, 700)]
    assert uia.uncovered_bands(lines, WIN, min_gap_px=48) == [(100, 180, 900, 400), (100, 430, 900, 660)]
    assert uia.uncovered_bands([], WIN) == [WIN]
    # 重叠、乱序的行
    assert uia.uncovered_bands([_ln(480, 700), _ln(300, 520), _ln(100, 330)], WIN) == []


def _canvas(h=600, w=800):
    return np.full((h, w, 3), 255, np.uint8)


def _write(img, x, y, text="一行短字 short", size=14):
    im = Image.fromarray(img)
    ImageDraw.Draw(im).text((x, y), text, fill=(0, 0, 0), font=ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", size))
    img[:] = np.asarray(im)


def test_has_ink():
    img = _canvas(120, 1500)
    assert not uia.has_ink(img)
    _write(img, 20, 50, "短词", 12)  # 宽横条里只有一个短词也要算有字
    assert uia.has_ink(img)
    grad = np.tile(np.linspace(0, 255, 1500, dtype=np.uint8)[None, :, None], (120, 1, 3))
    assert not uia.has_ink(grad)
    div = _canvas(120, 1500)
    div[:, 700] = 0  # 竖分隔线
    assert not uia.has_ink(div)
    line = _canvas(120, 1500)
    line[60] = 0  # 横线
    assert not uia.has_ink(line)
    assert uia.has_ink(img[:, :, 1])  # 单通道（绿色通道）也行
    assert not uia.has_ink(np.zeros((1, 10), np.uint8))


def test_ocr_regions():
    frame = _canvas()
    _write(frame, 40, 400, "图片里的字 text inside an image")
    origin = (WIN[0], WIN[1])  # 帧左上角 = 窗口可见边框左上角
    bad = uia.UiaResult("timeout", [], False, 1.0, window_rect=WIN)
    assert uia.ocr_regions(bad, frame) == [(0, 0, 800, 600)]
    assert uia.ocr_regions(bad, _canvas()) == []  # 整帧空白：不登记
    unusable = uia.UiaResult("ok", [_ln(100, 130, "短")], False, 1.0, window_rect=WIN)
    assert uia.ocr_regions(unusable, frame) == [(0, 0, 800, 600)]
    # 可用：只登记有字的那条没读到字的横条（屏幕 y 300..550 -> 帧 y 200..450），空白的横条不登记
    good = uia.UiaResult("ok", [_ln(100, 130, "足够长的一段正文文字 enough text here")], True, 1.0,
                         window_rect=WIN, uncovered=((100, 160, 900, 280), (100, 300, 900, 550)))
    assert uia.ocr_regions(good, frame) == [(0, 200, 800, 450)]
    assert uia.ocr_regions(good, frame, origin) == [(0, 200, 800, 450)]
    assert uia.ocr_regions(uia.UiaResult("ok", good.lines, True, 1.0, window_rect=WIN), frame) == []


# ---------- 读取器：线程与超时 ----------

class FakeWin:
    def __init__(self):
        self.iconic = False
        self.alive = True

    def is_window(self, hwnd):
        return self.alive

    def is_iconic(self, hwnd):
        return self.iconic

    def frame_rect(self, hwnd):
        return WIN

    def window_key(self, hwnd):
        return (1, "Cls")


class FakeBackend:
    def __init__(self, lines=None, delay=0.0, counts=None):
        self.lines = lines if lines is not None else [UiaLine("一段足够长的正文文字 enough text", (110, 110, 400, 130), "Text", "name")]
        self.delay = delay
        self.counts = list(counts or [])
        self.opened = 0
        self.closed = 0
        self.reads = 0
        self.threads: set[int] = set()

    def open(self):
        self.opened += 1
        return object()

    def read(self, ctx, hwnd, win_rect, cancel):
        self.reads += 1
        self.threads.add(threading.get_ident())
        if self.delay:
            cancel.wait(self.delay)
            if cancel.is_set():
                raise uia.Cancelled()
        n = self.counts.pop(0) if self.counts else 10
        return list(self.lines), n

    def close(self, ctx):
        self.closed += 1


def _reader(backend, **cfg):
    cfg.setdefault("first_touch_retry_s", 0)
    return UiaReader(UiaConfig(**cfg), backend=backend, win32=FakeWin())


def test_read_ok_runs_off_caller_thread():
    be = FakeBackend()
    r = _reader(be)
    res = r.read(1)
    assert res.status == "ok" and res.usable and res.element_count == 10 and res.window_rect == WIN
    assert threading.get_ident() not in be.threads
    r.read(1)
    assert be.opened == 1  # 同一个工作线程复用
    r.close()


def test_timeout_abandons_worker_and_recovers():
    be = FakeBackend(delay=5.0)
    r = _reader(be, timeout_s=0.1)
    t0 = time.perf_counter()
    res = r.read(1)
    assert res.status == "timeout" and not res.usable and res.lines == []
    assert time.perf_counter() - t0 < 0.5
    # 被放弃的线程拿到取消标记后自己退出
    end = time.monotonic() + 3
    while r.alive_abandoned() and time.monotonic() < end:
        time.sleep(0.02)
    assert r.alive_abandoned() == 0
    be.delay = 0
    assert r.read(1).status == "ok"
    assert be.opened == 2
    r.close()


def test_busy_when_too_many_stuck_threads():
    class Stuck(FakeBackend):
        def read(self, ctx, hwnd, win_rect, cancel):
            time.sleep(1.0)  # 不理会取消：模拟卡在跨进程调用里
            return [], 0

    r = _reader(Stuck(), timeout_s=0.05, max_abandoned=2)
    assert r.read(1).status == "timeout"
    assert r.read(1).status == "timeout"
    t0 = time.perf_counter()
    res = r.read(1)
    assert res.status == "busy" and time.perf_counter() - t0 < 0.05


def test_minimized_and_closed_windows_are_not_read():
    be = FakeBackend()
    r = _reader(be)
    r.win.iconic = True
    assert r.read(1).status == "minimized"
    r.win.iconic = False
    r.win.alive = False
    assert r.read(1).status == "no_window"
    assert be.reads == 0


def test_unusable_when_only_ui_text():
    be = FakeBackend(lines=[UiaLine("最小化", (1, 1, 2, 2), "Button", "name", "ui"), UiaLine("短", (1, 1, 2, 2), "Text", "name")])
    r = _reader(be)
    res = r.read(1)
    assert res.status == "ok" and not res.usable
    r.close()


def test_backend_error_reported():
    class Broken(FakeBackend):
        def read(self, ctx, hwnd, win_rect, cancel):
            raise RuntimeError("COMError")

    res = _reader(Broken()).read(1)
    assert res.status == "error" and "COMError" in res.error


def test_has_text_hint_streak_and_ttl():
    now = [0.0]
    be = FakeBackend(lines=[])
    r = UiaReader(UiaConfig(hint_fail_streak=3, hint_ttl_s=60, first_touch_retry_s=0), backend=be,
                  win32=FakeWin(), clock=lambda: now[0])
    for _ in range(2):
        r.read(1)
        assert r.has_text_hint(1)
    r.read(1)
    assert r.has_text_hint(1) is False
    now[0] += 61
    assert r.has_text_hint(1)
    be.lines = FakeBackend().lines
    r.read(1)  # 读到可用文字：清零
    now[0] = 0
    assert r.has_text_hint(1)
    r.close()


def test_result_carries_uncovered_bands():
    be = FakeBackend(lines=[UiaLine("一段足够长的正文文字 enough text", (110, 110, 400, 130), "Text", "name")])
    res = _reader(be).read(1)
    assert res.usable and res.uncovered == ((100, 130, 900, 700),)


def test_slow_init_is_busy_not_abandoned_and_not_counted():
    class SlowOpen(FakeBackend):
        def open(self):
            time.sleep(0.3)  # 进程里第一次初始化 COM / 生成接口代码
            return super().open()

    be = SlowOpen()
    r = _reader(be, timeout_s=0.1, hint_fail_streak=1)
    res = r.read(1)
    assert res.status == "busy" and "初始化" in res.error
    assert r.alive_abandoned() == 0 and r.has_text_hint(1)  # 不算目标程序卡住，也不算"没有文字"
    time.sleep(0.35)
    res = r.read(1)
    assert res.status == "ok" and be.opened == 1 and be.reads == 1  # 被取消的那次没有再去读
    r.close()


def test_concurrent_reader_never_waits_past_its_budget():
    be = FakeBackend(delay=0.6)
    r = _reader(be, timeout_s=1.0)
    t = threading.Thread(target=r.read, args=(1,))
    t.start()
    time.sleep(0.05)
    t0 = time.perf_counter()
    res = r.read(2, timeout_s=0.1)  # 另一个线程正占着读取器
    assert res.status == "busy" and time.perf_counter() - t0 < 0.3
    t.join()
    r.close()


def test_timed_out_first_touch_is_retried_later():
    be = FakeBackend(delay=5.0)
    r = UiaReader(UiaConfig(timeout_s=0.1, first_touch_retry_s=0.05), backend=be, win32=FakeWin())
    assert r.read(1).status == "timeout"
    be.delay = 0
    be.counts = [5, 40]
    res = r.read(1, timeout_s=1.0)
    assert be.reads == 3 and res.element_count == 40  # 这次仍按第一次访问处理：重试并取元素多的那次
    r.close()


def test_hint_stays_true_for_apps_that_gave_text():
    be = FakeBackend()
    r = _reader(be, hint_fail_streak=2)
    assert r.read(1).usable
    be.lines = []  # 同一程序的另一个页面是图片 / 画布：读不出字
    for _ in range(5):
        assert not r.read(1).usable
    assert r.has_text_hint(1)
    r.close()


def test_first_touch_retry_keeps_richer_read():
    be = FakeBackend(counts=[5, 40, 7])
    r = UiaReader(UiaConfig(timeout_s=1.0, first_touch_retry_s=0.05), backend=be, win32=FakeWin())
    res = r.read(1)
    assert be.reads == 2 and res.element_count == 40
    res = r.read(1)  # 第二次访问不再重试
    assert be.reads == 3 and res.element_count == 7
    r.close()
