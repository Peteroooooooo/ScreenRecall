"""截图会话池：前台窗口 + 钉住的窗口各一个会话（最多 4 个）。

- 前台换了，原来的窗口不马上停，延迟 ~3 秒（来回切窗口时不用反复开会话；开一个约 170 ms 才出第一帧，
  约 18 MiB 内存、16 个线程）；
- 会话死了（库的线程结束、回调出错、开了很久都没出第一帧）就重开；连续失败几次后换成 PrintWindow 兜底；
- 窗口关了（on_closed）的会话直接移除，由录制核心判断窗口状态并记缺口。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from .frames import Frame

log = logging.getLogger(__name__)


class Source(Protocol):
    hwnd: int
    kind: str
    closed: bool
    error: str | None
    frames_seen: int
    started_ms: int | None
    first_frame_ms: int | None

    def stop(self) -> None: ...
    def take(self, now: int | None = None) -> Frame | None: ...
    def set_max_fps(self, fps: float) -> None: ...
    def discard(self) -> None: ...

    @property
    def finished(self) -> bool: ...


# factory(hwnd, kind) -> 已启动的来源；失败时抛异常
SourceFactory = Callable[[int, str], Source]


@dataclass(frozen=True)
class PoolConfig:
    cap: int = 4                     # 同时最多几个会话
    stop_delay_ms: int = 3000        # 不再需要后延迟多久才停
    first_frame_timeout_ms: int = 3000  # WGC 开了这么久还没出第一帧：算坏了
    max_restarts: int = 2            # WGC 连续重开几次还不行就换 PrintWindow
    fallback: bool = True


@dataclass
class _Entry:
    source: Source
    kind: str
    stop_at: int | None = None
    restarts: int = 0


class SessionPool:
    def __init__(self, factory: SourceFactory, config: PoolConfig | None = None):
        self._factory = factory
        self.config = config or PoolConfig()
        self._entries: dict[int, _Entry] = {}
        self._failures: dict[int, int] = {}  # 每个窗口 WGC 失败的次数（移除后再开也记得）
        self.opened = 0
        self.fallbacks = 0

    def sources(self) -> dict[int, Source]:
        return {h: e.source for h, e in self._entries.items()}

    def kind_of(self, hwnd: int) -> str | None:
        e = self._entries.get(hwnd)
        return e.kind if e else None

    def __contains__(self, hwnd: int) -> bool:
        return hwnd in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def _open(self, hwnd: int) -> _Entry | None:
        fails = self._failures.get(hwnd, 0)
        kinds = ["wgc"] if fails <= self.config.max_restarts else []
        if self.config.fallback:
            kinds.append("printwindow")
        for kind in kinds:
            try:
                src = self._factory(hwnd, kind)
            except Exception as exc:  # noqa: BLE001 - 打不开就试下一种
                log.info("窗口 %x 打开 %s 会话失败：%s", hwnd, kind, exc)
                if kind == "wgc":
                    self._failures[hwnd] = self._failures.get(hwnd, 0) + 1
                continue
            self.opened += 1
            if kind != "wgc":
                self.fallbacks += 1
            return _Entry(src, kind)
        return None

    def _stop(self, hwnd: int) -> None:
        e = self._entries.pop(hwnd, None)
        if e is not None:
            try:
                e.source.stop()
            except Exception:  # noqa: BLE001
                log.exception("停止会话失败")

    def _broken(self, e: _Entry, now: int) -> bool:
        s = e.source
        if s.closed:
            return False
        if s.finished:
            return True
        if e.kind != "wgc":
            return False  # PrintWindow 偶尔截不到（空白、暂时失败）时自己会继续试
        if s.error:
            return True
        if e.kind == "wgc" and s.first_frame_ms is None and s.started_ms is not None:
            return now - s.started_ms > self.config.first_frame_timeout_ms
        return False

    def update(self, wanted: list[int], now: int) -> tuple[list[int], list[int]]:
        """让会话集合跟上 wanted（按优先级排好：前台在最前）。返回 (新开的, 停掉的)。"""
        started: list[int] = []
        stopped: list[int] = []
        want = []
        for h in wanted:
            if h and h not in want:
                want.append(h)
        want = want[: self.config.cap]
        # 1) 关掉的、坏掉的
        for h, e in list(self._entries.items()):
            if e.source.closed:
                self._stop(h)
                stopped.append(h)
                if h in want:
                    want.remove(h)  # 窗口关了：这一轮不再重开，由录制核心判断窗口状态
            elif self._broken(e, now):
                log.info("窗口 %x 的 %s 会话坏了：%s", h, e.kind, e.source.error or "没有帧")
                if e.kind == "wgc":
                    self._failures[h] = self._failures.get(h, 0) + 1
                self._stop(h)
                if h in want:
                    ne = self._open(h)
                    if ne is not None:
                        self._entries[h] = ne
                        started.append(h)
                        continue
                stopped.append(h)
        # 2) 不再需要的：延迟停
        for h, e in list(self._entries.items()):
            if h in want:
                e.stop_at = None
            elif e.stop_at is None:
                e.stop_at = now + self.config.stop_delay_ms
            elif now >= e.stop_at:
                self._stop(h)
                stopped.append(h)
        # 3) 需要但还没有的：超出上限时先停最早要停的
        for h in want:
            if h in self._entries:
                continue
            while len(self._entries) >= self.config.cap:
                waiting = [(e.stop_at, k) for k, e in self._entries.items() if e.stop_at is not None]
                if not waiting:
                    break
                _, victim = min(waiting)
                self._stop(victim)
                stopped.append(victim)
            if len(self._entries) >= self.config.cap:
                break
            e = self._open(h)
            if e is not None:
                self._entries[h] = e
                started.append(h)
        return started, stopped

    def close_all(self) -> list[int]:
        hs = list(self._entries)
        for h in hs:
            self._stop(h)
        return hs

    def forget(self, hwnd: int) -> None:
        """窗口没了：清掉它的失败计数。"""
        self._failures.pop(hwnd, None)
