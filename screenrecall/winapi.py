"""Windows 窗口枚举、像素捕获与锁屏检测（ctypes 实现，无额外依赖）。

坐标约定：后台进程启动时先调用 enable_dpi_awareness() 切到 Per-Monitor V2，
之后窗口矩形与截图都是物理像素，OCR 坐标与保存的原图一一对应（方案 2.4、3.4）。
不承诺在任意遮挡下取得完整窗口内容（方案 2.4）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

import numpy as np

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("screenrecall.winapi 仅支持 Windows")

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")

_HWND = wintypes.HWND
_HDC = wintypes.HDC
_HANDLE = wintypes.HANDLE
_BOOL = wintypes.BOOL
_UINT = wintypes.UINT
_DWORD = wintypes.DWORD
_INT = ctypes.c_int


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", _DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", _DWORD),
        ("biSizeImage", _DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", _DWORD),
        ("biClrImportant", _DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _DWORD * 3)]


_WNDENUMPROC = ctypes.WINFUNCTYPE(_BOOL, _HWND, wintypes.LPARAM)


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


# 句柄类返回值必须显式声明 restype，否则 64 位下会被截断成 int
_EnumWindows = _fn(_user32, "EnumWindows", _BOOL, _WNDENUMPROC, wintypes.LPARAM)
_IsWindow = _fn(_user32, "IsWindow", _BOOL, _HWND)
_IsWindowVisible = _fn(_user32, "IsWindowVisible", _BOOL, _HWND)
_IsIconic = _fn(_user32, "IsIconic", _BOOL, _HWND)
_GetWindow = _fn(_user32, "GetWindow", _HWND, _HWND, _UINT)
_GetWindowLongPtrW = _fn(_user32, "GetWindowLongPtrW", ctypes.c_ssize_t, _HWND, _INT)
_GetClassNameW = _fn(_user32, "GetClassNameW", _INT, _HWND, wintypes.LPWSTR, _INT)
_GetWindowTextLengthW = _fn(_user32, "GetWindowTextLengthW", _INT, _HWND)
_GetWindowTextW = _fn(_user32, "GetWindowTextW", _INT, _HWND, wintypes.LPWSTR, _INT)
_GetWindowThreadProcessId = _fn(
    _user32, "GetWindowThreadProcessId", _DWORD, _HWND, ctypes.POINTER(_DWORD)
)
_GetWindowRect = _fn(_user32, "GetWindowRect", _BOOL, _HWND, ctypes.POINTER(_RECT))
_GetForegroundWindow = _fn(_user32, "GetForegroundWindow", _HWND)
_GetDC = _fn(_user32, "GetDC", _HDC, _HWND)
_ReleaseDC = _fn(_user32, "ReleaseDC", _INT, _HWND, _HDC)
_PrintWindow = _fn(_user32, "PrintWindow", _BOOL, _HWND, _HDC, _UINT)
_OpenInputDesktop = _fn(_user32, "OpenInputDesktop", _HANDLE, _DWORD, _BOOL, _DWORD)
_CloseDesktop = _fn(_user32, "CloseDesktop", _BOOL, _HANDLE)
_GetUserObjectInformationW = _fn(
    _user32, "GetUserObjectInformationW", _BOOL, _HANDLE, _INT, ctypes.c_void_p, _DWORD,
    ctypes.POINTER(_DWORD),
)

_CreateCompatibleDC = _fn(_gdi32, "CreateCompatibleDC", _HDC, _HDC)
_CreateCompatibleBitmap = _fn(_gdi32, "CreateCompatibleBitmap", wintypes.HBITMAP, _HDC, _INT, _INT)
_SelectObject = _fn(_gdi32, "SelectObject", wintypes.HGDIOBJ, _HDC, wintypes.HGDIOBJ)
_DeleteObject = _fn(_gdi32, "DeleteObject", _BOOL, wintypes.HGDIOBJ)
_DeleteDC = _fn(_gdi32, "DeleteDC", _BOOL, _HDC)
_BitBlt = _fn(_gdi32, "BitBlt", _BOOL, _HDC, _INT, _INT, _INT, _INT, _HDC, _INT, _INT, _DWORD)
_GetDIBits = _fn(
    _gdi32, "GetDIBits", _INT, _HDC, wintypes.HBITMAP, _UINT, _UINT, ctypes.c_void_p,
    ctypes.POINTER(_BITMAPINFO), _UINT,
)

_DwmGetWindowAttribute = _fn(
    _dwmapi, "DwmGetWindowAttribute", ctypes.c_long, _HWND, _DWORD, ctypes.c_void_p, _DWORD
)

_OpenProcess = _fn(_kernel32, "OpenProcess", _HANDLE, _DWORD, _BOOL, _DWORD)
_CloseHandle = _fn(_kernel32, "CloseHandle", _BOOL, _HANDLE)
_QueryFullProcessImageNameW = _fn(
    _kernel32, "QueryFullProcessImageNameW", _BOOL, _HANDLE, _DWORD, wintypes.LPWSTR,
    ctypes.POINTER(_DWORD),
)

try:
    _GetDpiForWindow = _fn(_user32, "GetDpiForWindow", _UINT, _HWND)
except AttributeError:  # Windows 10 1607 之前
    _GetDpiForWindow = None

_MonitorFromWindow = _fn(_user32, "MonitorFromWindow", wintypes.HMONITOR, _HWND, _DWORD)
try:
    _shcore = ctypes.WinDLL("shcore")
    _GetDpiForMonitor = _fn(
        _shcore, "GetDpiForMonitor", ctypes.c_long, wintypes.HMONITOR, _INT,
        ctypes.POINTER(_UINT), ctypes.POINTER(_UINT),
    )
except (OSError, AttributeError):
    _GetDpiForMonitor = None

_PW_RENDERFULLCONTENT = 0x00000002
_SRCCOPY = 0x00CC0020
_DIB_RGB_COLORS = 0
_BI_RGB = 0
_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DESKTOP_READOBJECTS = 0x0001
_UOI_NAME = 2
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


# ---------- DPI ----------

def current_dpi_awareness() -> str:
    try:
        get_ctx = _fn(_user32, "GetThreadDpiAwarenessContext", _HANDLE)
        get_aw = _fn(_user32, "GetAwarenessFromDpiAwarenessContext", _INT, _HANDLE)
        return {0: "unaware", 1: "system", 2: "per_monitor"}.get(get_aw(get_ctx()), "unknown")
    except AttributeError:
        return "unknown"


def enable_dpi_awareness() -> str:
    """把当前进程设为按显示器 DPI 感知，使坐标与截图都是物理像素。返回最终模式。"""
    try:
        set_ctx = _fn(_user32, "SetProcessDpiAwarenessContext", _BOOL, _HANDLE)
        if set_ctx(_HANDLE(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return "per_monitor_v2"
    except AttributeError:
        pass
    if current_dpi_awareness() == "per_monitor":
        return "per_monitor"
    try:
        shcore = ctypes.WinDLL("shcore")
        if _fn(shcore, "SetProcessDpiAwareness", ctypes.c_long, _INT)(2) == 0:
            return "per_monitor"
    except (AttributeError, OSError):
        pass
    if _fn(_user32, "SetProcessDPIAware", _BOOL)():
        return "system"
    return current_dpi_awareness()


# ---------- 窗口信息 ----------

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int
    width: int
    height: int


def window_title(hwnd: int) -> str:
    n = _GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = _DWORD(0)
    _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def process_path(pid: int) -> str | None:
    handle = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = _DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if _QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _CloseHandle(handle)


def process_name(pid: int) -> str:
    path = process_path(pid)
    return os.path.basename(path) if path else "unknown"


def file_description(path: str) -> str | None:
    """exe 版本信息里的"文件说明"（如 Weixin.exe → 微信）；没有就 None。"""
    try:
        version = ctypes.WinDLL("version")
    except OSError:
        return None
    size = version.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, data):
        return None
    ptr, n = ctypes.c_void_p(), _UINT(0)
    langs = [(0x0409, 0x04B0), (0x0804, 0x04B0), (0x0000, 0x04B0), (0x0409, 0x04E4)]
    if version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) and n.value >= 4:
        pairs = (wintypes.WORD * (n.value // 2)).from_address(ptr.value)
        langs = [(pairs[i], pairs[i + 1]) for i in range(0, len(pairs) - 1, 2)] + langs
    for lang, cp in langs:
        key = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\FileDescription"
        if version.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(n)) and n.value > 1:
            text = ctypes.wstring_at(ptr.value, n.value - 1).strip()
            if text:
                return text
    return None


def _window_rect(hwnd: int) -> _RECT | None:
    r = _RECT()
    return r if _GetWindowRect(hwnd, ctypes.byref(r)) else None


def _frame_bounds(hwnd: int) -> _RECT | None:
    """可见边框（不含 Win10/11 的透明阴影边）。"""
    r = _RECT()
    hr = _DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
    return r if hr == 0 else None


def _is_cloaked(hwnd: int) -> bool:
    val = _DWORD(0)
    hr = _DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return hr == 0 and val.value != 0


def describe_window(hwnd: int | None) -> WindowInfo | None:
    if not hwnd or not _IsWindow(hwnd):
        return None
    pid = window_pid(hwnd)
    r = _frame_bounds(hwnd) or _window_rect(hwnd)
    w, h = (r.right - r.left, r.bottom - r.top) if r else (0, 0)
    return WindowInfo(int(hwnd), window_title(hwnd), process_name(pid), pid, w, h)


def _is_candidate(hwnd: int) -> bool:
    if not _IsWindowVisible(hwnd) or _GetWindow(hwnd, _GW_OWNER):
        return False
    if _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
        return False
    if _is_cloaked(hwnd) or class_name(hwnd) in _SKIP_CLASSES:
        return False
    return True


def list_windows() -> list[WindowInfo]:
    """可供录制的顶层窗口（近似 Alt-Tab 列表）。"""
    found: list[int] = []

    def _collect(hwnd, _lparam):
        if hwnd:
            found.append(int(hwnd))
        return True

    _EnumWindows(_WNDENUMPROC(_collect), 0)
    out: list[WindowInfo] = []
    for hwnd in found:
        try:
            if not _is_candidate(hwnd):
                continue
            info = describe_window(hwnd)
        except OSError:
            continue
        if info and info.title.strip():
            out.append(info)
    return out


# UWP 应用的窗口都属于 ApplicationFrameHost.exe，选它等于选了所有商店应用，名字也看不懂
_NOT_PICKABLE = frozenset({"applicationframehost.exe", "explorer.exe", "searchhost.exe", "startmenuexperiencehost.exe",
                           "shellexperiencehost.exe", "textinputhost.exe", "lockapp.exe", "unknown"})


def running_apps(skip_pids: frozenset[int] = frozenset()) -> list[dict]:
    """现在开着窗口的软件（按进程名合并）：[{process, name, title, windows}]，窗口多的在前。名单页点选用。
    name = exe 的"文件说明"（没有就用进程名）；title = 它第一个窗口的标题（有的软件说明是英文，标题才是"微信"）。"""
    seen: dict[str, dict] = {}
    for w in list_windows():
        if w.pid in skip_pids or w.process.lower() in _NOT_PICKABLE:
            continue
        hit = seen.get(w.process.lower())
        if hit:
            hit["windows"] += 1
            continue
        path = process_path(w.pid)
        name = (file_description(path) if path else None) or os.path.splitext(w.process)[0]
        seen[w.process.lower()] = {"process": w.process, "name": name, "title": w.title.strip()[:80], "windows": 1}
    return sorted(seen.values(), key=lambda a: (-a["windows"], a["name"].lower()))


# ---------- 捕获 ----------

class CaptureError(Exception):
    """可解释的捕获失败。reason 是稳定的短代码，记入缺口表。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Capture:
    bgr: bytes  # 紧凑 BGR（无 alpha），自上而下逐行
    width: int
    height: int
    method: str  # 'print_window' | 'screen'
    rect: tuple[int, int, int, int]  # 屏幕物理像素 (left, top, right, bottom)
    dpi: int | None


def _monitor_dpi(hwnd: int) -> int:
    if _GetDpiForMonitor is None:
        return 0
    hmon = _MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    x, y = _UINT(0), _UINT(0)
    if not hmon or _GetDpiForMonitor(hmon, 0, ctypes.byref(x), ctypes.byref(y)) != 0:  # MDT_EFFECTIVE_DPI
        return 0
    return int(x.value)


def _read_bits(dc, bmp, w: int, h: int) -> np.ndarray:
    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth = w
    hdr.biHeight = -h  # 负值表示自上而下
    hdr.biPlanes = 1
    hdr.biBitCount = 32
    hdr.biCompression = _BI_RGB
    buf = (ctypes.c_ubyte * (w * h * 4))()
    got = _GetDIBits(dc, bmp, 0, h, ctypes.cast(buf, ctypes.c_void_p), ctypes.byref(bmi), _DIB_RGB_COLORS)
    if got != h:
        raise CaptureError("gdi_failed", f"GetDIBits 返回 {got}/{h} 行")
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)


def _grab(w: int, h: int, draw) -> np.ndarray | None:
    """创建内存位图，调用 draw(mem_dc, screen_dc) 绘制，成功则读出 BGRA 像素。"""
    screen_dc = _GetDC(None)
    if not screen_dc:
        raise CaptureError("gdi_failed", "GetDC")
    mem_dc = bmp = None
    try:
        mem_dc = _CreateCompatibleDC(screen_dc)
        bmp = _CreateCompatibleBitmap(screen_dc, w, h)
        if not mem_dc or not bmp:
            raise CaptureError("gdi_failed", "CreateCompatibleBitmap")
        old = _SelectObject(mem_dc, bmp)
        try:
            ok = draw(mem_dc, screen_dc)
        finally:
            _SelectObject(mem_dc, old)  # GetDIBits 要求位图未被选入 DC
        if not ok:
            return None
        return _read_bits(mem_dc, bmp, w, h)
    finally:
        if bmp:
            _DeleteObject(bmp)
        if mem_dc:
            _DeleteDC(mem_dc)
        _ReleaseDC(None, screen_dc)


def capture_window(hwnd: int) -> Capture:
    """捕获窗口可见边框内的像素。

    优先 PrintWindow(PW_RENDERFULLCONTENT)，被部分遮挡时通常仍能取到窗口内容；
    若失败或返回全黑，只在窗口位于前台时退回屏幕拷贝（此时可能包含覆盖在上面的弹出物）。
    """
    if not hwnd or not _IsWindow(hwnd):
        raise CaptureError("window_gone")
    if _IsIconic(hwnd):
        raise CaptureError("minimized")
    if not _IsWindowVisible(hwnd):
        raise CaptureError("hidden")
    wr = _window_rect(hwnd)
    if wr is None:
        raise CaptureError("rect_failed")
    fb = _frame_bounds(hwnd) or wr
    full_w, full_h = wr.right - wr.left, wr.bottom - wr.top
    ox, oy = max(fb.left - wr.left, 0), max(fb.top - wr.top, 0)
    cw = min(fb.right - fb.left, full_w - ox)
    ch = min(fb.bottom - fb.top, full_h - oy)
    if full_w <= 0 or full_h <= 0 or cw <= 0 or ch <= 0:
        raise CaptureError("empty_rect")
    dpi = int(_GetDpiForWindow(hwnd)) if _GetDpiForWindow else 0
    mon_dpi = _monitor_dpi(hwnd)

    # DPI 不感知的程序：PrintWindow 只按逻辑尺寸画在位图左上角，其余为黑。
    # 此时截取程序实际绘制的逻辑尺寸内容（即未经 DWM 放大的原始像素）。
    scale = dpi / mon_dpi if dpi and mon_dpi and dpi < mon_dpi else 1.0
    pw_ox, pw_oy = round(ox * scale), round(oy * scale)
    pw_w, pw_h = max(1, round(cw * scale)), max(1, round(ch * scale))

    method = "print_window" if scale == 1.0 else "print_window_logical"
    pixels = _grab(full_w, full_h, lambda mem, _scr: _PrintWindow(hwnd, mem, _PW_RENDERFULLCONTENT))
    crop = None
    if pixels is not None:
        crop = pixels[pw_oy : pw_oy + pw_h, pw_ox : pw_ox + pw_w, :3]
        if not crop.any():
            crop = None
    if crop is None:
        if _GetForegroundWindow() != hwnd:
            raise CaptureError("blank_capture", "PrintWindow 无内容，且窗口不在前台")
        left, top = fb.left, fb.top
        shot = _grab(cw, ch, lambda mem, scr: _BitBlt(mem, 0, 0, cw, ch, scr, left, top, _SRCCOPY))
        if shot is None:
            raise CaptureError("gdi_failed", "BitBlt")
        crop = shot[:, :, :3]
        method = "screen"
    out_h, out_w = crop.shape[:2]
    return Capture(
        bgr=np.ascontiguousarray(crop).tobytes(),
        width=out_w,
        height=out_h,
        method=method,
        rect=(fb.left, fb.top, fb.left + cw, fb.top + ch),
        dpi=dpi or None,
    )


def is_input_desktop_locked() -> bool:
    """锁屏或安全桌面（UAC）时，输入桌面不是 Default 或无法打开。"""
    hdesk = _OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not hdesk:
        return True
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = _DWORD(0)
        if not _GetUserObjectInformationW(hdesk, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return False
        return buf.value.lower() != "default"
    finally:
        _CloseDesktop(hdesk)


# ---------- 全局快捷键（RegisterHotKey + 线程消息循环） ----------

_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_PM_NOREMOVE = 0x0000

_RegisterHotKey = _fn(_user32, "RegisterHotKey", _BOOL, _HWND, _INT, _UINT, _UINT)
_UnregisterHotKey = _fn(_user32, "UnregisterHotKey", _BOOL, _HWND, _INT)
_GetMessageW = _fn(_user32, "GetMessageW", _BOOL, ctypes.POINTER(wintypes.MSG), _HWND, _UINT, _UINT)
_PeekMessageW = _fn(_user32, "PeekMessageW", _BOOL, ctypes.POINTER(wintypes.MSG), _HWND, _UINT, _UINT, _UINT)
_PostThreadMessageW = _fn(_user32, "PostThreadMessageW", _BOOL, _DWORD, _UINT, wintypes.WPARAM, wintypes.LPARAM)
_GetCurrentThreadId = _fn(_kernel32, "GetCurrentThreadId", _DWORD)


def current_thread_id() -> int:
    return int(_GetCurrentThreadId())


def ensure_message_queue() -> None:
    """线程第一次调用消息函数时系统才创建消息队列；先建好，PostThreadMessage 才不会失败。"""
    msg = wintypes.MSG()
    _PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_NOREMOVE)


def register_hotkey(hotkey_id: int, modifiers: int, vk: int) -> bool:
    """热键注册到调用线程（hwnd=NULL），WM_HOTKEY 投递到该线程的消息队列。"""
    return bool(_RegisterHotKey(None, hotkey_id, modifiers, vk))


def unregister_hotkey(hotkey_id: int) -> None:
    _UnregisterHotKey(None, hotkey_id)


def hotkey_messages():
    """阻塞读取本线程消息，逐个产出 WM_HOTKEY 的热键 ID；收到 WM_QUIT 或出错时结束。"""
    msg = wintypes.MSG()
    while True:
        ret = _GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret == 0 or ret == -1:
            return
        if msg.message == _WM_HOTKEY:
            yield int(msg.wParam)


def post_quit(thread_id: int) -> None:
    _PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)
