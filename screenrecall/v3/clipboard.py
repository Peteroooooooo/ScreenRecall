"""剪贴板监听：消息专用窗口 + AddClipboardFormatListener。

剪贴板一变（WM_CLIPBOARDUPDATE）就在消息线程里读 CF_UNICODETEXT（OpenClipboard 被别的程序占着时重试几次），
回调 on_copy(text, t_ms)。不是文字（图片、文件）时不回调；放进剪贴板时带了系统约定的
"ExcludeClipboardContentFromMonitorProcessing" 格式（密码管理器等要求剪贴板监听程序别处理）时也不回调。
回调只应入队（CaptureLoop.on_copy）。前台是不是要录的窗口由捕获循环判断。
回调还带上剪贴板属主窗口（GetClipboardOwner）的进程号：后台程序（剪贴板历史工具、远程桌面同步等）写进剪贴板时，
捕获循环据此不把它算成"在前台窗口里复制的"；属主取不到时 None（照常按前台算）。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Callable

from ..rec import win32
from ..rec.clock import Clock, now_ms

log = logging.getLogger(__name__)

WM_CLIPBOARDUPDATE = 0x031D
CF_UNICODETEXT = 13
OPEN_RETRIES = 5
OPEN_RETRY_S = 0.02
EXCLUDE_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


_AddListener = _fn(_user32, "AddClipboardFormatListener", wintypes.BOOL, wintypes.HWND)
_RemoveListener = _fn(_user32, "RemoveClipboardFormatListener", wintypes.BOOL, wintypes.HWND)
_OpenClipboard = _fn(_user32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
_CloseClipboard = _fn(_user32, "CloseClipboard", wintypes.BOOL)
_IsFormatAvailable = _fn(_user32, "IsClipboardFormatAvailable", wintypes.BOOL, wintypes.UINT)
_GetClipboardData = _fn(_user32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
_RegisterFormat = _fn(_user32, "RegisterClipboardFormatW", wintypes.UINT, wintypes.LPCWSTR)
_GlobalLock = _fn(_kernel32, "GlobalLock", wintypes.LPVOID, wintypes.HGLOBAL)
_GlobalUnlock = _fn(_kernel32, "GlobalUnlock", wintypes.BOOL, wintypes.HGLOBAL)
_GlobalSize = _fn(_kernel32, "GlobalSize", ctypes.c_size_t, wintypes.HGLOBAL)
_GetOwner = _fn(_user32, "GetClipboardOwner", wintypes.HWND)
_GetWindowPid = _fn(_user32, "GetWindowThreadProcessId", wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


def owner_pid() -> int | None:
    """剪贴板属主窗口的进程号；没有属主（OpenClipboard(NULL) 放的）或取不到时 None。"""
    h = _GetOwner()
    if not h:
        return None
    pid = wintypes.DWORD(0)
    _GetWindowPid(h, ctypes.byref(pid))
    return int(pid.value) or None


def read_text(hwnd: int | None = None, *, max_chars: int = 1_000_000) -> str | None:
    """读剪贴板里的文字；没有文字、要求别处理、或一直打不开时 None。"""
    if not _IsFormatAvailable(CF_UNICODETEXT):
        return None
    fmt = _RegisterFormat(EXCLUDE_FORMAT)
    if fmt and _IsFormatAvailable(fmt):
        return None
    for i in range(OPEN_RETRIES):
        if _OpenClipboard(hwnd):
            break
        time.sleep(OPEN_RETRY_S * (i + 1))
    else:
        return None
    try:
        h = _GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _GlobalLock(h)
        if not p:
            return None
        try:
            n = int(_GlobalSize(h)) // 2
            return ctypes.wstring_at(p, max(0, min(n, max_chars + 1))).split("\0", 1)[0]
        finally:
            _GlobalUnlock(h)
    finally:
        _CloseClipboard()


class ClipboardWatcher:
    """见模块说明。start() 失败（装不上监听）时返回 False，录制照常（只是不记复制）。"""

    _class_seq = 0

    def __init__(self, on_copy: Callable[..., None], *, clock: Clock = now_ms,
                 reader: Callable[[int | None], str | None] | None = None,
                 owner: Callable[[], int | None] | None = None):
        self._on_copy = on_copy
        self._clock = clock
        self._read = reader or read_text
        self._owner = owner or owner_pid
        self._hwnd = None
        self._wndproc = None
        self._class_name = ""
        self._hinst = None
        self.updates = 0
        self.copies = 0
        self._thread = win32.MessageThread("sr-v3-clipboard", self._setup, self._teardown)

    def start(self) -> bool:
        ok = self._thread.start()
        if not ok:
            log.warning("剪贴板监听没有启动：%s", self._thread.error)
        return ok

    def stop(self) -> None:
        self._thread.stop()

    # ---- 消息线程里 ----

    def _setup(self) -> None:
        self._hinst = win32.GetModuleHandleW(None)
        ClipboardWatcher._class_seq += 1
        self._class_name = f"ScreenRecallClip{id(self):x}_{ClipboardWatcher._class_seq}"
        self._wndproc = win32.WNDPROC(self._on_message)
        wc = win32.WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(wc)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self._hinst
        wc.lpszClassName = self._class_name
        if not win32.RegisterClassExW(ctypes.byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassExW")
        self._hwnd = win32.CreateWindowExW(0, self._class_name, "", 0, 0, 0, 0, 0, win32.HWND_MESSAGE,
                                           None, self._hinst, None)
        if not self._hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW")
        if not _AddListener(self._hwnd):
            raise OSError(ctypes.get_last_error(), "AddClipboardFormatListener")

    def _teardown(self) -> None:
        if self._hwnd:
            _RemoveListener(self._hwnd)
            win32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._class_name:
            win32.UnregisterClassW(self._class_name, self._hinst)
            self._class_name = ""

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_CLIPBOARDUPDATE:
            self.on_update()
            return 0
        return win32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def on_update(self) -> None:
        """剪贴板变了：读文字、回调。回调里不能抛异常（消息线程）。"""
        self.updates += 1
        try:
            t = self._clock()
            text = self._read(self._hwnd)
            if text and text.strip():
                self.copies += 1
                try:
                    pid = self._owner()
                except Exception:  # noqa: BLE001
                    pid = None
                self._on_copy(text, t, owner_pid=pid)
        except Exception:  # noqa: BLE001
            log.exception("读剪贴板失败")
