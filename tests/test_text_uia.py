"""UIA 读字的纯逻辑与线程行为：可见过滤、去重、超时放弃（不碰真实窗口）。"""

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
    assert by["地址和搜索栏"].role == "ui"  # 输入框的名字是它的标签，屏幕上画的是值
    assert uia.content_chars(lines) == len("example.test/page")


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


def test_text_drawn_over_the_document_from_outside_it_is_ui():
    """浏览器的翻译提示、弹出气泡：不在正文（产出了可见行的 TextPattern 元素）里，却画在正文上面 → ui，
    不当成正文存（每张截图都会重复一遍）。正文正上方、不在正文里的字（工具栏下的提示条）也是程序的界面
    （录制时本来就按"正文上方的固定区域"不收）；正文旁边的字（侧栏）照旧。"""
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Document", name="页面", rect=(100, 200, 700, 700), has_tp=True),
        Elem(0, "Pane", rect=(600, 210, 690, 330)),
        Elem(2, "Text", name="翻译 英语 页面?", rect=(610, 220, 680, 250)),
        Elem(0, "Text", name="状态栏文字 outside the page", rect=(110, 150, 600, 180)),
        Elem(0, "Text", name="侧栏文字 beside the page", rect=(720, 300, 890, 330)),
    ]
    tps = [TpLines(1, [("合成正文的一行 visible line", [(110, 220, 590, 250)])])]
    by = {ln.text: ln for ln in collect_lines(elems, tps, WIN)}
    assert by["翻译 英语 页面?"].role == "ui"
    assert by["状态栏文字 outside the page"].role == "ui"
    assert by["侧栏文字 beside the page"].role == "text"
    assert by["合成正文的一行 visible line"].role == "text"


BROWSER = (0, 0, 1350, 1140)


def _browser(doc: str | None):
    """合成的浏览器控件树，结构照着临时配置 Edge 实测的样子（不含任何真实文字）。
    doc：None = 页面还没进辅助功能树；'empty' = 文档元素在了但还没有可见行（加载的第一次读字）；'lines' = 正常。"""
    elems = [
        Elem(-1, "Window", name="合成页面 - 浏览器", rect=BROWSER),                                  # 0
        Elem(0, "Pane", rect=(2, 0, 1331, 1130)),                                                    # 1
        Elem(1, "Tab", name="标签页条", rect=(5, 0, 1124, 62)),                                       # 2
        Elem(2, "TabItem", name="合成页面标题 synthetic page", rect=(50, 0, 434, 62)),                # 3
        Elem(1, "ToolBar", name="导航", rect=(2, 60, 1331, 120)),                                     # 4
        Elem(4, "Group", rect=(200, 66, 1120, 112)),                                                 # 5
        Elem(5, "Edit", name="地址和搜索栏", value="file:///synthetic/page.html", rect=(206, 72, 1115, 108),
             has_tp=True),                                                                           # 6
        Elem(1, "Pane", rect=(250, 95, 950, 145)),                                                   # 7 地址栏下弹出的提示
        Elem(7, "Text", name="合成的弹出提示 a synthetic bubble", rect=(260, 99, 806, 141), has_tp=True),  # 8
        Elem(7, "Hyperlink", name="了解详情", rect=(812, 99, 938, 141)),                              # 9
        Elem(1, "Pane", rect=(66, 64, 470, 135)),                                                    # 10 标签页悬停卡片
        Elem(10, "Text", name="合成页面标题 synthetic page", rect=(66, 68, 462, 92), has_tp=True),    # 11
        Elem(10, "Text", name="synthetic.test", rect=(102, 104, 462, 131), has_tp=True),             # 12
        Elem(1, "Pane", rect=(1331, 120, 1350, 1124)),                                               # 13 侧栏
        Elem(13, "ListItem", name="侧栏里的合成条目 side item", rect=(1332, 300, 1349, 330)),          # 14
    ]
    tps = [TpLines(6, []), TpLines(8, []), TpLines(11, []), TpLines(12, [])]
    body = []
    if doc is not None:
        elems.append(Elem(1, "Document", name="合成页面标题 synthetic page", value="file:///synthetic/page.html",
                          rect=(8, 120, 1325, 1124), has_tp=True))                                    # 15
        for i in range(5):
            text = f"合成正文第{i}段 synthetic paragraph {i}"
            elems.append(Elem(15, "Text", name=text, rect=(30, 140 + 40 * i, 900, 170 + 40 * i)))
            body.append((text, [(30, 140 + 40 * i, 900, 170 + 40 * i)]))
        tps.append(TpLines(15, body if doc == "lines" else []))
    return elems, tps


def test_browser_chrome_is_ui_on_every_read_of_a_page_load():
    """页面加载的第一次读字（文档还没有可见行，甚至文档元素还不在）：标签页标题、地址栏的名字和网址、
    文档自己的名字和网址、地址栏下弹出的提示、标签页的悬停卡片都不是正文。正文和侧栏照收。"""
    want = {f"合成正文第{i}段 synthetic paragraph {i}" for i in range(5)}
    for state in (None, "empty", "lines"):
        elems, tps = _browser(state)
        lines = collect_lines(elems, tps, BROWSER)
        text = {ln.text for ln in lines if ln.role == "text"}
        body = want if state else set()
        assert text == body | {"侧栏里的合成条目 side item"}, state
        assert uia.chrome_bottom(elems, BROWSER) == 120
    # 正文行的位置和来源不变（有可见行时来自 TextPattern）
    elems, tps = _browser("lines")
    got = [ln for ln in collect_lines(elems, tps, BROWSER) if ln.role == "text" and "合成正文" in ln.text]
    assert len(got) == 5 and all(ln.source_detail == "textpattern:Document" for ln in got)


def test_chrome_bars_need_to_be_horizontal_strips_outside_the_document():
    """整个窗口大小的 Tab 控件（对话框的分页）不是界面条，里面的内容照收；网页里的工具栏也不算。"""
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Tab", name="分页", rect=(100, 100, 900, 700)),
        Elem(1, "TabItem", name="常规", rect=(110, 100, 180, 130)),
        Elem(1, "Text", name="分页里的合成说明文字", rect=(120, 140, 600, 160)),
    ]
    by = {ln.text: ln for ln in collect_lines(elems, [], WIN)}
    assert uia.chrome_bottom(elems, WIN) is None
    assert by["分页里的合成说明文字"].role == "text" and by["常规"].role == "ui"
    page = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Document", rect=(100, 100, 900, 700), has_tp=True),
        Elem(1, "ToolBar", rect=(100, 100, 900, 140)),
        Elem(2, "Text", name="网页工具栏里的合成字", rect=(110, 105, 400, 135)),
    ]
    assert uia.chrome_bottom(page, WIN) is None
    assert {ln.text: ln.role for ln in collect_lines(page, [TpLines(1, [])], WIN)}["网页工具栏里的合成字"] == "text"


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


def test_caption_height_of_non_window_is_zero():
    assert uia.caption_height(0) == 0  # 不是窗口：0，照旧整帧


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


def test_slow_init_is_busy_not_abandoned_and_not_counted():
    class SlowOpen(FakeBackend):
        def open(self):
            time.sleep(0.3)  # 进程里第一次初始化 COM / 生成接口代码
            return super().open()

    be = SlowOpen()
    r = _reader(be, timeout_s=0.1)
    res = r.read(1)
    assert res.status == "busy" and "初始化" in res.error
    assert r.alive_abandoned() == 0  # 不算目标程序卡住
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


def test_slow_first_touch_without_room_to_retry_is_flagged():
    """第一次读本身就慢、预算里放不下"稍等再读"：结果标 first_touch，调用方（录制进程的读字线程）自己重读；
    之后的读取不再标。"""
    be = FakeBackend(delay=0.3)
    r = UiaReader(UiaConfig(timeout_s=0.5, first_touch_retry_s=0.2), backend=be, win32=FakeWin())
    res = r.read(1)
    assert res.status == "ok" and res.first_touch and be.reads == 1
    be.delay = 0
    assert r.read(1).first_touch is False
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


def test_doc_url_takes_the_largest_document_value():
    """最大的 Document 元素的值就是页面网址；页面里嵌的小文档、不像网址的值、密码框都不算。"""
    elems = [
        Elem(-1, "Window", rect=WIN),
        Elem(0, "Document", value="https://small.test/frame", rect=(110, 110, 300, 200), has_tp=True),
        Elem(0, "Document", value="https://page.test/a?b=1#c", rect=(100, 130, 900, 700), has_tp=True),
        Elem(0, "Document", value="C:\\not\\a\\url.txt", rect=(0, 0, 2000, 2000)),
        Elem(0, "Document", value="file:///secret", rect=(0, 0, 3000, 3000), password=True),
        Elem(0, "Edit", value="https://address.bar/", rect=(0, 0, 4000, 4000)),
    ]
    assert uia.doc_url(elems) == "https://page.test/a?b=1#c"
    assert uia.doc_url([Elem(-1, "Window", rect=WIN), Elem(0, "Document", value="合成的正文", rect=WIN)]) is None
    assert uia.doc_url([Elem(0, "Document", value="about:blank", rect=WIN)]) == "about:blank"
    elems, _ = _browser("lines")
    assert uia.doc_url(elems) == "file:///synthetic/page.html"


def test_doc_url_is_threaded_through_the_reader():
    class WithUrl(FakeBackend):
        def read(self, ctx, hwnd, win_rect, cancel):
            lines, n = super().read(ctx, hwnd, win_rect, cancel)
            return lines, n, 130, "https://synthetic.test/page"

    res = _reader(WithUrl()).read(1)
    assert res.doc_url == "https://synthetic.test/page" and res.chrome_bottom == 130
    assert _reader(FakeBackend()).read(1).doc_url is None  # 只给两项的后端：None


def test_url_found_before_walk_survives_timeout():
    """读整棵树之前先单独找网址（backend.doc）：大页面正文超时，网址和网页标题照样给；整棵树读完时以树里最大的为准。"""
    class Docs(FakeBackend):
        def doc(self, ctx, hwnd):
            return "https://synthetic.test/big", "合成长文"

    be = Docs(delay=5.0)
    res = _reader(be, timeout_s=0.1).read(1)
    assert res.status == "timeout" and res.lines == []
    assert (res.doc_url, res.doc_name) == ("https://synthetic.test/big", "合成长文")

    class DocsAndTree(Docs):
        def read(self, ctx, hwnd, win_rect, cancel):
            lines, n = FakeBackend.read(self, ctx, hwnd, win_rect, cancel)
            return lines, n, None, "https://synthetic.test/largest", "树里的标题"

    res = _reader(DocsAndTree()).read(1)
    assert res.status == "ok" and (res.doc_url, res.doc_name) == ("https://synthetic.test/largest", "树里的标题")

    class BrokenDoc(FakeBackend):
        def doc(self, ctx, hwnd):
            raise RuntimeError("COMError")

    res = _reader(BrokenDoc()).read(1)
    assert res.status == "ok" and res.doc_url is None  # 单独找出错不影响读字


def test_doc_info_largest_document_value_and_name():
    elems = [uia.Elem(-1, "Window", rect=(0, 0, 1000, 800)),
             uia.Elem(0, "Document", name="小框", value="https://ad.test/x", rect=(0, 0, 100, 100)),
             uia.Elem(0, "Document", name=" 页面标题 ", value="https://page.test/a", rect=(0, 0, 900, 700)),
             uia.Elem(0, "Document", name="不是网址", value="一大段\n全文", rect=(0, 0, 1000, 800))]
    assert uia.doc_info(elems) == ("https://page.test/a", "页面标题")
    # 没有大小的 Document（藏起来的 edge:// 小对话框）不算，哪怕它排在前面、页面还没出来
    hidden = [uia.Elem(-1, "Document", name="对话框", value="edge://sync-confirmation-dialog", rect=(0, 0, 0, 0))]
    assert uia.doc_info(hidden) == (None, None) and uia.doc_info(hidden + elems[2:3])[0] == "https://page.test/a"
    assert uia.doc_url(elems) == "https://page.test/a"
    assert uia.url_value("  app://x/index.html ") == "app://x/index.html" and uia.url_value("C:\\x") is None


def test_first_touch_retry_keeps_richer_read():
    be = FakeBackend(counts=[5, 40, 7])
    r = UiaReader(UiaConfig(timeout_s=1.0, first_touch_retry_s=0.05), backend=be, win32=FakeWin())
    res = r.read(1)
    assert be.reads == 2 and res.element_count == 40
    res = r.read(1)  # 第二次访问不再重试
    assert be.reads == 3 and res.element_count == 7
    r.close()


def test_timeout_keeps_document_top_as_chrome_bottom():
    """整棵树超时：读字前找到的 Document 上沿当界面条下沿（OCR 不收标签页条 / 地址栏里的字）。"""
    class Docs(FakeBackend):
        def doc(self, ctx, hwnd):
            return "https://synthetic.test/big", "合成长文", 180

    res = _reader(Docs(delay=5.0), timeout_s=0.1).read(1)
    assert res.status == "timeout" and res.chrome_bottom == 180


def test_doc_info_prefers_outer_document_of_same_size():
    """Chrome 的 PDF 查看器（实测）：外层 Document 的值是 PDF 网址，里面套一层同样大小的 chrome-extension:// 查看器页面，
    再里面是没有值的 PDF 文档。一样大取树里排在前面的（外层），网址是 PDF 的、不会被当成内部页。"""
    r = (101, 221, 2000, 1430)
    elems = [uia.Elem(-1, "Window", rect=(90, 90, 2010, 1440)),
             uia.Elem(0, "Document", value="https://arxiv.example/pdf/1706.03762", rect=r),
             uia.Elem(1, "Document", value="chrome-extension://abcdefghijklmnop/index.html", rect=r),
             uia.Elem(2, "Document", name="包含 15 页内容的 PDF 文档", rect=(645, 305, 1877, 24364))]
    assert uia.doc_info(elems) == ("https://arxiv.example/pdf/1706.03762", None)
    local = [elems[0], uia.Elem(0, "Document", value="file:///C:/Docs/%E5%90%88.pdf", rect=r), elems[2]]
    assert uia.doc_info(local)[0] == "file:///C:/Docs/%E5%90%88.pdf"


def test_link_value_is_its_target_not_drawn_text():
    """链接的名字是屏幕上的字，值是它指向的网址（屏幕上没画）：只收名字。"""
    elems = [Elem(-1, "Window", rect=WIN),
             Elem(0, "Hyperlink", name="合成链接文字", value="http://127.0.0.1:8611/#/entry/14", rect=(120, 200, 300, 220)),
             Elem(0, "Edit", name="合成输入框", value="合成输入框里打的字", rect=(120, 300, 500, 320))]
    got = _texts(collect_lines(elems, [], WIN))
    assert "合成链接文字" in got and "http://127.0.0.1:8611/#/entry/14" not in got and "合成输入框里打的字" in got


def test_switched_away_tab_left_in_tree_is_not_the_page():
    """Chrome（实测）：切走的标签页的网页还留在树里、不标看不见、和正在看的一样大、排在前面。
    按窗口标题认出正在看的那个：网址取它的，切走的那个网页里的字不收。"""
    big = (527, 248, 2288, 1313)
    elems = [uia.Elem(-1, "Window", name="合成页 B - 浏览器", rect=(520, 71, 2295, 1320)),
             uia.Elem(0, "Document", name="合成页 A", value="https://a.test/", rect=(527, 71, 2288, 1136), has_tp=True),
             uia.Elem(1, "Text", name="切走的 A 页上的字", rect=(600, 300, 900, 330)),
             uia.Elem(0, "Document", name="合成页 B", value="http://127.0.0.1:8611/", rect=big, has_tp=True),
             uia.Elem(3, "Document", name="B 里嵌的框", value="https://frame.test/", rect=(600, 400, 900, 600)),
             uia.Elem(4, "Text", name="B 里嵌的框里的字", rect=(610, 410, 890, 440)),
             uia.Elem(3, "Text", name="正在看的 B 页上的字", rect=(600, 700, 900, 730))]
    assert uia.page_doc(elems, elems[0].name) == 3
    assert uia.doc_info(elems, elems[0].name) == ("http://127.0.0.1:8611/", "合成页 B")
    assert uia.doc_info(elems)[0] == "https://a.test/"  # 不给标题：照旧取一样大里排前面的
    uia.hide_other_pages(elems, 3)
    got = _texts(collect_lines(elems, [], (520, 71, 2295, 1320)))
    assert "正在看的 B 页上的字" in got and "B 里嵌的框里的字" in got
    assert "切走的 A 页上的字" not in got
    # 标题对不上任何网页（没给标题的页面）：在全部里挑最大的
    assert uia.page_doc(elems, "别的窗口标题") == 1


def test_window_names_root_and_direct_children():
    """窗口根元素和直接子元素的名字（Chrome 无痕窗口：根下面的 Pane 名字带"（无痕）"，窗口标题不带）。"""
    elems = [uia.Elem(-1, "Window", name="合成页 - 浏览器"),
             uia.Elem(0, "Pane", name=" 合成页 - 浏览器（无痕） "),
             uia.Elem(1, "Button", name="无痕模式"),
             uia.Elem(0, "Pane", name="合成页 - 浏览器"),
             uia.Elem(0, "Pane", name="")]
    assert uia.window_names(elems) == ("合成页 - 浏览器", "合成页 - 浏览器（无痕）")
    assert uia.window_names([]) == ()


def test_window_names_from_doc_and_tree_reach_result():
    """读字前单独找的（doc 第四项）和整棵树里的（read 第六项）合起来给调用方；正文超时也有。"""
    class Names(FakeBackend):
        def doc(self, ctx, hwnd):
            return "https://synthetic.test/a", "合成页", 100, ("合成页 - 浏览器", "合成页 - 浏览器（无痕）")

    res = _reader(Names(delay=5.0), timeout_s=0.1).read(1)
    assert res.status == "timeout" and res.window_names == ("合成页 - 浏览器", "合成页 - 浏览器（无痕）")

    class Both(Names):
        def read(self, ctx, hwnd, win_rect, cancel):
            lines, n = FakeBackend.read(self, ctx, hwnd, win_rect, cancel)
            return lines, n, None, "https://synthetic.test/a", "合成页", ("合成页 - 浏览器", "树里的名字")

    res = _reader(Both()).read(1)
    assert res.window_names == ("合成页 - 浏览器", "合成页 - 浏览器（无痕）", "树里的名字")
    assert _reader(FakeBackend()).read(1).window_names == ()
