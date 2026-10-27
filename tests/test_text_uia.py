"""UIA 读字的纯逻辑与线程行为：可见过滤、去重、超时放弃、提示缓存（不碰真实窗口）。"""

from __future__ import annotations

import threading
import time

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


def test_first_touch_retry_keeps_richer_read():
    be = FakeBackend(counts=[5, 40, 7])
    r = UiaReader(UiaConfig(timeout_s=1.0, first_touch_retry_s=0.05), backend=be, win32=FakeWin())
    res = r.read(1)
    assert be.reads == 2 and res.element_count == 40
    res = r.read(1)  # 第二次访问不再重试
    assert be.reads == 3 and res.element_count == 7
    r.close()
