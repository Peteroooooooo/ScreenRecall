"""UI Automation 读字：窗口"静止"后立刻读一次屏幕上看得见的文字（v2 设计"文字"一节）。

实现来自 experiments/uia（RESULTS.md）的实测结论：
- comtypes 直接调系统 UIAutomationCore（CUIAutomation8 + IUIAutomation2 超时），整棵控件树用一次
  缓存请求取回，再在本进程里遍历，避免逐个元素跨进程调用；
- 只收"屏幕上看得见"的：去掉标了屏外的、矩形不和窗口相交的、密码框、空文字；
  文档和终端只取 TextPattern 的"可见范围"逐行文字，不取 Value / 全文范围（那是整篇，含滚走的部分）；
- 窗口最小化时直接不读（浏览器不会把元素标成屏外）；
- 文字先去掉格式控制字符（资源管理器日期里的 U+200E 这类）。

线程与超时：每次读取放到独立的工作线程（各自初始化 COM，MTA），调用方最多等 timeout_s；
目标程序卡死时放弃那个线程（它带着取消标记，之后自己退出），下次读取换新线程，调用方永远不会被拖住。
被放弃且仍未退出的线程超过上限时直接返回 busy，不再继续开新线程。

待 OCR：usable=False（程序给不出字）时整帧要 OCR；usable=True 时也可能有大片没读到字的区域（浏览器第一次被
访问时只给外框、页面里的图片 / 画布 / 扫描 PDF），结果里的 uncovered 记下这些整宽横条；ocr_regions(result, frame)
再按帧像素去掉空白横条，返回要登记待 OCR 的帧坐标矩形。

坐标：UIA 矩形是屏幕坐标；调用进程必须先切到 Per-Monitor V2（winapi.enable_dpi_awareness），
矩形才是物理像素、和 WGC / PrintWindow 截图一致。窗口矩形用 DWMWA_EXTENDED_FRAME_BOUNDS，
与 WGC 帧的范围相同；换成帧坐标用 to_frame_rect。

本模块自带最小的 user32 / dwmapi 调用，不依赖 screenrecall.winapi（单元测试会把它换成假的）。
"""

from __future__ import annotations

import ctypes
import os
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
    # usable 为 True 时也可能有大片区域没读到字（浏览器第一次被访问只给外框、页面里的图片 / 画布 / 扫描 PDF）：
    # 这些区域是整宽的横条（屏幕物理像素），调用方用 ocr_regions() 结合帧像素决定哪些要登记待 OCR
    uncovered: tuple[Rect, ...] = ()

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
    hint_fail_streak: int = 3  # 同一程序连续这么多次读不出可用文字 -> has_text_hint 返回 False
    hint_ttl_s: float = 600.0  # 判定"没有文字"后，过这么久再试一次
    min_gap_px: int = 48  # 没有任何文字行的横条高于这么多像素，记进 uncovered
    container_frac: float = 0.4  # 高度超过窗口这个比例的行（窗口 / 面板的名字）不算"覆盖"了那片区域
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


def _tp_ancestor(elems: Sequence[Elem], idx: int, tp_set: set[int]) -> int:
    """idx 的最近一个"产出了可见行"的 TextPattern 祖先（不含自身），没有返回 -1。"""
    p = elems[idx].parent
    while p >= 0:
        if p in tp_set:
            return p
        p = elems[p].parent
    return -1


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
    - 同文字同矩形去重。
    """
    out: list[UiaLine] = []
    covered: dict[int, str] = {}
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
            covered[tp.elem] = "\n".join(keys)

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
        for raw, src in cands:
            for t in _split_lines(raw):
                if anc >= 0 and _norm_key(t) in covered[anc]:
                    continue
                out.append(UiaLine(t, rect, e.ct, src, role))

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


def to_frame_rect(rect: Rect, frame_origin: tuple[int, int]) -> Rect:
    """屏幕矩形换成截图（帧）坐标：减去帧左上角（窗口可见边框的左上角）。"""
    ox, oy = frame_origin
    return (rect[0] - ox, rect[1] - oy, rect[2] - ox, rect[3] - oy)


def uncovered_bands(lines: Sequence[UiaLine], win_rect: Rect, min_gap_px: int = 48,
                    container_frac: float = 0.4) -> list[Rect]:
    """窗口里没有任何文字行的整宽横条（屏幕坐标），只返回高于 min_gap_px 的。

    所有行（正文和界面文字）都算覆盖；但高度超过窗口 container_frac 的行（窗口、面板、文档本身的名字）
    不算，否则一个整窗大小的名字就会把整个窗口标成"读到了"。只看纵向，不分左右栏。
    """
    l, t, r, b = win_rect
    h = b - t
    if h <= 0 or r <= l:
        return []
    spans = sorted(
        (max(ln.rect[1], t), min(ln.rect[3], b)) for ln in lines
        if ln.rect[3] > ln.rect[1] and (ln.rect[3] - ln.rect[1]) <= container_frac * h
    )
    out: list[Rect] = []
    cur = t
    for s0, s1 in spans:
        if s0 - cur >= min_gap_px:
            out.append((l, cur, r, s0))
        cur = max(cur, s1)
    if b - cur >= min_gap_px:
        out.append((l, cur, r, b))
    return out


def has_ink(img, *, edge_level: int = 48, min_edges: int = 24) -> bool:
    """这块图上有没有"像字的东西"：横向和纵向的强边缘（相邻像素差 > edge_level）都至少 min_edges 个。

    按个数而不是比例：宽横条里只有一个短词也要算有字。两个方向都要有：一条竖分隔线只有横向边缘、
    一条横线只有纵向边缘，都不算；纯色背景、渐变、Mica 这类轻微纹理也判为空白。
    img：HxWx3/4 的 BGR(A) uint8，或单通道（如录制进程里的绿色通道）。
    """
    import numpy as np

    a = np.asarray(img)
    if a.ndim == 3:
        a = a[:, :, 1]  # 绿色通道近似亮度，和比对用的通道一致
    if a.ndim != 2 or a.shape[0] < 2 or a.shape[1] < 2:
        return False
    a = a.astype(np.int16)
    h = int(np.count_nonzero(np.abs(np.diff(a, axis=1)) > edge_level))
    if h < min_edges:
        return False
    v = int(np.count_nonzero(np.abs(np.diff(a, axis=0)) > edge_level))
    return v >= min_edges


def ocr_regions(res: UiaResult, frame, frame_origin: tuple[int, int] | None = None,
                *, min_gap_px: int = 48) -> list[Rect]:
    """录制进程用：这一帧里哪些区域要登记待 OCR（帧坐标 (l, t, r, b)），空列表表示 UIA 已经够了。

    - 读取失败 / 超时 / 窗口给不出可用文字：整帧（若整帧都是空白则不登记）；
    - 读到了可用文字：只取 uncovered 横条里"有像字的东西"的那些（空白的段落间距、底部留白不登记）。
    frame：这一帧的像素（BGR(A) 或绿色通道）；frame_origin 默认是 res.window_rect 的左上角
    （窗口可见边框 = WGC 帧范围）。
    """
    import numpy as np

    a = np.asarray(frame)
    fh, fw = a.shape[0], a.shape[1]
    whole = [(0, 0, fw, fh)]
    if res.status not in ("ok", "empty") or not res.usable:
        return whole if has_ink(a) else []
    if frame_origin is None:
        if res.window_rect is None:
            return whole if has_ink(a) else []
        frame_origin = (res.window_rect[0], res.window_rect[1])
    out = []
    for band in res.uncovered:
        l, t, r, b = to_frame_rect(band, frame_origin)
        l, t, r, b = max(0, l), max(0, t), min(fw, r), min(fh, b)
        if b - t < min_gap_px or r <= l:
            continue
        if has_ink(a[t:b, l:r]):
            out.append((l, t, r, b))
    return out


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
        self._GetClassNameW = u.GetClassNameW
        self._GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._GetClassNameW.restype = ctypes.c_int
        self._GetPid = u.GetWindowThreadProcessId
        self._GetPid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._GetPid.restype = wintypes.DWORD
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

    def window_key(self, hwnd: int) -> tuple[int, str]:
        """同一程序的近似标识：(进程号, 窗口类名)。"""
        from ctypes import wintypes

        pid = wintypes.DWORD(0)
        self._GetPid(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(256)
        self._GetClassNameW(hwnd, buf, 256)
        return int(pid.value), buf.value


_WIN32: _Win32 | None = None


def _win32() -> _Win32:
    global _WIN32
    if _WIN32 is None:
        _WIN32 = _Win32()
    return _WIN32


# ---------- comtypes 后端（在工作线程里运行） ----------

class Backend(Protocol):
    def open(self) -> object: ...
    def read(self, ctx: object, hwnd: int, win_rect: Rect, cancel: threading.Event) -> tuple[list[UiaLine], int]: ...
    def close(self, ctx: object) -> None: ...


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
        return _ComCtx(auto, cr, uia)

    def close(self, ctx: _ComCtx) -> None:
        import comtypes

        ctx.auto = ctx.cr = None
        import gc

        gc.collect()  # 先释放全部 COM 指针，再反初始化
        uninit = getattr(comtypes, "CoUninitialize", None) or comtypes._CoUninitialize
        uninit()

    def read(self, ctx: _ComCtx, hwnd: int, win_rect: Rect, cancel: threading.Event) -> tuple[list[UiaLine], int]:
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
        return collect_lines(elems, tps, win_rect), len(elems)

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
                    try:
                        job.lines, job.count = self.backend.read(ctx, job.hwnd, job.win_rect, job.cancel)
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

    read(hwnd) 永远在 timeout_s（加少量调度误差）内返回；has_text_hint(hwnd) 不做跨进程调用。
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
        self._hint_lock = threading.Lock()
        self._hint: OrderedDict[tuple[int, str], tuple[int, float]] = OrderedDict()  # key -> (连续失败次数, 最后时间)
        # 读出过可用文字的程序：它的某个页面读不出字（图片页、画布）不代表整个程序不行，提示一直为 True
        self._capable: OrderedDict[tuple[int, str], None] = OrderedDict()

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
        finally:
            self._lock.release()
        self._update_hint(hwnd, res)
        return res

    def has_text_hint(self, hwnd: int) -> bool:
        """便宜的提示：同一程序（进程 + 窗口类）最近连续几次都读不出可用文字时返回 False。

        False 时调用方可以跳过 UIA、直接登记待 OCR；过 hint_ttl_s 后重新返回 True，再试一次。
        本进程里读出过可用文字的程序（如浏览器）一直返回 True：它的某些页面读不出字时由每次结果的
        usable / uncovered 处理，不连累同一程序的其他窗口和页面。
        """
        try:
            key = self.win.window_key(hwnd)
        except Exception:
            return True
        with self._hint_lock:
            if key in self._capable:
                return True
            streak, at = self._hint.get(key, (0, 0.0))
        if streak < self.cfg.hint_fail_streak:
            return True
        return self._clock() - at > self.cfg.hint_ttl_s

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
            return UiaResult("timeout", [], False, _ms(t0), window_rect=rect)
        if job.error is not None:
            return UiaResult("error", [], False, _ms(t0), window_rect=rect, error=job.error)
        lines = job.lines or []
        usable = content_chars(lines) >= self.cfg.min_content_chars
        gaps = tuple(uncovered_bands(lines, rect, self.cfg.min_gap_px, self.cfg.container_frac))
        return UiaResult("ok" if lines else "empty", lines, usable, _ms(t0), job.count, rect, uncovered=gaps)

    def _remember(self, hwnd: int) -> None:
        self._seen[hwnd] = None
        self._seen.move_to_end(hwnd)
        while len(self._seen) > 256:
            self._seen.popitem(last=False)

    def _update_hint(self, hwnd: int, res: UiaResult) -> None:
        if res.status in ("minimized", "no_window", "busy"):
            return
        try:
            key = self.win.window_key(hwnd)
        except Exception:
            return
        with self._hint_lock:
            if res.usable:
                self._hint.pop(key, None)
                self._capable[key] = None
                self._capable.move_to_end(key)
                while len(self._capable) > 512:
                    self._capable.popitem(last=False)
                return
            streak, _ = self._hint.pop(key, (0, 0.0))
            self._hint[key] = (streak + 1, self._clock())
            while len(self._hint) > 512:
                self._hint.popitem(last=False)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000.0, 1)


_DEFAULT: UiaReader | None = None
_DEFAULT_LOCK = threading.Lock()


def default_reader() -> UiaReader:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = UiaReader()
        return _DEFAULT


def read_visible_lines(hwnd: int, timeout_s: float = 0.5) -> list[UiaLine]:
    """读窗口里屏幕上看得见的文字行（屏幕物理像素矩形）。超时、最小化、读不出时返回空列表。

    录制进程应直接用 default_reader().read(hwnd)：结果里的 usable / uncovered 配合 ocr_regions()
    决定哪些区域要登记待 OCR。
    """
    return default_reader().read(hwnd, timeout_s).lines


def has_text_hint(hwnd: int) -> bool:
    return default_reader().has_text_hint(hwnd)
