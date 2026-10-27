"""电脑是否空闲：键鼠空闲秒数（GetLastInputInfo）、是否锁屏，以及 IdleWatcher。

空闲整理进程（OCR、向量、概括）只在空闲时跑：键鼠 5 分钟没动，或者已经锁屏。
IdleWatcher 在后台线程里按固定间隔查看，状态变化时调用 on_idle_start(原因) / on_idle_end()。
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable

log = logging.getLogger("screenrecall.app.idle")

if sys.platform == "win32":
    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetLastInputInfo = _user32.GetLastInputInfo
    _GetLastInputInfo.restype, _GetLastInputInfo.argtypes = wintypes.BOOL, [ctypes.POINTER(_LASTINPUTINFO)]
    _GetTickCount = _kernel32.GetTickCount
    _GetTickCount.restype, _GetTickCount.argtypes = wintypes.DWORD, []
    _OpenInputDesktop = _user32.OpenInputDesktop
    _OpenInputDesktop.restype = wintypes.HANDLE
    _OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _CloseDesktop = _user32.CloseDesktop
    _CloseDesktop.restype, _CloseDesktop.argtypes = wintypes.BOOL, [wintypes.HANDLE]
    _GetUserObjectInformationW = _user32.GetUserObjectInformationW
    _GetUserObjectInformationW.restype = wintypes.BOOL
    _GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]

_DESKTOP_READOBJECTS = 0x0001
_UOI_NAME = 2


def idle_seconds() -> float:
    """距离最后一次键盘 / 鼠标输入的秒数（本会话内）。"""
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
    if not _GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    # 两个都是 32 位毫秒计数，约 49.7 天回绕一次，按无符号差值计算
    return ((_GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def is_locked() -> bool:
    """锁屏或安全桌面（UAC）时，输入桌面不是 Default 或打不开（与 winapi.is_input_desktop_locked 同样的判断，
    这里单独实现，主进程不必加载录制用的窗口模块）。"""
    hdesk = _OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    if not hdesk:
        return True
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD(0)
        if not _GetUserObjectInformationW(hdesk, _UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return False
        return buf.value.lower() != "default"
    finally:
        _CloseDesktop(hdesk)


class IdleWatcher:
    """键鼠空闲 >= threshold_s 或锁屏 → 空闲。状态变化时回调（在监视线程里调用，回调里不要长时间阻塞）。

    idle_fn / locked_fn / clock 供测试替换；poll() 也可以手动调用（测试不起线程）。
    """

    def __init__(self, threshold_s: float = 300.0, *, interval_s: float = 5.0,
                 on_idle_start: Callable[[str], None] | None = None, on_idle_end: Callable[[], None] | None = None,
                 idle_fn: Callable[[], float] = idle_seconds, locked_fn: Callable[[], bool] = is_locked):
        self.threshold_s, self.interval_s = threshold_s, interval_s
        self.on_idle_start, self.on_idle_end = on_idle_start, on_idle_end
        self._idle_fn, self._locked_fn = idle_fn, locked_fn
        self.idle = False
        self.reason: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def check(self) -> tuple[bool, str | None]:
        """当前是否空闲及原因（locked / input）。查询失败时按"不空闲"处理。"""
        try:
            if self._locked_fn():
                return True, "locked"
            if self._idle_fn() >= self.threshold_s:
                return True, "input"
        except OSError:
            log.warning("查询空闲状态失败", exc_info=True)
        return False, None

    def poll(self) -> bool:
        idle, reason = self.check()
        if idle and not self.idle:
            self.idle, self.reason = True, reason
            self._fire(self.on_idle_start, reason)
        elif not idle and self.idle:
            self.idle, self.reason = False, None
            self._fire(self.on_idle_end)
        elif idle:
            self.reason = reason
        return self.idle

    @staticmethod
    def _fire(cb, *args) -> None:
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:  # noqa: BLE001 - 回调出错不能停掉监视线程
            log.exception("空闲回调出错")

    def start(self) -> "IdleWatcher":
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="screenrecall-idle", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


def main() -> int:
    """python -m screenrecall.app.idle：打印当前空闲秒数和锁屏状态。"""
    print(f"键鼠空闲 {idle_seconds():.1f} 秒，锁屏：{'是' if is_locked() else '否'}")
    t0 = time.monotonic()
    for _ in range(100):
        idle_seconds()
        is_locked()
    print(f"每次查询约 {(time.monotonic() - t0) * 10_000:.1f} 微秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
