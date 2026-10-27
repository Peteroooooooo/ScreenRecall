"""截图来源：Windows.Graphics.Capture（windows-capture 2.0.1）为主，PrintWindow 兜底。

WGC 只在窗口画面变了时送帧，窗口被挡住也照样送。windows-capture 2.0.1 打开后改不了
minimum_update_interval，所以会话一律按 100 ms（最多 10 帧/秒）打开，再在 Python 里按
set_max_fps() 的值限速：平时 ≤ 1 帧/秒，滚轮在转时 ≤ 10 帧/秒。

回调里只做一件事：把这一帧的 BGRA 原样拷进一个"只留最新一张"的槽（缓冲重复使用），记下时间和
窗口边框，从不保留库给的 frame_buffer。转灰度、算指纹都在分析线程里 take() 之后做。
一律 cursor_capture=False、draw_border=False。

两种来源接口相同：start() / stop() / take(now_ms) / set_max_fps() / closed / error / frames_seen。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable

import numpy as np

from . import win32
from .clock import Clock, now_ms
from .frames import Frame, FrameMap

log = logging.getLogger(__name__)

UPDATE_INTERVAL_MS = 100  # 会话打开时的最小间隔；之后只能在 Python 里限速


_mta_lock = threading.Lock()
_mta_cookie = None


def keep_mta_alive() -> bool:
    """让进程的多线程 COM 套间（MTA）一直活着（CoIncrementMTAUsage，只做一次）。

    实测 windows-capture 2.0.1：没有任何线程持有 MTA 时，开过会话的线程一退出（或主线程上的
    Tk 根窗口销毁），MTA 就被拆掉，库里缓存的 WinRT 对象随之失效，下一次 start_free_threaded
    访问冲突、整个进程崩掉。先占住 MTA 后同样的场景反复跑都正常。
    """
    global _mta_cookie
    with _mta_lock:
        if _mta_cookie is not None:
            return True
        try:
            import ctypes

            cookie = ctypes.c_void_p()
            if ctypes.windll.ole32.CoIncrementMTAUsage(ctypes.byref(cookie)) != 0:
                return False
        except (OSError, AttributeError):
            return False
        _mta_cookie = cookie
        return True


class _BufferPool:
    """几块同尺寸的 BGRA 缓冲轮流用。别人（某个 Frame）还拿着的缓冲不会被覆盖。"""

    def __init__(self, keep: int = 3):
        self._keep = keep
        self._bufs: list[np.ndarray] = []

    def get(self, shape: tuple[int, ...]) -> np.ndarray:
        self._bufs = [b for b in self._bufs if b.shape == shape]
        for b in self._bufs:
            # 引用：列表 1 + 循环变量 1 + getrefcount 参数 1；再多就是有帧还在用
            if sys.getrefcount(b) <= 3:
                return b
        buf = np.empty(shape, dtype=np.uint8)
        if len(self._bufs) < self._keep:
            self._bufs.append(buf)
        return buf


class _SlotSource:
    """"只留最新一张"的交接槽和 Python 侧限速，两种来源共用。"""

    kind = "base"

    def __init__(self, hwnd: int, on_frame: Callable[[int], None] | None, on_closed: Callable[[int], None] | None,
                 max_fps: float, clock: Clock | None):
        self.hwnd = int(hwnd)
        self._on_frame = on_frame
        self._on_closed = on_closed
        self._clock = clock or now_ms
        self._lock = threading.Lock()
        self._slot: tuple[np.ndarray, int, tuple[int, int, int, int] | None, bool] | None = None
        self._min_gap_ms = 0
        self.set_max_fps(max_fps)
        self._last_take = -10**12
        self.closed = False
        self.error: str | None = None
        self.frames_seen = 0
        self.frames_taken = 0
        self.started_ms: int | None = None
        self.first_frame_ms: int | None = None
        self._seq = 0

    def set_max_fps(self, fps: float) -> None:
        self._min_gap_ms = int(1000 / fps) if fps and fps > 0 else 0

    @property
    def max_fps(self) -> float:
        return 1000 / self._min_gap_ms if self._min_gap_ms else float("inf")

    def _put(self, pixels: np.ndarray, t: int, bounds, is_bgra: bool) -> None:
        with self._lock:
            self._slot = (pixels, t, bounds, is_bgra)
            self.frames_seen += 1
            if self.first_frame_ms is None:
                self.first_frame_ms = t
        if self._on_frame is not None:
            try:
                self._on_frame(self.hwnd)
            except Exception:  # noqa: BLE001 - 通知失败不能影响截图线程
                log.exception("on_frame 通知失败")

    def pending(self) -> bool:
        return self._slot is not None

    def take(self, now: int | None = None) -> Frame | None:
        """取走最新一帧（限速：距离上次取走不到 1/max_fps 秒时先不给，下次再取）。"""
        now = self._clock() if now is None else now
        with self._lock:
            if self._slot is None or now - self._last_take < self._min_gap_ms:
                return None
            pixels, t, bounds, is_bgra = self._slot
            self._slot = None
            self._last_take = now
            self.frames_taken += 1
            self._seq += 1
            seq = self._seq
        h, w = pixels.shape[:2]
        fmap = FrameMap.from_bounds(bounds, w, h)
        if is_bgra:
            return Frame.from_bgra(self.hwnd, t, pixels, fmap, seq=seq, source=self.kind)
        return Frame.from_bgr(self.hwnd, t, pixels, fmap, seq=seq, source=self.kind)

    def discard(self) -> None:
        """丢掉槽里还没取走的帧（例如窗口刚命中排除规则）。"""
        with self._lock:
            self._slot = None

    def _closed(self) -> None:
        self.closed = True
        if self._on_closed is not None:
            try:
                self._on_closed(self.hwnd)
            except Exception:  # noqa: BLE001
                log.exception("on_closed 通知失败")


class CaptureSession(_SlotSource):
    """一个窗口的 WGC 截图会话。"""

    kind = "wgc"

    def __init__(self, hwnd: int, on_frame: Callable[[int], None] | None = None,
                 on_closed: Callable[[int], None] | None = None, max_fps: float = 1.0, *,
                 clock: Clock | None = None, bounds: Callable[[int], tuple[int, int, int, int] | None] | None = None):
        super().__init__(hwnd, on_frame, on_closed, max_fps, clock)
        self._bounds = bounds or win32.frame_bounds
        self._pool = _BufferPool()
        self._cap = None
        self._control = None
        self._stopping = False

    def start(self) -> None:
        from windows_capture import WindowsCapture  # 用到时才导入：单元测试和 PrintWindow 兜底不需要它

        cap = WindowsCapture(cursor_capture=False, draw_border=False, minimum_update_interval=UPDATE_INTERVAL_MS,
                             window_hwnd=self.hwnd)

        @cap.event
        def on_frame_arrived(frame, control):
            if self._stopping:
                control.stop()
                return
            try:
                src = frame.frame_buffer
                buf = self._pool.get(src.shape)
                np.copyto(buf, src)
                t = self._clock()
                self._put(buf, t, self._bounds(self.hwnd), True)
            except Exception as exc:  # noqa: BLE001 - 回调里的异常会让库停掉会话
                self.error = f"{type(exc).__name__}: {exc}"

        @cap.event
        def on_closed():
            self._closed()

        self.started_ms = self._clock()
        self._cap = cap  # 会话对象要一直留着：被回收时原生线程可能还在用它
        keep_mta_alive()
        self._control = cap.start_free_threaded()

    def stop(self) -> None:
        self._stopping = True
        ctl, self._control = self._control, None
        if ctl is not None:
            try:
                ctl.stop()
                ctl.wait()
            except Exception as exc:  # noqa: BLE001 - 窗口已经关掉时停止会报错
                log.debug("停止 WGC 会话：%s", exc)
        with self._lock:
            self._slot = None

    @property
    def finished(self) -> bool:
        """库的截图线程已经结束（窗口关了、出错了或已停止）。"""
        ctl = self._control
        if ctl is None:
            return True
        try:
            return bool(ctl.is_finished())
        except Exception:  # noqa: BLE001
            return True


class PrintWindowSource(_SlotSource):
    """兜底：按 max_fps 定时 PrintWindow，画面变了才放进槽里，行为上模仿 WGC"只在变化时送帧"。"""

    kind = "printwindow"

    def __init__(self, hwnd: int, on_frame: Callable[[int], None] | None = None,
                 on_closed: Callable[[int], None] | None = None, max_fps: float = 1.0, *,
                 clock: Clock | None = None, grab: Callable[[int], object] | None = None):
        super().__init__(hwnd, on_frame, on_closed, max_fps, clock)
        self._grab = grab
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_bytes: bytes | None = None

    def start(self) -> None:
        if self._grab is None:
            from .. import winapi

            self._grab = winapi.capture_window
        self.started_ms = self._clock()
        self._thread = threading.Thread(target=self._loop, name=f"sr-printwindow-{self.hwnd:x}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2.0)
        self._thread = None
        with self._lock:
            self._slot = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            self.poll_once()
            if self.closed:
                return
            gap = (self._min_gap_ms or 100) / 1000
            self._stop.wait(max(0.01, gap - (time.monotonic() - t0)))

    def poll_once(self) -> bool:
        """截一次；画面和上次不同就放进槽。返回是否放了新帧。"""
        try:
            cap = self._grab(self.hwnd)
        except Exception as exc:  # noqa: BLE001 - CaptureError 等
            reason = getattr(exc, "reason", type(exc).__name__)
            if reason == "window_gone":
                self._closed()
            else:
                self.error = str(reason)
            return False
        self.error = None
        if cap.bgr == self._last_bytes:
            return False
        self._last_bytes = cap.bgr
        img = np.frombuffer(cap.bgr, dtype=np.uint8).reshape(cap.height, cap.width, 3)
        self._put(img, self._clock(), tuple(cap.rect), False)
        return True

    @property
    def finished(self) -> bool:
        return self._thread is None or not self._thread.is_alive()


def open_source(hwnd: int, kind: str, *, on_frame=None, on_closed=None, max_fps: float = 1.0,
                clock: Clock | None = None) -> _SlotSource:
    """按种类建一个截图来源并启动。kind: 'wgc' | 'printwindow'。启动失败时抛异常。"""
    cls = CaptureSession if kind == "wgc" else PrintWindowSource
    src = cls(hwnd, on_frame, on_closed, max_fps, clock=clock)
    src.start()
    return src
