"""v2 录制核心用到的 Win32 调用（ctypes），只放 screenrecall/winapi.py 里没有的部分。

窗口标题、进程名、锁屏检测、PrintWindow 截图仍然用 winapi（在用到时才导入，
这样单元测试里装上的假 winapi 不会影响本模块被导入）。
所有坐标都是物理像素：录制进程启动时要先调用 winapi.enable_dpi_awareness()。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("screenrecall.rec.win32 仅支持 Windows")

log = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


_IsWindow = _fn(_user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
_IsIconic = _fn(_user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
_IsWindowVisible = _fn(_user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
_GetForegroundWindow = _fn(_user32, "GetForegroundWindow", wintypes.HWND)
_WindowFromPoint = _fn(_user32, "WindowFromPoint", wintypes.HWND, POINT)
_GetAncestor = _fn(_user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
_GetCursorPos = _fn(_user32, "GetCursorPos", wintypes.BOOL, ctypes.POINTER(POINT))
_GetWindowRect = _fn(_user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, ctypes.POINTER(RECT))
_DwmGetWindowAttribute = _fn(_dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wintypes.HWND, wintypes.DWORD,
                             ctypes.c_void_p, wintypes.DWORD)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


_GetLastInputInfo = _fn(_user32, "GetLastInputInfo", wintypes.BOOL, ctypes.POINTER(LASTINPUTINFO))
_GetTickCount = _fn(_kernel32, "GetTickCount", wintypes.DWORD)

_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DWMWA_CLOAKED = 14
_GA_ROOT = 2
_GA_ROOTOWNER = 3


# ---------- 窗口状态 ----------

def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(_IsWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    return bool(_IsIconic(hwnd))


def is_visible(hwnd: int) -> bool:
    return bool(_IsWindowVisible(hwnd))


def is_cloaked(hwnd: int) -> bool:
    """被 DWM 隐藏（别的虚拟桌面、UWP 挂起等）。"""
    val = wintypes.DWORD(0)
    hr = _DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return hr == 0 and val.value != 0


def foreground_window() -> int:
    return int(_GetForegroundWindow() or 0)


def root_owner(hwnd: int) -> int:
    """沿父窗口和所有者一直往上找到的最顶层窗口（GA_ROOTOWNER）；没有所有者时就是它自己。"""
    return int(_GetAncestor(hwnd, _GA_ROOTOWNER) or hwnd)


def frame_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口可见边框（DWMWA_EXTENDED_FRAME_BOUNDS，不含透明阴影），屏幕物理像素 (左, 上, 右, 下)。"""
    r = RECT()
    hr = _DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
    if hr != 0:
        if not _GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def root_window_at(x: int, y: int) -> int:
    """屏幕点 (x, y) 下面的顶层窗口。"""
    h = _WindowFromPoint(POINT(x, y))
    if not h:
        return 0
    return int(_GetAncestor(h, _GA_ROOT) or h)


def cursor_pos() -> tuple[int, int] | None:
    p = POINT()
    return (int(p.x), int(p.y)) if _GetCursorPos(ctypes.byref(p)) else None


def idle_seconds() -> float:
    """距离最后一次键盘/鼠标输入（任何程序）过了多少秒（GetLastInputInfo）。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------- 消息循环线程 ----------

_GetMessageW = _fn(_user32, "GetMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                   wintypes.UINT, wintypes.UINT)
_TranslateMessage = _fn(_user32, "TranslateMessage", wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_DispatchMessageW = _fn(_user32, "DispatchMessageW", LRESULT, ctypes.POINTER(wintypes.MSG))
_PeekMessageW = _fn(_user32, "PeekMessageW", wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                    wintypes.UINT, wintypes.UINT, wintypes.UINT)
_PostThreadMessageW = _fn(_user32, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT,
                          wintypes.WPARAM, wintypes.LPARAM)
_GetCurrentThreadId = _fn(_kernel32, "GetCurrentThreadId", wintypes.DWORD)
_WM_QUIT = 0x0012


class MessageThread:
    """专用的消息循环线程：setup() 在线程里安装钩子 / 建窗口，teardown() 在退出前清理。

    回调只做极少的事（记个时间、放进队列），消息循环线程不做任何耗时工作。
    """

    def __init__(self, name: str, setup: Callable[[], None], teardown: Callable[[], None] | None = None):
        self._name = name
        self._setup = setup
        self._teardown = teardown
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self.error: str | None = None

    def start(self, timeout: float = 5.0) -> bool:
        """启动（已经在跑时直接返回）。停掉之后可以再启动。"""
        if self._thread is not None and self._thread.is_alive():
            return self.error is None and self._ready.is_set()
        self._ready.clear()
        self.error = None
        self._tid = 0
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.error is None and self._ready.is_set()

    def _run(self) -> None:
        msg = wintypes.MSG()
        _PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # 先建消息队列，PostThreadMessage 才不会失败
        self._tid = int(_GetCurrentThreadId())
        try:
            self._setup()
        except Exception as exc:  # noqa: BLE001 - 安装失败要报告给调用方
            self.error = f"{type(exc).__name__}: {exc}"
            log.warning("%s 启动失败：%s", self._name, self.error)
            if self._teardown:  # 装了一半的钩子 / 窗口也要拆掉
                try:
                    self._teardown()
                except Exception:  # noqa: BLE001
                    log.exception("%s 清理失败", self._name)
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                ret = _GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0 or ret == -1:
                    break
                _TranslateMessage(ctypes.byref(msg))
                _DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._teardown:
                try:
                    self._teardown()
                except Exception:  # noqa: BLE001
                    log.exception("%s 清理失败", self._name)

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        if self._tid:
            _PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        self._thread.join(timeout)
        self._thread = None


# ---------- WinEvent 钩子 ----------

WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                  wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
SetWinEventHook = _fn(_user32, "SetWinEventHook", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                      wintypes.HMODULE, WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
UnhookWinEvent = _fn(_user32, "UnhookWinEvent", wintypes.BOOL, wintypes.HANDLE)
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0


# ---------- Raw Input ----------

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD),
                ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE),
                ("wParam", wintypes.WPARAM)]


class _RAWMOUSE_BUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT)]


class _RAWMOUSE_U(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG), ("s", _RAWMOUSE_BUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("u", _RAWMOUSE_U), ("ulRawButtons", wintypes.ULONG),
                ("lLastX", wintypes.LONG), ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT), ("Reserved", wintypes.USHORT),
                ("VKey", wintypes.USHORT), ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class _RAWINPUT_U(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE), ("keyboard", RAWKEYBOARD)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUT_U)]


RegisterClassExW = _fn(_user32, "RegisterClassExW", wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
UnregisterClassW = _fn(_user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
CreateWindowExW = _fn(_user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                      wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                      wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
DestroyWindow = _fn(_user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
DefWindowProcW = _fn(_user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
RegisterRawInputDevices = _fn(_user32, "RegisterRawInputDevices", wintypes.BOOL, ctypes.POINTER(RAWINPUTDEVICE),
                              wintypes.UINT, wintypes.UINT)
GetRawInputData = _fn(_user32, "GetRawInputData", wintypes.UINT, wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                      ctypes.POINTER(wintypes.UINT), wintypes.UINT)
GetModuleHandleW = _fn(_kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)

WM_INPUT = 0x00FF
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RI_MOUSE_WHEEL = 0x0400
RI_MOUSE_HWHEEL = 0x0800
HWND_MESSAGE = wintypes.HWND(-3)


# ---------- 低级鼠标钩子（可选：部分触控板的双指滚动不经过 Raw Input） ----------

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


SetWindowsHookExW = _fn(_user32, "SetWindowsHookExW", wintypes.HHOOK, ctypes.c_int, HOOKPROC, wintypes.HINSTANCE,
                        wintypes.DWORD)
UnhookWindowsHookEx = _fn(_user32, "UnhookWindowsHookEx", wintypes.BOOL, wintypes.HHOOK)
CallNextHookEx = _fn(_user32, "CallNextHookEx", LRESULT, wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
