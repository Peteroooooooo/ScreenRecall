"""键盘活动监测：只记"最后一次打字是什么时候"，从不记录按了哪个键。

"打字"（v3 的跳过规则：这个页面 5 秒内打过字就先不拍）：按下的不是翻页 / 方向键、不是修饰键，并且当时没按着
Ctrl / Alt / Win（所以复制、粘贴、Ctrl+组合键都不算）。修饰键按着没有，靠 Raw Input 的按下 / 抬起跟踪
（TypingState，纯逻辑，可单测）。

只注册键盘的 Raw Input（RIDEV_INPUTSINK，消息只是排进本线程队列，系统不会因为我们处理慢而卡住；
不注册鼠标，免得每次鼠标移动都来一条消息）。SendInput 模拟的按键同样经过 Raw Input；
浏览器开发者协议（CDP）派发的按键不经过系统输入，这里看不到，也不算打字。
空闲秒数用 GetLastInputInfo，任何程序的输入都会让它归零。
"""

from __future__ import annotations

import ctypes
import logging

from . import win32
from .clock import Clock, now_ms

log = logging.getLogger(__name__)


# 翻页 / 方向键：空格、PageUp/PageDown、End/Home、方向键。只是在翻看，不算打字
NAV_KEYS = frozenset({0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28})

# 修饰键：Shift / Ctrl / Alt（通用码和左右各自的码）、左右 Win。其中按着 Ctrl / Alt / Win 时按别的键算"组合键"，不算打字；
# Shift 只是大写 / 符号，Shift+字母照样算打字
_SHIFT_KEYS = frozenset({0x10, 0xA0, 0xA1})
_CHORD_KEYS = {0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl", 0x12: "alt", 0xA4: "alt", 0xA5: "alt",
               0x5B: "win", 0x5C: "win"}
MODIFIER_KEYS = _SHIFT_KEYS | frozenset(_CHORD_KEYS)
# 没收到抬起（按着 Win+L 锁屏、安全桌面上松开）时，修饰键不能一直算"按着"：这么久没再收到它的按下就当松开了
# （一直按着的键会不停地自动重复按下）
MOD_STALE_MS = 30000

WM_KEYUP = 0x0101
WM_SYSKEYUP = 0x0105
RI_KEY_BREAK = 0x0001


class TypingState:
    """跟踪 Ctrl / Alt / Win 按着没有，判断一次按下算不算"打字"。只记修饰键的种类和时间，不记别的键值。"""

    def __init__(self):
        self._held: dict[str, int] = {}  # "ctrl" / "alt" / "win" → 最近一次按下的时间

    def key(self, vkey: int, down: bool, t_ms: int) -> bool:
        """一次按下 / 抬起；返回这次是不是"打字"（只有按下才可能是）。"""
        chord = _CHORD_KEYS.get(vkey)
        if chord is not None:
            if down:
                self._held[chord] = t_ms
            else:
                self._held.pop(chord, None)
            return False
        if not down or vkey in _SHIFT_KEYS or vkey in NAV_KEYS:
            return False
        for k, at in list(self._held.items()):
            if t_ms - at > MOD_STALE_MS:
                del self._held[k]
        return not self._held


class InputMonitor:
    """专用消息循环线程里收按键。"""

    _class_seq = 0

    def __init__(self, clock: Clock | None = None):
        self._clock = clock or now_ms
        self._last_typing_ms: int | None = None
        self._typing = TypingState()
        self.key_downs = 0       # 只有次数，没有键值
        self._hwnd = None
        self._wndproc = None
        self._class_name = ""
        self._hinst = None
        self._rb = None  # 消息线程复用的 Raw Input 缓冲
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
    def last_typing_ms(self) -> int | None:
        """最后一次"打字"的时间（见 TypingState；只有时间）。"""
        return self._last_typing_ms

    @staticmethod
    def idle_seconds() -> float:
        return win32.idle_seconds()

    # ---- 消息线程里 ----

    def _setup(self) -> None:
        self._hinst = win32.GetModuleHandleW(None)
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
        dev = (win32.RAWINPUTDEVICE * 1)()
        dev[0].usUsagePage, dev[0].usUsage = 1, 6  # 键盘
        dev[0].dwFlags = win32.RIDEV_INPUTSINK
        dev[0].hwndTarget = self._hwnd
        if not win32.RegisterRawInputDevices(dev, 1, ctypes.sizeof(win32.RAWINPUTDEVICE)):
            raise OSError(ctypes.get_last_error(), "RegisterRawInputDevices")

    def _teardown(self) -> None:
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
        # 缓冲和参数预先建好、只在本线程里复用
        rb = self._rb
        if rb is None:
            buf = win32.RAWINPUT()
            size = ctypes.c_uint(0)
            rb = self._rb = (buf, size, ctypes.byref(buf), ctypes.byref(size), ctypes.sizeof(buf),
                             ctypes.sizeof(win32.RAWINPUTHEADER))
        buf, size, buf_ref, size_ref, full, hdr = rb
        size.value = full
        got = win32.GetRawInputData(lparam, win32.RID_INPUT, buf_ref, size_ref, hdr)
        if got == 0xFFFFFFFF or got == 0:
            return
        if buf.header.dwType != win32.RIM_TYPEKEYBOARD:
            return
        kb = buf.data.keyboard
        down = kb.Message in (win32.WM_KEYDOWN, win32.WM_SYSKEYDOWN) and not kb.Flags & RI_KEY_BREAK
        if down:
            t = self._clock()
            self.key_downs += 1
            if self._typing.key(kb.VKey, True, t):  # 键值只用来判断"算不算打字"，不留
                self._last_typing_ms = t
        elif kb.Message in (WM_KEYUP, WM_SYSKEYUP) or kb.Flags & RI_KEY_BREAK:
            self._typing.key(kb.VKey, False, self._clock())  # 只用来跟踪修饰键松开


class FakeInput:
    """测试用：手动塞按键。接口和 InputMonitor 一样。"""

    def __init__(self):
        self.last_typing_ms: int | None = None
        self.idle = 0.0

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def key(self, t_ms: int, nav: bool = False, typing: bool = True) -> None:
        """按了一个键。翻页 / 方向键（nav=True）从来不算打字；typing=False 模拟 Ctrl+C 这类组合键。"""
        if typing and not nav:
            self.last_typing_ms = t_ms

    def typing(self, t_ms: int) -> None:
        self.key(t_ms)

    def idle_seconds(self) -> float:
        return self.idle
