"""UI Automation 读字：窗口"静止"后立刻读一次屏幕上看得见的文字（v2 设计"文字"一节）。

实现来自 experiments/uia（RESULTS.md）的实测结论：
- comtypes 直接调系统 UIAutomationCore（CUIAutomation8 + IUIAutomation2 超时），整棵控件树用一次
  缓存请求取回，再在本进程里遍历，避免逐个元素跨进程调用；
- 只收"屏幕上看得见"的：去掉标了屏外的、矩形不和窗口相交的、密码框、空文字；
  文档和终端只取 TextPattern 的"可见范围"逐行文字，不取 Value / 全文范围（那是整篇，含滚走的部分）；
- 窗口最小化时直接不读（浏览器不会把元素标成屏外）；
- 文字先去掉格式控制字符（资源管理器日期里的 U+200E 这类）；
- 顺手记下最大的 Document 元素的值（浏览器里是当前网址，打开本地文件时是 file:/// 地址）和名字（网页标题）：
  UiaResult.doc_url / doc_name。读整棵树之前先单独找一次所有 Document（很快），整棵树超时时网址照样有。

线程与超时：每次读取放到独立的工作线程（各自初始化 COM，MTA），调用方最多等 timeout_s；
目标程序卡死时放弃那个线程（它带着取消标记，之后自己退出），下次读取换新线程，调用方永远不会被拖住。
被放弃且仍未退出的线程超过上限时直接返回 busy，不再继续开新线程。

要不要再 OCR 由调用方（v3/read.py）按正文字数决定；usable=False 表示程序给不出可用的字。

坐标：UIA 矩形是屏幕坐标；调用进程必须先切到 Per-Monitor V2（winapi.enable_dpi_awareness），
矩形才是物理像素、和 WGC / PrintWindow 截图一致。窗口矩形用 DWMWA_EXTENDED_FRAME_BOUNDS，
与 WGC 帧的范围相同。

本模块自带最小的 user32 / dwmapi 调用，不依赖 screenrecall.winapi（单元测试会把它换成假的）。
"""

from __future__ import annotations

import ctypes
import dataclasses
import os
import re
import sys
import threading
import time
import types
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)，屏幕物理像素

# ---------- UIA 常量 ----------

P_RECT, P_CTYPE, P_NAME, P_AID, P_CLASS = 30001, 30003, 30005, 30011, 30012
P_PASSWORD, P_OFFSCREEN, P_FRAMEWORK = 30019, 30022, 30024
P_HAS_TEXT, P_HAS_VALUE, P_VALUE = 30040, 30043, 30045
TEXT_PATTERN = 10014
TREE_SCOPE_SUBTREE = 7  # Element | Children | Descendants
TREE_SCOPE_DESCENDANTS = 4
CT_DOCUMENT = 50030
TEXT_UNIT_LINE = 3

CTYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}

# 正文类角色：名字通常就是屏幕上画出来的字。按钮 / 菜单 / 窗口等的名字多是图标提示，屏幕上未必有，
# 这类行照样返回，但 role 记为 "ui"，不计入"能不能用 UIA 读字"的判断
CONTENT_CT = frozenset({
    "Text", "Document", "Edit", "Hyperlink", "ListItem", "DataItem", "TreeItem", "TabItem",
    "HeaderItem", "Header", "ComboBox", "CheckBox", "RadioButton", "Table", "Custom",
})

# Value 是"整篇全文"的判断：带 TextPattern 的元素，值含换行或很长（单行短值如地址栏、单元格照常收）
_LONG_VALUE = 500


# ---------- 结果 ----------

@dataclass(frozen=True)
class UiaLine:
    text: str
    rect: Rect  # 屏幕物理像素，已裁到窗口矩形内
    control_type: str
    source_detail: str  # textpattern:<控件类型> / name / value
    role: str = "text"  # text：正文类；ui：按钮、菜单等界面文字


@dataclass(frozen=True)
class UiaResult:
    status: str  # ok / empty / timeout / busy / minimized / no_window / error
    lines: list[UiaLine]
    usable: bool  # False：这个窗口给不出可用的 UIA 文字，调用方应登记待 OCR
    elapsed_ms: float
    element_count: int = 0
    window_rect: Rect | None = None
    error: str | None = None
    # 窗口上部界面条（标签页条、工具栏）的最下沿，屏幕 y（chrome_bottom）；OCR 的行只收它下面的
    chrome_bottom: int | None = None
    # 这是对这个窗口的第一次读取，预算不够、没有按 first_touch_retry_s 再读一次：浏览器这时常常只给外框
    # （没有页面），调用方（自己的线程，不怕多等）应当稍等再读一次
    first_touch: bool = False
    # 最大的 Document 元素（ValuePattern）的值，形如 http(s)://… / file:///…：浏览器里就是当前页面的网址；
    # 取不到时 None。整棵树读完时取最大的那个（doc_info()）；整棵树来不及读完（超时）时用读字前先单独找的
    # 最大的 Document（ComBackend.doc，实测 40–260 ms），网址不跟着正文一起丢。密码框的值从来不取
    doc_url: str | None = None
    doc_name: str | None = None  # 那个 Document 的名字：浏览器里是网页自己的标题（不带"- 个人 - Microsoft Edge"）

    @property
    def content_chars(self) -> int:
        return content_chars(self.lines)


@dataclass(frozen=True)
class UiaConfig:
    timeout_s: float = 0.5  # 调用方最多等这么久
    connection_timeout_ms: int = 1000  # IUIAutomation2：被放弃的线程也会在这之后返回
    transaction_timeout_ms: int = 2000
    max_elements: int = 4000  # 遍历上限（缓存树里取前 N 个）
    max_text_patterns: int = 8  # 最外层 TextPattern 元素最多读几个
    max_lines_per_range: int = 400
    min_content_chars: int = 16  # 正文类文字少于这么多字：判为"给不出可用文字"
    first_touch_retry_s: float = 0.2  # 浏览器第一次被访问只给外框，约 200 ms 后才完整
    max_abandoned: int = 3  # 被放弃仍未退出的线程上限
    comtypes_cache_dir: str | None = None  # comtypes 生成的接口代码放这里，默认 %LOCALAPPDATA%\ScreenRecall\comtypes_cache


# ---------- 纯逻辑：可见过滤与整理（单元测试直接覆盖） ----------

@dataclass
class Elem:
    """缓存树里一个元素的属性快照。parent 是父元素下标（根为 -1）。"""

    parent: int
    ct: str
    name: str = ""
    value: str = ""
    rect: Rect = (0, 0, 0, 0)
    offscreen: bool = False
    password: bool = False
    has_tp: bool = False


@dataclass
class TpLines:
    """一个最外层 TextPattern 元素的可见范围逐行结果：[(文字, [矩形...]), ...]。"""

    elem: int
    lines: list[tuple[str, list[Rect]]] = field(default_factory=list)


def clean_text(s: str) -> str:
    """去掉格式控制字符（U+200E 等）与首尾空白；制表符换成空格。保留中文原文，不做大小写变化。"""
    if not s:
        return ""
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    return s.replace("\t", " ").strip()


def _split_lines(s: str) -> list[str]:
    out = []
    for part in s.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        t = clean_text(part)
        if t:
            out.append(t)
    return out


def _norm_key(s: str) -> str:
    """比较用：NFKC + casefold + 去空白。"""
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if not ch.isspace())


def _intersect(a: Rect, b: Rect) -> Rect | None:
    l, t = max(a[0], b[0]), max(a[1], b[1])
    r, btm = min(a[2], b[2]), min(a[3], b[3])
    if r <= l or btm <= t:
        return None
    return (l, t, r, btm)


def _union(rects: Sequence[Rect]) -> Rect | None:
    rs = [r for r in rects if r[2] > r[0] and r[3] > r[1]]
    if not rs:
        return None
    return (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))


def content_chars(lines: Sequence[UiaLine]) -> int:
    return sum(len(_norm_key(ln.text)) for ln in lines if ln.role == "text")


# 子元素里含有一整条可见行（至少这么多字）时，它就是那几行所在的段落：看得见的部分已经由可见行收了
_MIN_COVER_KEY = 8


def _is_covered(key: str, cov: tuple[str, list[str]]) -> bool:
    """文档子元素的文字（已 _norm_key）是否已被文档的可见行收过：它是可见行连起来的一段，
    或者它包含某条可见行（只露出一半的段落，Name 是整段，屏幕外那半不该收）。"""
    joined, long_keys = cov
    return key in joined or any(k in key for k in long_keys)


def _over_document(rect: Rect, elems: Sequence[Elem], tp_set: set[int]) -> bool:
    """rect 的中心落在某个"产出了可见行"的 TextPattern 元素（正文）矩形里。"""
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    for i in tp_set:
        r = elems[i].rect
        if r[0] <= cx < r[2] and r[1] <= cy < r[3]:
            return True
    return False


def _above_document(rect: Rect, elems: Sequence[Elem], docs: set[int]) -> bool:
    """rect 的中心在某个正文区域（_doc_panes）的正上方：横向落在它的范围里、纵向在它顶边以上。"""
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    for i in docs:
        r = elems[i].rect
        if r[0] <= cx < r[2] and cy < r[1]:
            return True
    return False


def _tp_ancestor(elems: Sequence[Elem], idx: int, tp_set: set[int]) -> int:
    """idx 的最近一个"产出了可见行"的 TextPattern 祖先（不含自身），没有返回 -1。"""
    p = elems[idx].parent
    while p >= 0:
        if p in tp_set:
            return p
        p = elems[p].parent
    return -1


# 程序自己的界面条（标签页条、工具栏、菜单栏、标题栏）。它们里面、或者和它们同一高度的字是界面（标签页标题、
# 地址栏、搜索框、按钮提示），不是正文
_BAR_CT = frozenset({"ToolBar", "MenuBar", "TitleBar", "Tab"})
_CHROME_ANCESTOR_CT = frozenset({"ToolBar", "MenuBar", "TitleBar"})
_DOC_MIN_AREA = 0.15  # 文档元素至少占窗口这么大，才算"正文区域"（不是页面里的一个小输入框）
_BAR_MAX_H = 0.15  # 界面条：高度不超过窗口的这个比例、宽度至少一半、在窗口上部 _BAR_TOP 以内
_BAR_TOP = 0.3


def _area(r: Rect) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _doc_panes(elems: Sequence[Elem], win_rect: Rect) -> set[int]:
    """正文区域：控件类型是 Document、带 TextPattern、看得见并且足够大的元素（浏览器页面、文档编辑区）。
    页面刚开始加载时它还没有可见行，但位置已经在了：它上面和它上面浮着的字是程序的界面。"""
    out = set()
    wa = max(1, _area(win_rect))
    for i, e in enumerate(elems):
        if e.ct != "Document" or not e.has_tp or e.offscreen or e.password:
            continue
        r = _intersect(e.rect, win_rect)
        if r is not None and _area(r) >= _DOC_MIN_AREA * wa:
            out.add(i)
    return out


def _has_ancestor(elems: Sequence[Elem], idx: int, pred: Callable[[int], bool]) -> bool:
    p = elems[idx].parent
    while p >= 0:
        if pred(p):
            return True
        p = elems[p].parent
    return False


def chrome_bottom(elems: Sequence[Elem], win_rect: Rect, docs: set[int] | None = None) -> int | None:
    """窗口上部界面条（标签页条、工具栏、菜单栏）的最下沿（屏幕 y）；没有时 None。

    界面条：控件类型 ToolBar / MenuBar / TitleBar / Tab，横向（宽至少窗口一半、高不超过 15%），顶边在窗口上部
    30% 以内，并且不在正文里（网页里的工具栏不算）。"""
    l, t, r, b = win_rect
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    docs = _doc_panes(elems, win_rect) if docs is None else docs
    bottom = None
    for i, e in enumerate(elems):
        if e.ct not in _BAR_CT or e.offscreen:
            continue
        rr = _intersect(e.rect, win_rect)
        if rr is None:
            continue
        if rr[2] - rr[0] < 0.5 * w or rr[3] - rr[1] > _BAR_MAX_H * h or rr[1] > t + _BAR_TOP * h:
            continue
        if i in docs or _has_ancestor(elems, i, lambda p: p in docs):
            continue
        bottom = rr[3] if bottom is None else max(bottom, rr[3])
    return bottom


# 网址形状：协议（至少两个字母，免得把盘符 C: 当成协议）+ 冒号，后面还有东西
_URL_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:\S")


def url_value(value: str | None) -> str | None:
    """Document 的值像一个网址（scheme:…、单行）时原样（去首尾空白）返回，否则 None。"""
    v = (value or "").strip()
    return v if v and "\n" not in v and _URL_SHAPE.match(v) else None


def doc_info(elems: Sequence[Elem]) -> tuple[str | None, str | None]:
    """最大的 Document 元素的 (Value, 名字)；值不像网址的 Document 不算；没有时 (None, None)。密码框不看。
    多个 Document（页面里嵌的 iframe、PDF 查看器）取面积最大的那个，同样大取先出现的；面积为 0 的不算。"""
    best, best_area = (None, None), -1
    for e in elems:
        if e.ct != "Document" or e.password:
            continue
        v = url_value(e.value)
        if v is None:
            continue
        a = _area(e.rect)
        if a <= 0:  # 没有大小的 Document（藏起来的 edge:// 小对话框、还没排版的页面）不算
            continue
        if a > best_area:
            best, best_area = (v, clean_text(e.name) or None), a
    return best


def doc_url(elems: Sequence[Elem]) -> str | None:
    """最大的 Document 元素的 Value（形如 scheme:…）；没有时 None。"""
    return doc_info(elems)[0]


def outermost_tp(elems: Sequence[Elem]) -> list[int]:
    """只取最外层的 TextPattern 元素，避免嵌套重复（文档里的段落也常带 TextPattern）。"""
    out = []
    for i, e in enumerate(elems):
        if not e.has_tp or e.password:
            continue
        p, nested = e.parent, False
        while p >= 0:
            if elems[p].has_tp:
                nested = True
                break
            p = elems[p].parent
        if not nested:
            out.append(i)
    return out


def collect_lines(elems: Sequence[Elem], tps: Sequence[TpLines], win_rect: Rect) -> list[UiaLine]:
    """把缓存树和 TextPattern 可见行整理成"屏幕上看得见"的行，按从上到下、从左到右排序。

    - TextPattern 可见行优先：每行矩形取各段矩形的并集，裁到窗口内；没有矩形的行用元素矩形；
    - 普通元素：去掉屏外、矩形为空、不和窗口相交、密码框；Name 与 Value 各算一条；
    - 带 TextPattern 的元素：Value 若是整篇全文（多行或很长）不收；产出了可见行时 Name / Value 都不收；
    - 位于"产出了可见行"的文档里的子元素，文字已被可见行覆盖时不重复收；
    - 程序自己的界面（role 'ui'，不当正文存）：标签页标题、文档元素自己的名字和值（网页标题、网址）、输入框的名字
      （它的标签），以及不在正文里的元素中：工具栏 / 菜单栏里的、和窗口上部界面条同一高度的、画在正文区域上面或
      正上方的（悬停卡片、弹出提示）。正文区域在页面还没有可见行时（刚开始加载）就按文档元素的位置算，
      所以页面加载的第一次读字也不会把标签页、地址栏当成正文；
    - 同文字同矩形去重。
    """
    out: list[UiaLine] = []
    covered: dict[int, tuple[str, list[str]]] = {}
    tp_with_lines: set[int] = set()
    for tp in tps:
        e = elems[tp.elem]
        keys = []
        for text, rects in tp.lines:
            rect = _union(rects) or e.rect
            rect = _intersect(rect, win_rect) if rect else None
            if rect is None:
                continue
            for t in _split_lines(text):
                out.append(UiaLine(t, rect, e.ct, f"textpattern:{e.ct}", "text"))
                keys.append(_norm_key(t))
        if keys:
            tp_with_lines.add(tp.elem)
            # 不加分隔：折成几行显示的段落（网页里的 <p>）整段文字也算被覆盖
            covered[tp.elem] = ("".join(keys), [k for k in keys if len(k) >= _MIN_COVER_KEY])

    docs = _doc_panes(elems, win_rect)
    panes = docs | tp_with_lines
    zone = chrome_bottom(elems, win_rect, docs)
    for i, e in enumerate(elems):
        if e.password or e.offscreen or i in tp_with_lines:
            continue
        rect = _intersect(e.rect, win_rect)
        if rect is None:
            continue
        cands: list[tuple[str, str]] = []
        if e.name.strip():
            cands.append((e.name, "name"))
        v = e.value
        if v.strip() and v != e.name:
            whole_doc = e.has_tp and ("\n" in v or "\r" in v or len(v) > _LONG_VALUE)
            if not whole_doc:
                cands.append((v, "value"))
        if not cands:
            continue
        anc = _tp_ancestor(elems, i, tp_with_lines) if tp_with_lines else -1
        role = "text" if e.ct in CONTENT_CT else "ui"
        # 标签页标题、文档自己的名字和值（网页标题、网址）是界面，正文由可见行 / 子元素给出
        if e.ct in ("TabItem", "Document"):
            role = "ui"
        if role == "text" and i not in docs and not (panes and _has_ancestor(elems, i, lambda p: p in panes)):
            if _has_ancestor(elems, i, lambda p: elems[p].ct in _CHROME_ANCESTOR_CT):
                role = "ui"  # 工具栏 / 菜单栏里的字：地址栏、搜索框
            elif zone is not None and rect[1] < zone - 2:
                role = "ui"  # 和窗口上部的界面条同一高度：标签页的悬停卡片、地址栏下面弹出的提示
            elif _over_document(rect, elems, panes) or _above_document(rect, elems, docs):
                role = "ui"  # 不在正文里、却画在正文上面或正文上方：浮层（翻译提示、弹出气泡、通知）
        for raw, src in cands:
            # 输入框的名字是它的标签（"地址和搜索栏"），屏幕上画的是它的值
            r_src = "ui" if src == "name" and e.ct == "Edit" else role
            for t in _split_lines(raw):
                if anc >= 0 and _is_covered(_norm_key(t), covered[anc]):
                    continue
                out.append(UiaLine(t, rect, e.ct, src, r_src))

    seen: set[tuple[str, Rect]] = set()
    uniq = []
    for ln in out:
        k = (ln.text, ln.rect)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(ln)
    uniq.sort(key=lambda ln: (ln.rect[1], ln.rect[0]))
    return uniq


# ---------- 最小的 Win32 调用 ----------

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _Win32:
    def __init__(self):
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        d = ctypes.WinDLL("dwmapi")
        self._IsWindow = u.IsWindow
        self._IsWindow.argtypes, self._IsWindow.restype = [wintypes.HWND], wintypes.BOOL
        self._IsIconic = u.IsIconic
        self._IsIconic.argtypes, self._IsIconic.restype = [wintypes.HWND], wintypes.BOOL
        self._GetWindowRect = u.GetWindowRect
        self._GetWindowRect.argtypes, self._GetWindowRect.restype = [wintypes.HWND, ctypes.POINTER(_RECT)], wintypes.BOOL
        self._Dwm = d.DwmGetWindowAttribute
        self._Dwm.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        self._Dwm.restype = ctypes.c_long

    def is_window(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self._IsWindow(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._IsIconic(hwnd))

    def frame_rect(self, hwnd: int) -> Rect | None:
        """可见边框（DWMWA_EXTENDED_FRAME_BOUNDS，与 WGC 帧范围相同），取不到时用 GetWindowRect。"""
        r = _RECT()
        if self._Dwm(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            if not self._GetWindowRect(hwnd, ctypes.byref(r)):
                return None
        return (r.left, r.top, r.right, r.bottom)

    def client_top(self, hwnd: int) -> int | None:
        """客户区左上角的屏幕 y（物理像素）。"""
        from ctypes import wintypes

        pt = wintypes.POINT(0, 0)
        u = ctypes.windll.user32
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        if not u.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return int(pt.y)


MAX_CAPTION_PX = 200  # 比这还高的"标题栏"不当标题栏（怪异的窗口），整帧照常识别


def caption_height(hwnd: int) -> int:
    """系统画的标题栏有多高（物理像素）：客户区顶边 - 可见边框顶边。自己画标题栏的程序（Edge、VS Code、
    新版记事本）客户区就是整个窗口，得 0；取不到也是 0（照旧整帧识别）。"""
    try:
        w = _win32()
        fr = w.frame_rect(hwnd)
        ct = w.client_top(hwnd)
    except Exception:  # noqa: BLE001
        return 0
    if fr is None or ct is None:
        return 0
    h = ct - fr[1]
    return h if 0 < h <= MAX_CAPTION_PX else 0


_WIN32: _Win32 | None = None


def _win32() -> _Win32:
    global _WIN32
    if _WIN32 is None:
        _WIN32 = _Win32()
    return _WIN32


# ---------- comtypes 后端（在工作线程里运行） ----------

class Backend(Protocol):
    def open(self) -> object: ...
    # 返回 (行, 元素数[, chrome_bottom[, doc_url[, doc_name]]])：后几项可省（测试用的后端只给前两项）
    def read(self, ctx: object, hwnd: int, win_rect: Rect, cancel: threading.Event) -> tuple: ...
    def close(self, ctx: object) -> None: ...
    # 可选：def doc(self, ctx, hwnd) -> (doc_url, doc_name[, 上沿 y])，读整棵树之前先单独找网址（没有这个方法就不找）


class Cancelled(Exception):
    pass


_COM_LOCK = threading.Lock()
_UIA_MOD = None


def _default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "ScreenRecall" / "comtypes_cache"


def _import_uia(cache_dir: str | None):
    """导入 comtypes 并生成 UIAutomationCore 接口模块（每个进程一次）。

    comtypes.client 一导入就会在 site-packages/comtypes/gen 下建包、写生成代码；这里先放一个
    只在内存里的 comtypes.gen 包，路径指向本程序自己的缓存目录，避免写进 site-packages。
    comtypes 导入时会给当前线程初始化 COM：在工作线程里导入，并临时指定 MTA。
    """
    global _UIA_MOD
    with _COM_LOCK:
        if _UIA_MOD is not None:
            return _UIA_MOD
        had_flags = hasattr(sys, "coinit_flags")
        if not had_flags:
            sys.coinit_flags = 0  # COINIT_MULTITHREADED
        try:
            import comtypes

            if "comtypes.client" not in sys.modules and "comtypes.gen" not in sys.modules:
                d = Path(cache_dir) if cache_dir else _default_cache_dir()
                d.mkdir(parents=True, exist_ok=True)
                gen = types.ModuleType("comtypes.gen")
                gen.__path__ = [str(d)]
                gen.__package__ = "comtypes.gen"
                sys.modules["comtypes.gen"] = gen
                comtypes.gen = gen  # 预先放进 sys.modules 的子包不会自动挂到父包上
            import comtypes.client

            _UIA_MOD = comtypes.client.GetModule("UIAutomationCore.dll")
        finally:
            if not had_flags:
                del sys.coinit_flags
        return _UIA_MOD


@dataclass
class _ComCtx:
    auto: object
    cr: object
    uia: object
    doc_cond: object = None   # ControlType == Document
    doc_cr: object = None     # 只取 Value / 名字 / 是否密码框


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _rects(flat) -> list[Rect]:
    flat = list(flat or [])
    out = []
    for i in range(0, len(flat) - 3, 4):
        x, y, w, h = flat[i:i + 4]
        out.append((int(round(x)), int(round(y)), int(round(x + w)), int(round(y + h))))
    return out


class ComBackend:
    """真实的 UIA 读取：一次缓存请求取回整棵控件树，再读最外层 TextPattern 的可见范围。"""

    def __init__(self, cfg: UiaConfig):
        self.cfg = cfg

    def open(self) -> _ComCtx:
        uia = _import_uia(self.cfg.comtypes_cache_dir)
        import comtypes
        import comtypes.client

        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            auto = comtypes.client.CreateObject(uia.CUIAutomation8, interface=uia.IUIAutomation2)
            # 卡死的目标程序：调用最多阻塞到这两个超时，之后被放弃的线程也能退出
            auto.ConnectionTimeout = int(self.cfg.connection_timeout_ms)
            auto.TransactionTimeout = int(self.cfg.transaction_timeout_ms)
        except Exception:
            auto = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
        cr = auto.CreateCacheRequest()
        for pid in (P_RECT, P_CTYPE, P_NAME, P_PASSWORD, P_OFFSCREEN, P_HAS_TEXT, P_HAS_VALUE, P_VALUE):
            cr.AddProperty(pid)
        cr.TreeScope = TREE_SCOPE_SUBTREE
        cr.TreeFilter = auto.ControlViewCondition
        doc_cr = auto.CreateCacheRequest()
        for pid in (P_RECT, P_NAME, P_PASSWORD, P_VALUE):
            doc_cr.AddProperty(pid)
        doc_cond = auto.CreatePropertyCondition(P_CTYPE, CT_DOCUMENT)
        return _ComCtx(auto, cr, uia, doc_cond, doc_cr)

    def close(self, ctx: _ComCtx) -> None:
        import comtypes

        ctx.auto = ctx.cr = None
        import gc

        gc.collect()  # 先释放全部 COM 指针，再反初始化
        uninit = getattr(comtypes, "CoUninitialize", None) or comtypes._CoUninitialize
        uninit()

    def doc(self, ctx: _ComCtx, hwnd: int) -> tuple[str | None, str | None, int | None]:
        """所有 Document 元素里最大的那个的 (网址, 名字, 上沿屏幕 y)，规则和 doc_info 一样：上沿就是浏览器标签页条 /
        地址栏的下沿（整棵树超时、算不出 chrome_bottom 时 OCR 照样不收界面条里的字）。一次 FindAllBuildCache，只取四个属性
        （实测后台 Edge 40–260 ms，而整棵树要 0.6–3 秒）。不能只取第一个：Edge 树里排在前面的可能是藏起来的
        edge:// 小对话框，页面会被当成内部页。"""
        root = ctx.auto.ElementFromHandle(hwnd)
        arr = root.FindAllBuildCache(TREE_SCOPE_DESCENDANTS, ctx.doc_cond, ctx.doc_cr)
        elems = []
        for i in range(arr.Length if arr else 0):
            el = arr.GetElement(i)
            r = el.CachedBoundingRectangle
            elems.append(Elem(parent=-1, ct="Document", name=_str(el.CachedName),
                              value=_str(el.GetCachedPropertyValue(P_VALUE)), password=bool(el.CachedIsPassword),
                              rect=(int(r.left), int(r.top), int(r.right), int(r.bottom))))
        url, name = doc_info(elems)
        top = next((e.rect[1] for e in elems if url and url_value(e.value) == url and _area(e.rect) > 0), None)
        return url, name, top

    def read(self, ctx: _ComCtx, hwnd: int, win_rect: Rect,
             cancel: threading.Event) -> tuple[list[UiaLine], int, int | None, str | None, str | None]:
        root = ctx.auto.ElementFromHandle(hwnd)
        cached = root.BuildUpdatedCache(ctx.cr)
        if cancel.is_set():
            raise Cancelled()
        elems, handles = self._walk(cached, cancel)
        tps = []
        for i in outermost_tp(elems)[: self.cfg.max_text_patterns]:
            if cancel.is_set():
                raise Cancelled()
            tp = TpLines(i)
            try:
                pat = handles[i].GetCurrentPattern(TEXT_PATTERN).QueryInterface(ctx.uia.IUIAutomationTextPattern)
                vr = pat.GetVisibleRanges()
                for k in range(vr.Length if vr else 0):
                    tp.lines.extend(self._range_lines(vr.GetElement(k), cancel))
            except Cancelled:
                raise
            except Exception:  # 各程序的 TextPattern 实现差异很大：读不出就退回普通元素
                tp.lines = []
            tps.append(tp)
        handles.clear()
        url, name = doc_info(elems)
        return collect_lines(elems, tps, win_rect), len(elems), chrome_bottom(elems, win_rect), url, name

    def _walk(self, cached, cancel: threading.Event) -> tuple[list[Elem], list]:
        elems: list[Elem] = []
        handles: list = []
        stack = [(cached, -1)]
        limit = self.cfg.max_elements
        while stack and len(elems) < limit:
            el, parent = stack.pop()
            if len(elems) % 200 == 0 and cancel.is_set():
                raise Cancelled()
            r = el.CachedBoundingRectangle
            e = Elem(
                parent=parent,
                ct=CTYPES.get(el.CachedControlType, str(el.CachedControlType)),
                name=_str(el.CachedName),
                rect=(int(r.left), int(r.top), int(r.right), int(r.bottom)),
                offscreen=bool(el.CachedIsOffscreen),
                password=bool(el.CachedIsPassword),
                has_tp=bool(el.GetCachedPropertyValue(P_HAS_TEXT)),
            )
            # 密码框的值一律不取
            if not e.password and el.GetCachedPropertyValue(P_HAS_VALUE):
                e.value = _str(el.GetCachedPropertyValue(P_VALUE))
            idx = len(elems)
            elems.append(e)
            handles.append(el if e.has_tp else None)
            kids = el.GetCachedChildren()
            if kids:
                # 逆序压栈，出栈时保持文档顺序
                for i in range(kids.Length - 1, -1, -1):
                    stack.append((kids.GetElement(i), idx))
        return elems, handles

    def _range_lines(self, vis, cancel: threading.Event) -> list[tuple[str, list[Rect]]]:
        """把一个可见范围按"行"单位切开，取每行文字与矩形。"""
        cur = vis.Clone()
        cur.MoveEndpointByRange(1, cur, 0)  # End -> Start，折叠到可见范围起点
        out = []
        for _ in range(self.cfg.max_lines_per_range):
            if cancel.is_set():
                raise Cancelled()
            line = cur.Clone()
            line.ExpandToEnclosingUnit(TEXT_UNIT_LINE)
            if line.CompareEndpoints(0, vis, 1) >= 0:  # 行首已越过可见范围末尾
                break
            # 行可能从可见范围之前开始（首行被半截挡住），裁到可见范围内
            if line.CompareEndpoints(0, vis, 0) < 0:
                line.MoveEndpointByRange(0, vis, 0)
            if line.CompareEndpoints(1, vis, 1) > 0:
                line.MoveEndpointByRange(1, vis, 1)
            out.append((_str(line.GetText(-1)), _rects(line.GetBoundingRectangles())))
            prev = cur.Clone()
            if cur.Move(TEXT_UNIT_LINE, 1) == 0 or cur.CompareEndpoints(0, prev, 0) <= 0:
                break
        return out


# ---------- 工作线程与读取器 ----------

@dataclass
class _Job:
    hwnd: int
    win_rect: Rect
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lines: list[UiaLine] | None = None
    count: int = 0
    chrome: int | None = None
    doc_url: str | None = None
    doc_name: str | None = None
    error: str | None = None


class _Worker(threading.Thread):
    def __init__(self, backend: Backend):
        super().__init__(name="uia-reader", daemon=True)
        self.backend = backend
        self.jobs: list[_Job | None] = []
        self.cv = threading.Condition()
        self.abandoned = threading.Event()
        self.ready = threading.Event()  # open() 已结束（成功或失败）
        self.open_error: str | None = None

    def submit(self, job: _Job | None) -> None:
        with self.cv:
            self.jobs.append(job)
            self.cv.notify()

    def run(self) -> None:
        try:
            ctx = self.backend.open()
        except Exception as exc:
            self.open_error = f"{type(exc).__name__}: {exc}"[:300]
            ctx = None
        self.ready.set()
        try:
            while True:
                with self.cv:
                    while not self.jobs:
                        self.cv.wait()
                    job = self.jobs.pop(0)
                if job is None:
                    return
                if job.cancel.is_set():  # 初始化期间调用方已经不等了：不再去读
                    job.error = "cancelled"
                elif ctx is None:
                    job.error = self.open_error or "UIA 初始化失败"
                else:
                    find_doc = getattr(self.backend, "doc", None)
                    if find_doc is not None:
                        # 先单独找网址：整棵树来不及读完时调用方照样拿得到（job 上的值随时可读）
                        try:
                            got = find_doc(ctx, job.hwnd)
                            job.doc_url, job.doc_name = got[0], got[1]
                            job.chrome = got[2] if len(got) > 2 else None
                        except Exception:  # noqa: BLE001 - 找不到就等整棵树里的
                            pass
                    try:
                        if job.cancel.is_set():
                            raise Cancelled()
                        got = self.backend.read(ctx, job.hwnd, job.win_rect, job.cancel)
                        job.lines, job.count = got[0], got[1]
                        if len(got) > 2 and got[2] is not None:  # 测试用的后端只给两项；树里算不出时留着 Document 的上沿
                            job.chrome = got[2]
                        if len(got) > 3 and got[3]:  # 整棵树里最大的那个 Document 为准
                            job.doc_url = got[3]
                            job.doc_name = got[4] if len(got) > 4 else None
                    except Cancelled:
                        job.error = "cancelled"
                    except Exception as exc:
                        job.error = f"{type(exc).__name__}: {exc}"[:300]
                job.done.set()
                if self.abandoned.is_set():
                    return
        finally:
            if ctx is not None:
                try:
                    self.backend.close(ctx)
                except Exception:
                    pass


class UiaReader:
    """线程安全的 UIA 读取器。一个进程用一个实例（录制进程里）。

    read(hwnd) 永远在 timeout_s（加少量调度误差）内返回。
    """

    def __init__(self, cfg: UiaConfig | None = None, *, backend: Backend | None = None,
                 win32: object | None = None, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg or UiaConfig()
        self._backend = backend or ComBackend(self.cfg)
        self._win = win32  # 测试注入；默认用真实 user32
        self._clock = clock
        self._lock = threading.Lock()  # 一次只读一个窗口
        self._worker: _Worker | None = None
        self._abandoned: list[_Worker] = []
        self._seen: OrderedDict[int, None] = OrderedDict()

    @property
    def win(self):
        if self._win is None:
            self._win = _win32()
        return self._win

    # ----- 对外 -----

    def read(self, hwnd: int, timeout_s: float | None = None) -> UiaResult:
        t0 = time.perf_counter()
        budget = self.cfg.timeout_s if timeout_s is None else float(timeout_s)
        win = self.win
        if not win.is_window(hwnd):
            return UiaResult("no_window", [], False, _ms(t0))
        if win.is_iconic(hwnd):
            return UiaResult("minimized", [], False, _ms(t0))
        rect = win.frame_rect(hwnd)
        if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
            return UiaResult("no_window", [], False, _ms(t0))
        # 另一个线程正在读：最多等到自己的预算用完，不能被别人的读取拖过 timeout
        if not self._lock.acquire(timeout=max(0.0, budget - (time.perf_counter() - t0))):
            return UiaResult("busy", [], False, _ms(t0), window_rect=rect, error="另一次读取还没结束")
        try:
            res = self._read_once(hwnd, rect, budget, t0)
            if res.status in ("ok", "empty"):
                first = hwnd not in self._seen
                self._remember(hwnd)  # 只记成功读过的：第一次超时的窗口下次仍按"第一次访问"处理
                if first:
                    # 第一次访问：浏览器等程序此时才打开辅助功能树，稍等再读一次，取元素多的那次
                    spent = time.perf_counter() - t0
                    retry = self.cfg.first_touch_retry_s
                    if retry > 0 and budget - spent > retry + max(0.05, res.elapsed_ms / 1000.0 * 1.5):
                        time.sleep(retry)
                        res2 = self._read_once(hwnd, rect, budget, t0)  # budget 是从 t0 起算的总预算
                        if res2.status in ("ok", "empty") and res2.element_count >= res.element_count:
                            res = res2
                    elif retry > 0:
                        # 第一次读本身就慢（初始化 COM、目标程序刚建辅助功能树），预算里放不下重读：让调用方来
                        res = dataclasses.replace(res, first_touch=True)
        finally:
            self._lock.release()
        return res

    def close(self) -> None:
        with self._lock:
            if self._worker is not None:
                self._worker.submit(None)
                self._worker = None

    def alive_abandoned(self) -> int:
        self._abandoned = [w for w in self._abandoned if w.is_alive()]
        return len(self._abandoned)

    # ----- 内部 -----

    def _read_once(self, hwnd: int, rect: Rect, budget: float, t0: float) -> UiaResult:
        if self._worker is None:
            if self.alive_abandoned() >= self.cfg.max_abandoned:
                return UiaResult("busy", [], False, _ms(t0), window_rect=rect,
                                 error="之前卡住的读取线程还没退出")
            self._worker = _Worker(self._backend)
            self._worker.start()
        job = _Job(hwnd, rect)
        worker = self._worker
        worker.submit(job)
        if not job.done.wait(max(0.0, budget - (time.perf_counter() - t0))):
            job.cancel.set()
            if not worker.ready.is_set():
                # 还在初始化 COM / 生成 comtypes 接口（进程里第一次、慢机器上可能超过预算）：
                # 不是目标程序卡住，线程留着继续初始化，这次答"忙"，也不计入提示的失败次数
                return UiaResult("busy", [], False, _ms(t0), window_rect=rect, error="UIA 正在初始化")
            # 目标程序卡住：放弃这个线程，下次换新的
            worker.abandoned.set()
            worker.submit(None)
            self._abandoned.append(worker)
            self._worker = None
            # 正文来不及读完，但读字前先找到的网址照样给（大页面：网址 + 调用方 OCR）
            return UiaResult("timeout", [], False, _ms(t0), window_rect=rect, doc_url=job.doc_url,
                             doc_name=job.doc_name, chrome_bottom=job.chrome)
        if job.error is not None:
            return UiaResult("error", [], False, _ms(t0), window_rect=rect, error=job.error, doc_url=job.doc_url,
                             doc_name=job.doc_name)
        lines = job.lines or []
        usable = content_chars(lines) >= self.cfg.min_content_chars
        return UiaResult("ok" if lines else "empty", lines, usable, _ms(t0), job.count, rect,
                         chrome_bottom=job.chrome, doc_url=job.doc_url, doc_name=job.doc_name)

    def _remember(self, hwnd: int) -> None:
        self._seen[hwnd] = None
        self._seen.move_to_end(hwnd)
        while len(self._seen) > 256:
            self._seen.popitem(last=False)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 1)
