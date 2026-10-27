"""录制核心：把前台跟踪、截图会话池、输入监测和每个窗口的 WindowTracker 串起来。

一个分析线程按固定节拍（默认 40 ms，来新帧时提前叫醒）跑 step()：
1. 处理页面发来的命令（暂停 / 继续 / 钉住 / 取消钉住 / 换规则）；
2. 锁屏 → 所有窗口记"locked"缺口并关掉会话；
3. 要录的窗口 = 前台窗口（系统界面除外）+ 钉住的窗口；逐个看状态和规则：关了 / 最小化 / 被隐藏 / 命中排除
   → 记缺口，不录；
4. 会话池跟上（前台换走的窗口延迟约 3 秒才停，停时把它的候选帧存下）；
5. 滚轮事件按光标下的顶层窗口分给对应窗口，滚轮在转的窗口限速放宽到 10 帧/秒，其余 1 帧/秒；
   按键时间交给前台窗口；
6. 每个会话取最新一帧喂给它的 WindowTracker，再给所有窗口 tick() 做静止判断。

块号、静止编号由核心统一分配（整个录制进程内唯一）。所有事件都在分析线程里同步发给 sink，
sink 只应入队；模型、数据库、编码都不在这个线程里。测试时注入时钟、窗口系统、输入和截图来源，直接调 step()。
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from ..config import RulesConfig
from .clock import Clock, now_ms
from .sessions import PoolConfig, SessionPool, SourceFactory
from .tracker import Sink, TrackerConfig, WindowTracker
from .windows import WindowSystem, rule_reason

log = logging.getLogger(__name__)

_GLOBAL_GAPS = ("locked", "paused")


@dataclass(frozen=True)
class RecorderConfig:
    normal_fps: float = 1.0        # 平时（含视频）最多几帧/秒
    wheel_fps: float = 10.0        # 滚轮在转时最多几帧/秒
    wheel_boost_ms: int = 1000     # 最后一次滚轮之后多久内算"在转"
    step_ms: int = 40              # 分析线程节拍
    lock_poll_ms: int = 500        # 多久查一次锁屏
    exist_poll_ms: int = 1000      # 多久查一次不在录的窗口还在不在
    max_trackers: int = 16         # 最多记住几个窗口的状态（多了丢最久没动的）
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)


class RecorderCore:
    def __init__(self, sink: Sink, config: RecorderConfig | None = None, *, rules: RulesConfig | None = None,
                 pinned: tuple[int, ...] = (), clock: Clock | None = None, windows: WindowSystem | None = None,
                 inputs=None, source_factory: SourceFactory | None = None, watcher=None):
        self.sink = sink
        self.config = config or RecorderConfig()
        self.clock = clock or now_ms
        self._wake_evt = threading.Event()
        if windows is None:
            from .windows import ForegroundWatcher, RealWindows

            windows = RealWindows()
            if watcher is None:
                watcher = ForegroundWatcher(self.wake)
        self.windows = windows
        self._watcher = watcher
        if inputs is None:
            from .inputmon import InputMonitor

            inputs = InputMonitor(self.clock)
        self.inputs = inputs
        if source_factory is None:
            from .wgc import open_source

            def source_factory(hwnd: int, kind: str):
                return open_source(hwnd, kind, on_frame=lambda _h: self.wake(), on_closed=lambda _h: self.wake(),
                                   max_fps=self.config.normal_fps, clock=self.clock)
        self.pool = SessionPool(source_factory, self.config.pool)
        self.trackers: dict[int, WindowTracker] = {}
        self._active: dict[int, int] = {}  # 窗口 → 最后一次有动静的时间（淘汰用）
        self._rules = rules
        self._pinned: list[int] = [int(h) for h in pinned]
        self._paused = False
        self._cmd_lock = threading.Lock()
        self._cmds: list[tuple[str, object]] = []
        self._locked = False
        self._last_lock_poll = -10**12
        self._last_exist_poll = -10**12
        self._wheel_at: dict[int, int] = {}
        self._last_key_seen: int | None = None
        self._block_ids = itertools.count()
        self._still_ids = itertools.count()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.steps = 0
        self.frames_fed = 0

    # ---------- 命令（任何线程都可以调） ----------

    def _cmd(self, name: str, arg: object = None) -> None:
        with self._cmd_lock:
            self._cmds.append((name, arg))
        self.wake()

    def pause(self) -> None:
        self._cmd("pause")

    def resume(self) -> None:
        self._cmd("resume")

    def pin(self, hwnd: int) -> None:
        self._cmd("pin", int(hwnd))

    def unpin(self, hwnd: int) -> None:
        self._cmd("unpin", int(hwnd))

    def set_rules(self, rules: RulesConfig | None) -> None:
        self._cmd("rules", rules)

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def pinned(self) -> tuple[int, ...]:
        return tuple(self._pinned)

    def wake(self) -> None:
        self._wake_evt.set()

    # ---------- 线程 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._watcher is not None:
            self._watcher.start()
        self.inputs.start()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="sr-recorder-core", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self.wake()
        if self._thread is not None:
            self._thread.join(5.0)
            self._thread = None
        self.shutdown()
        self.inputs.stop()
        if self._watcher is not None:
            self._watcher.stop()

    def shutdown(self, now: int | None = None) -> None:
        """关掉所有会话；每个窗口存下候选帧并记"stopped"缺口。"""
        now = self.clock() if now is None else now
        self.pool.close_all()
        for tr in self.trackers.values():
            tr.close(now, "stopped")
        self.trackers.clear()

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001 - 一轮出错不能让录制停掉
                log.exception("录制核心这一轮出错")
            self._wake_evt.wait(self.config.step_ms / 1000)
            self._wake_evt.clear()

    # ---------- 一轮 ----------

    def _tracker(self, hwnd: int, now: int) -> WindowTracker:
        tr = self.trackers.get(hwnd)
        if tr is None:
            tr = WindowTracker(hwnd, self.sink, self.config.tracker, block_ids=self._block_ids,
                               still_ids=self._still_ids)
            self.trackers[hwnd] = tr
            self._evict(now)
        self._active[hwnd] = now
        return tr

    def _evict(self, now: int) -> None:
        while len(self.trackers) > self.config.max_trackers:
            idle = [(self._active.get(h, 0), h) for h in self.trackers if h not in self.pool]
            if not idle:
                return
            _, h = min(idle)
            tr = self.trackers.pop(h)
            self._active.pop(h, None)
            if tr.in_gap:
                tr.gap_end(now)
            tr.suspend(now)

    def _drop_tracker(self, hwnd: int, now: int) -> None:
        tr = self.trackers.pop(hwnd, None)
        self._active.pop(hwnd, None)
        if tr is not None:
            tr.close(now, "closed")
        if hwnd in self._pinned:
            self._pinned.remove(hwnd)
        self.pool.forget(hwnd)
        self._wheel_at.pop(hwnd, None)

    def _apply_commands(self, now: int) -> None:
        with self._cmd_lock:
            cmds, self._cmds = self._cmds, []
        for name, arg in cmds:
            if name == "pause" and not self._paused:
                self._paused = True
                self.pool.close_all()
                for tr in self.trackers.values():
                    if tr.in_gap and tr.in_gap != "paused":
                        tr.gap_end(now)
                    tr.gap_start(now, "paused")
            elif name == "resume" and self._paused:
                self._paused = False
                for tr in self.trackers.values():
                    if tr.in_gap == "paused":
                        tr.gap_end(now)
            elif name == "pin" and arg not in self._pinned:
                self._pinned.append(arg)
            elif name == "unpin" and arg in self._pinned:
                self._pinned.remove(arg)
            elif name == "rules":
                self._rules = arg

    def _check_lock(self, now: int) -> bool:
        if now - self._last_lock_poll >= self.config.lock_poll_ms:
            self._last_lock_poll = now
            try:
                self._locked = bool(self.windows.locked())
            except Exception:  # noqa: BLE001
                self._locked = False
        if self._locked:
            if len(self.pool):
                self.pool.close_all()
            for tr in self.trackers.values():
                if tr.in_gap != "locked":
                    if tr.in_gap:
                        tr.gap_end(now)
                    tr.gap_start(now, "locked")
            return True
        for tr in self.trackers.values():
            if tr.in_gap == "locked":
                tr.gap_end(now)
        return False

    def _reason(self, hwnd: int) -> str | None:
        st = self.windows.state(hwnd)
        reason = st.gap_reason
        if reason is not None:
            return reason
        desc = self.windows.describe(hwnd)
        if desc is None:
            return "closed"
        return "excluded" if rule_reason(desc[0], desc[1], self._rules) else None

    def step(self, now: int | None = None) -> None:
        now = self.clock() if now is None else now
        self.steps += 1
        self._apply_commands(now)
        if self._paused:
            return
        if self._check_lock(now):
            return
        if self._watcher is not None:
            self._watcher.drain()  # 钩子只用来叫醒；状态每一轮都直接查

        # 要录哪些窗口
        fg = self.windows.foreground()
        cands: list[int] = []
        if fg and not self.windows.is_shell(fg):
            cands.append(fg)
        cands += [h for h in self._pinned if h != fg]
        wanted: list[int] = []
        for h in cands:
            reason = self._reason(h)
            if reason == "closed":
                self._drop_tracker(h, now)
                continue
            tr = self.trackers.get(h)
            if reason is not None:
                tr = tr or self._tracker(h, now)
                if tr.in_gap != reason:
                    if tr.in_gap:
                        tr.gap_end(now)
                    tr.gap_start(now, reason)
                src = self.pool.sources().get(h)
                if src is not None:
                    src.discard()
                continue
            if tr is not None and tr.in_gap:
                tr.gap_end(now)
            wanted.append(h)
        # 不再是候选的窗口：缺口到此为止（之后只是没在看它）
        for h, tr in self.trackers.items():
            if h not in cands and tr.in_gap and tr.in_gap not in _GLOBAL_GAPS:
                tr.gap_end(now)
        # 不在录的窗口偶尔查一下还在不在
        if now - self._last_exist_poll >= self.config.exist_poll_ms:
            self._last_exist_poll = now
            for h in [h for h in self.trackers if h not in cands]:
                if not self.windows.state(h).exists:
                    self._drop_tracker(h, now)

        started, stopped = self.pool.update(wanted, now)
        for h in started:
            self._tracker(h, now)
        for h in stopped:
            tr = self.trackers.get(h)
            if tr is not None and not tr.in_gap:
                tr.suspend(now)

        # 输入
        sources = self.pool.sources()
        for ev in self.inputs.drain_wheel():
            h = self.windows.root_at(ev.x, ev.y)
            self._wheel_at[h] = ev.t_ms
            tr = self.trackers.get(h)
            if tr is not None and h in sources:
                tr.on_wheel(ev)
        lk = self.inputs.last_key_ms
        if lk is not None and lk != self._last_key_seen:
            self._last_key_seen = lk
            tr = self.trackers.get(fg)
            if tr is not None:
                tr.on_key(lk)

        # 帧
        for h, src in sources.items():
            boost = now - self._wheel_at.get(h, -10**12) < self.config.wheel_boost_ms
            src.set_max_fps(self.config.wheel_fps if boost else self.config.normal_fps)
            tr = self.trackers.get(h)
            if tr is None or tr.in_gap or tr.closed:
                src.discard()
                continue
            f = src.take(now)
            if f is not None:
                self.frames_fed += 1
                self._active[h] = now
                tr.feed(f)
        for tr in self.trackers.values():
            tr.tick(now)

    def stats(self) -> dict:
        return {
            "steps": self.steps,
            "frames_fed": self.frames_fed,
            "sessions": {h: self.pool.kind_of(h) for h in self.pool.sources()},
            "trackers": {h: {"block": t.block, "pos_y": t.pos_y, "frames": t.frames, "dropped": t.dropped,
                             "saved": t.saved_count, "gap": t.in_gap} for h, t in self.trackers.items()},
            "pinned": list(self._pinned),
            "paused": self._paused,
            "locked": self._locked,
        }
