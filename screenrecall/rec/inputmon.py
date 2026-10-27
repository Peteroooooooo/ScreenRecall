"""滚轮和键盘活动监测：只记"什么时候滚了、往哪滚、什么时候按过键"，从不记录按了哪个键。

默认用 Raw Input（RIDEV_INPUTSINK，消息只是排进本线程队列，系统不会因为我们处理慢而卡住
或摘掉钩子）。部分触控板的双指滚动不经过 Raw Input，可以另开 WH_MOUSE_LL 低级鼠标钩子
（use_mouse_hook=True）；钩子回调只往队列里放一个元组，立刻 CallNextHookEx 返回。
空闲秒数用 GetLastInputInfo，任何程序的输入都会让它归零。
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections import deque
from dataclasses import dataclass

from . import win32
from .clock import Clock, now_ms

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WheelEvent:
    """一次滚轮。delta 正数 = 往上（远离用户）滚，一格是 120；x, y 是当时光标的屏幕物理像素。"""

    t_ms: int
    delta: int
    horizontal: bool
    x: int
    y: int


def _signed16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class InputMonitor:
    """专用消息循环线程里收滚轮和按键。drain_wheel() 取走积压的滚轮事件。"""

    _class_seq = 0

    def __init__(self, clock: Clock | None = None, *, use_raw_input: bool = True, use_mouse_hook: bool = False,
                 max_events: int = 1024):
        self._clock = clock or now_ms
        self._use_raw = use_raw_input
        self._use_hook = use_mouse_hook
        self._wheel: deque[WheelEvent] = deque(maxlen=max_events)
        self._last_key_ms: int | None = None
        self.key_downs = 0       # 只有次数，没有键值
        self.wheel_events = 0
        self._hwnd = None
        self._hook = None
        self._wndproc = None
        self._hookproc = None
        self._class_name = ""
        self._hinst = None
        self._thread = win32.MessageThread("sr-inputmon", self._setup, self._teardown)

    # ---- 对外 ----

    def start(self) -> bool:
        ok = self._thread.start()
        if not ok:
            log.warning("输入监测没有启动：%s", self._thread.error)
        return ok

    def stop(self) -> None:
        self._thread.stop()

    @property
    def error(self) -> str | None:
        return self._thread.error

    @property
    def last_key_ms(self) -> int | None:
        return self._last_key_ms

    def drain_wheel(self) -> list[WheelEvent]:
        out = []
        while True:
            try:
                out.append(self._wheel.popleft())
            except IndexError:
                return out

    @staticmethod
    def idle_seconds() -> float:
        return win32.idle_seconds()

    # ---- 消息线程里 ----

    def _setup(self) -> None:
        self._hinst = win32.GetModuleHandleW(None)
        if self._use_raw:
            InputMonitor._class_seq += 1
            self._class_name = f"ScreenRecallInput{id(self):x}_{InputMonitor._class_seq}"
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
            devs = []
            if not self._use_hook:
                devs.append((1, 2))  # 鼠标
            devs.append((1, 6))      # 键盘
            arr = (win32.RAWINPUTDEVICE * len(devs))()
            for i, (page, usage) in enumerate(devs):
                arr[i].usUsagePage, arr[i].usUsage = page, usage
                arr[i].dwFlags = win32.RIDEV_INPUTSINK
                arr[i].hwndTarget = self._hwnd
            if not win32.RegisterRawInputDevices(arr, len(devs), ctypes.sizeof(win32.RAWINPUTDEVICE)):
                raise OSError(ctypes.get_last_error(), "RegisterRawInputDevices")
        if self._use_hook:
            self._hookproc = win32.HOOKPROC(self._on_hook)
            self._hook = win32.SetWindowsHookExW(win32.WH_MOUSE_LL, self._hookproc, self._hinst, 0)
            if not self._hook:
                raise OSError(ctypes.get_last_error(), "SetWindowsHookExW")

    def _teardown(self) -> None:
        if self._hook:
            win32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        if self._hwnd:
            win32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._class_name:
            win32.UnregisterClassW(self._class_name, self._hinst)

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == win32.WM_INPUT:
            try:
                self._handle_raw(lparam)
            except Exception:  # noqa: BLE001 - 回调里不能抛异常
                pass
        return win32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _handle_raw(self, lparam) -> None:
        size = ctypes.c_uint(ctypes.sizeof(win32.RAWINPUT))
        buf = win32.RAWINPUT()
        got = win32.GetRawInputData(lparam, win32.RID_INPUT, ctypes.byref(buf), ctypes.byref(size),
                                    ctypes.sizeof(win32.RAWINPUTHEADER))
        if got == 0xFFFFFFFF or got == 0:
            return
        kind = buf.header.dwType
        if kind == win32.RIM_TYPEKEYBOARD:
            if buf.data.keyboard.Message in (win32.WM_KEYDOWN, win32.WM_SYSKEYDOWN):
                self._last_key_ms = self._clock()
                self.key_downs += 1
        elif kind == win32.RIM_TYPEMOUSE:
            flags = buf.data.mouse.u.s.usButtonFlags
            if flags & (win32.RI_MOUSE_WHEEL | win32.RI_MOUSE_HWHEEL):
                pos = win32.cursor_pos() or (0, 0)
                self._push(_signed16(buf.data.mouse.u.s.usButtonData), bool(flags & win32.RI_MOUSE_HWHEEL), pos)

    def _on_hook(self, code, wparam, lparam):
        if code >= 0 and wparam in (win32.WM_MOUSEWHEEL, win32.WM_MOUSEHWHEEL):
            try:
                info = ctypes.cast(lparam, ctypes.POINTER(win32.MSLLHOOKSTRUCT)).contents
                self._push(_signed16(info.mouseData >> 16), wparam == win32.WM_MOUSEHWHEEL, (info.pt.x, info.pt.y))
            except Exception:  # noqa: BLE001
                pass
        return win32.CallNextHookEx(None, code, wparam, lparam)

    def _push(self, delta: int, horizontal: bool, pos: tuple[int, int]) -> None:
        self.wheel_events += 1
        self._wheel.append(WheelEvent(self._clock(), delta, horizontal, int(pos[0]), int(pos[1])))


class FakeInput:
    """测试用：手动塞滚轮和按键。接口和 InputMonitor 一样。"""

    def __init__(self):
        self._wheel: deque[WheelEvent] = deque()
        self.last_key_ms: int | None = None
        self.idle = 0.0
        self._lock = threading.Lock()

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def wheel(self, t_ms: int, delta: int, x: int = 0, y: int = 0, horizontal: bool = False) -> None:
        self._wheel.append(WheelEvent(t_ms, delta, horizontal, x, y))

    def key(self, t_ms: int) -> None:
        self.last_key_ms = t_ms

    def drain_wheel(self) -> list[WheelEvent]:
        out = list(self._wheel)
        self._wheel.clear()
        return out

    def idle_seconds(self) -> float:
        return self.idle
