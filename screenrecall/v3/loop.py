"""v3 捕获循环（录制进程的分析线程）：前台、名单三档、会话池、拍的时机、灰色时段、走开。

节拍约 100 ms（来新帧、按复制时提前叫醒）。每一轮：
1. 暂停（set_paused）/ 锁屏（windows.locked()，500 ms 查一次）→ 关会话、FocusEvent(0)、GreyEvent paused / locked（开始 / 结束各一次）；
2. 前台：桌面 / 开始菜单（windows.is_shell）、最小化 / 被隐藏 → FocusEvent(0)，什么都不留；
   进程不在名单（tier == never）→ FocusEvent(0) + GreyEvent unlisted(app)（切走时结束）；
   无痕窗口（source.is_private_title）或命中不录窗口（source.title_excluded）→ FocusEvent(0)，什么都不留；
   否则是要录的前台：(hwnd, 标题) 变了 → FocusEvent(hwnd, pid, app, title) + trigger.reset。同一窗口只是标题变了时
   等它稳定 cfg.title_settle_ms 再报（进度百分比这类标题不刷屏），等的这段时间这个窗口先不拍；
3. 后台：tier == always 的进程的可见顶层窗口（每 2 秒列一次，最多 cfg.max_background_windows 个）也录；
4. rec.sessions.SessionPool 跟上要录的窗口（rec.wgc.open_source，cfg.fps）；在录的窗口取最新帧喂给各自的 SnapTrigger。
   前台换走后会话还会延迟约 3 秒才停，这期间不取它的帧（不在看的画面不录），槽里的最新一帧留着，切回来时接着用；
5. 打字：inputs.last_typing_ms 变了 → 记到当前前台的页面 (hwnd, 标题)；这个页面 cfg.typing_quiet_ms 内打过字时先不拍；
6. 剪贴板：前台是要录的窗口 → 先一个 reason='copy' 的 SnapEvent（同样受打字规则限制），再发 CopyEvent（文字截到
   cfg.clip_max_chars）；拍完的 SnapEvent 在前，处理线程就能把复制记到这一拍的出处和条目上；
7. trigger.tick 出了原因 → SnapEvent(…, frame=trigger.latest, changed=trigger.snapped(t))；
8. 走开：idle_seconds() ≥ cfg.away_ms 时记下走开起点（现在 - idle）；又有输入时发 AwayEvent(起点, 回来的时间)。
   走开期间前台换了（自动锁屏、暂停、窗口自己跳出来）：先发 AwayEvent(起点, 现在)（扣给旧的那条），起点挪到现在。

所有软件一套规则：进程名只和名单（数据）比，从不按软件名写分支。事件只放进队列，不碰数据库、不读 UIA。
测试注入 windows / inputs / source_factory / clock / window_list，直接调 step(now)。
"""

from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from typing import Callable, Sequence

from ..rec.sessions import SessionPool
from .config import V3Config
from .trigger import SnapTrigger
from .types import (GREY_LOCKED, GREY_PAUSED, GREY_UNLISTED, REASON_COPY, AwayEvent, CopyEvent, FocusEvent,
                    GreyEvent, SnapEvent)

log = logging.getLogger(__name__)

STEP_MS = 100          # 分析线程节拍
LOCK_POLL_MS = 500     # 多久查一次锁屏
LIST_POLL_MS = 2000    # 多久列一次"一直录"的后台窗口

WindowList = Callable[[], list[tuple[int, str, str]]]  # [(hwnd, 进程名, 标题)]


def real_window_list() -> list[tuple[int, str, str]]:
    from .. import winapi

    return [(w.hwnd, w.process, w.title) for w in winapi.list_windows()]


def _title_checks():
    """(is_private_title, title_excluded)：text 单元的 v3.source 里的纯函数（懒导入；测试可以换掉这个函数）。"""
    from .source import is_private_title, title_excluded

    return is_private_title, title_excluded


class CaptureLoop:
    """分析线程。见模块说明。"""

    def __init__(self, cfg: V3Config, out: "queue.Queue", *, windows=None, inputs=None, source_factory=None,
                 clock=None, window_list: WindowList | None = None):
        from ..rec.clock import now_ms

        self.cfg = cfg
        self.out = out
        self.clock = clock or now_ms
        self._wake_evt = threading.Event()
        self._watcher = None
        if windows is None:
            from ..rec.windows import ForegroundWatcher, RealWindows

            windows = RealWindows()
            self._watcher = ForegroundWatcher(self.wake)
        self.windows = windows
        if inputs is None:
            from ..rec.inputmon import InputMonitor

            inputs = InputMonitor(self.clock)
        self.inputs = inputs
        if source_factory is None:
            from ..rec.wgc import open_source

            def source_factory(hwnd: int, kind: str):
                return open_source(hwnd, kind, on_frame=lambda _h: self.wake(), on_closed=lambda _h: self.wake(),
                                   max_fps=cfg.fps, clock=self.clock)
        self.pool = SessionPool(source_factory)
        self.window_list = window_list or real_window_list
        self.triggers: dict[int, SnapTrigger] = {}

        # 别的线程设的（control 线程、剪贴板线程）：只是赋值 / 入队，下一轮生效
        self._tiers: dict[str, str] = {}
        self._title_kw: tuple[str, ...] = ()
        self._paused = False
        self._copies: deque[tuple[str, int, int | None]] = deque(maxlen=16)

        # 只在分析线程里改
        self._checks = None
        self._locked = False
        self._last_lock_poll = -10**15
        self._global: str | None = None            # 开着的 paused / locked 灰色时段
        self._unlisted: str | None = None          # 开着的 unlisted 灰色时段的 app
        self._fg: tuple | None = None              # 已报的前台 (hwnd, pid, app, title)；None = 没有要录的前台
        self._fg_pending: tuple[tuple, int] | None = None  # 同一窗口标题变了、还在等它稳定：((hwnd, pid, app, title), 起点)
        self._bg: list[tuple[int, int | None, str, str]] = []
        self._last_list = -10**15
        self._typing_at: dict[tuple[int, str], int] = {}
        self._last_typing_seen: int | None = None
        self._away_start: int | None = None

        self._step_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.counts: dict[str, int] = {"steps": 0, "frames": 0, "copies": 0, "copies_dropped": 0, "away": 0}

    # ---------- 别的线程调 ----------

    def set_tiers(self, tiers: dict[str, str]) -> None:
        """名单（control 线程在 rev 变了时调）：{进程名: tier}，不在里面的 = never。"""
        self._tiers = {str(k).lower(): str(v) for k, v in (tiers or {}).items()}
        self._last_list = -10**15  # 后台窗口下一轮重列
        self.wake()

    def set_excludes(self, title_keywords: Sequence[str]) -> None:
        self._title_kw = tuple(str(k) for k in title_keywords or () if str(k).strip())
        self._last_list = -10**15
        self.wake()

    def set_paused(self, paused: bool) -> None:
        if bool(paused) != self._paused:
            self._paused = bool(paused)
            self.wake()

    def on_copy(self, text: str, t_ms: int, owner_pid: int | None = None) -> None:
        """给 ClipboardWatcher（剪贴板线程里调）：只入队，下一轮处理。owner_pid：剪贴板属主的进程号（不知道时 None）。"""
        if text:
            self._copies.append((text, int(t_ms), owner_pid))
            self.wake()

    def wake(self) -> None:
        self._wake_evt.set()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def state(self) -> str:
        """recording / paused / locked（心跳用）。"""
        return "paused" if self._paused else "locked" if self._locked else "recording"

    @property
    def current(self) -> tuple[str, str] | None:
        """现在前台在录的窗口 (app, 标题)；没在录时 None（心跳用）。"""
        fg = self._fg
        return (fg[2], fg[3]) if fg is not None else None

    @property
    def current_hwnd(self) -> int | None:
        """现在前台在录的窗口句柄；没在录时 None（心跳用：这个页面按网址跳过了就不写标题）。"""
        fg = self._fg
        return fg[0] if fg is not None else None

    def stats(self) -> dict:
        with self._step_lock:
            snaps = sum(v for k, v in self.counts.items() if k.startswith("snap_"))
            return {**self.counts, "snaps": snaps, "sessions": len(self.pool), "background": len(self._bg),
                    "opened": self.pool.opened, "fallbacks": self.pool.fallbacks}

    # ---------- 线程 ----------

    def start(self) -> None:
        if self._thread is not None:
            return
        if self._watcher is not None:
            self._watcher.start()
        self.inputs.start()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="sr-v3-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停下分析线程；收尾（发走开、FocusEvent(0)、结束灰色时段、关会话）在线程退出前做，不和 step() 并发。"""
        self._stop_evt.set()
        self.wake()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(3.0)
            if th.is_alive():
                log.warning("捕获循环 3 秒内没有退出")
        else:
            self.shutdown()
        try:
            self.inputs.stop()
        except Exception:  # noqa: BLE001
            log.exception("停止输入监测失败")
        if self._watcher is not None:
            self._watcher.stop()

    def shutdown(self, now: int | None = None) -> None:
        with self._step_lock:
            now = self.clock() if now is None else now
            if self._away_start is not None:
                self._emit(AwayEvent(self._away_start, max(self._away_start, now)))
                self._away_start = None
            self._set_focus(None, now)
            self._set_unlisted(None, now)
            self._set_global(None, now)
            self._close_all()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001 - 一轮出错不能让录制停掉
                log.exception("捕获循环这一轮出错")
            self._wake_evt.wait(STEP_MS / 1000)
            self._wake_evt.clear()
        try:
            self.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("捕获循环收尾出错")

    # ---------- 事件 ----------

    def _emit(self, ev) -> None:
        self.out.put(ev)

    def _set_global(self, reason: str | None, now: int) -> None:
        if reason == self._global:
            return
        if self._global is not None:
            self._emit(GreyEvent(now, self._global, end=True))
        self._global = reason
        if reason is not None:
            self._emit(GreyEvent(now, reason))

    def _set_unlisted(self, app: str | None, now: int) -> None:
        if app == self._unlisted:
            return
        if self._unlisted is not None:
            self._emit(GreyEvent(now, GREY_UNLISTED, self._unlisted, end=True))
        self._unlisted = app
        if app is not None:
            self._emit(GreyEvent(now, GREY_UNLISTED, app))

    def _set_focus(self, fg: tuple | None, now: int) -> None:
        """fg = (hwnd, pid, app, title) 或 None。和已报的不同才发 FocusEvent。"""
        self._fg_pending = None
        if fg == self._fg:
            return
        if self._away_start is not None and self._fg is not None:
            # 走开的时候前台换了（走开后自动锁屏、暂停、别的窗口自己跳到前面）：已经走开的这一段先报给旧的那条，
            # 之后的接着从现在算（回来时再报）
            self._emit(AwayEvent(self._away_start, max(self._away_start, now)))
            self._away_start = now
        self._fg = fg
        if fg is None:
            self._emit(FocusEvent(now))
            return
        self._emit(FocusEvent(now, fg[0], fg[1], fg[2], fg[3]))
        self._trigger(fg[0]).reset(now)

    def _trigger(self, hwnd: int) -> SnapTrigger:
        tr = self.triggers.get(hwnd)
        if tr is None:
            tr = self.triggers[hwnd] = SnapTrigger(self.cfg)
        return tr

    def _close_all(self) -> None:
        self.pool.close_all()
        self.triggers.clear()
        self._bg = []
        self._last_list = -10**15

    # ---------- 一轮 ----------

    def step(self, now: int | None = None) -> None:
        with self._step_lock:
            self._step(self.clock() if now is None else now)

    def _title_skip(self, title: str) -> bool:
        if self._checks is None:
            self._checks = _title_checks()
        is_private, excluded = self._checks
        return bool(is_private(title, self.cfg.private_title_keywords) or excluded(title, self._title_kw))

    def _pid(self, hwnd: int) -> int | None:
        fn = getattr(self.windows, "pid", None)
        try:
            return fn(hwnd) if fn else None
        except Exception:  # noqa: BLE001
            return None

    def _tier(self, app: str) -> str:
        return self._tiers.get(app.lower(), "never")

    def _step(self, now: int) -> None:
        self.counts["steps"] += 1
        self._check_away(now)
        if now - self._last_lock_poll >= LOCK_POLL_MS:
            self._last_lock_poll = now
            try:
                self._locked = bool(self.windows.locked())
            except Exception:  # noqa: BLE001
                self._locked = False
        glob = GREY_PAUSED if self._paused else GREY_LOCKED if self._locked else None
        if glob is not None:
            self._set_focus(None, now)
            self._set_unlisted(None, now)
            self._set_global(glob, now)
            if len(self.pool):
                self._close_all()
            self._copies.clear()
            return
        self._set_global(None, now)
        if self._watcher is not None:
            self._watcher.drain()  # 钩子只用来叫醒；状态每一轮都直接查

        # 前台
        fg, unlisted = self._classify_foreground()
        self._set_unlisted(unlisted, now)
        self._note_foreground(fg, now)
        settled = self._fg if self._fg_pending is None else None

        # 后台："一直录"的窗口
        if now - self._last_list >= LIST_POLL_MS:
            self._last_list = now
            self._bg = self._list_background()
        fg_h = self._fg[0] if self._fg else 0
        recording: dict[int, tuple[int | None, str, str, bool]] = {}  # 在录的窗口 → (pid, app, title, 后台)
        if settled:
            recording[fg_h] = (settled[1], settled[2], settled[3], False)
        for h, pid, app, title in self._bg:
            if h != fg_h and h not in recording:
                recording[h] = (pid, app, title, True)
        wanted = ([fg_h] if fg_h else []) + [h for h in recording if h != fg_h]

        # 会话：新开的会话换一个新的 SnapTrigger（旧的最新帧可能早就过时了），拿到第一帧就开始计"停下"，
        # 画面一直不变的窗口（刚切进来、后台窗口刚开始录）第一屏也拍
        started, _stopped = self.pool.update(wanted, now)
        for h in started:
            self.triggers.pop(h, None)
            self._trigger(h).reset(now)
        for h in [h for h in self.triggers if h not in self.pool]:
            del self.triggers[h]

        # 打字（先于取帧：正在打字的页面不学"一直在变的小块"）
        lt = getattr(self.inputs, "last_typing_ms", None)
        if lt is not None and lt != self._last_typing_seen:
            self._last_typing_seen = lt
            if self._fg is not None:
                self._typing_at[(self._fg[0], self._fg[3])] = lt
        quiet = self.cfg.typing_quiet_ms
        for k in [k for k, t in self._typing_at.items() if now - t >= quiet]:
            del self._typing_at[k]

        # 帧：只取在录窗口的；延迟停的会话不取（槽里留着最新一帧）
        for h, src in self.pool.sources().items():
            if h not in recording:
                continue
            f = src.take(now)
            if f is not None:
                self.counts["frames"] += 1
                _pid, _app, title, bg = recording[h]
                self._trigger(h).feed(f, f.t_ms, typing=(not bg) and self._typing(h, title, now))

        # 复制
        copies = []
        while self._copies:
            copies.append(self._copies.popleft())
        for text, t, owner in copies:
            cur = self._fg  # 标题还在等稳定时也记复制（标题用已报的），只是先不拍
            # 属主是别的进程：后台程序（剪贴板历史、远程桌面同步……）写的，不是在这个窗口里复制的
            if cur is None or owner is not None and cur[1] is not None and owner != cur[1]:
                self.counts["copies_dropped"] += 1
                continue
            h, pid, app, title = cur
            tr = self.triggers.get(h)
            if settled and tr is not None and tr.latest is not None and not self._typing(h, title, now):
                self._snap(h, pid, app, title, False, tr, now, REASON_COPY)
            self.counts["copies"] += 1
            self._emit(CopyEvent(t, h, app, title, text[: self.cfg.clip_max_chars]))

        # 拍
        for h, (pid, app, title, bg) in recording.items():
            tr = self.triggers.get(h)
            if tr is None:
                continue
            reason = tr.tick(now, typing=(not bg) and self._typing(h, title, now))
            if reason is not None:
                self._snap(h, pid, app, title, bg, tr, now, reason)

    def _typing(self, hwnd: int, title: str, now: int) -> bool:
        t = self._typing_at.get((hwnd, title))
        return t is not None and now - t < self.cfg.typing_quiet_ms

    def _snap(self, h: int, pid, app: str, title: str, bg: bool, tr: SnapTrigger, now: int, reason: str) -> None:
        frame = tr.latest
        changed = tr.snapped(now)
        self.counts["snap_" + reason] = self.counts.get("snap_" + reason, 0) + 1
        self._emit(SnapEvent(h, pid, app, title, now, reason, frame, bg, changed))

    def _classify_foreground(self) -> tuple[tuple | None, str | None]:
        """(要录的前台 (hwnd, pid, app, title) 或 None, 不在名单的 app 或 None)。"""
        w = self.windows
        h = w.foreground()
        if not h or w.is_shell(h) or w.state(h).gap_reason is not None:
            return None, None
        desc = w.describe(h)
        if desc is None:
            return None, None
        app, title = desc[0] or "", desc[1] or ""
        if self._tier(app) == "never":
            return None, app
        if self._title_skip(title):
            return None, None
        return (h, self._pid(h), app, title), None

    def _note_foreground(self, fg: tuple | None, now: int) -> None:
        """换了窗口立刻报；同一窗口只是标题变了，等稳定 title_settle_ms 再报（报的时间是标题开始变的时候）。"""
        cur = self._fg
        if fg == cur:
            self._fg_pending = None
            return
        settle = self.cfg.title_settle_ms
        if fg is not None and cur is not None and fg[0] == cur[0] and settle > 0:
            if self._fg_pending is None or self._fg_pending[0] != fg:
                self._fg_pending = (fg, now)
                return
            if now - self._fg_pending[1] < settle:
                return
            self._set_focus(fg, self._fg_pending[1])
            return
        self._set_focus(fg, now)

    def _list_background(self) -> list[tuple[int, int | None, str, str]]:
        if not any(t == "always" for t in self._tiers.values()):
            return []
        try:
            wins = list(self.window_list())
        except Exception:  # noqa: BLE001
            log.exception("列出窗口失败")
            return []
        out = []
        for h, app, title in wins:
            if len(out) >= self.cfg.max_background_windows:
                break
            if self._tier(app or "") != "always" or self._title_skip(title or ""):
                continue
            try:
                if self.windows.is_shell(h) or self.windows.state(h).gap_reason is not None:
                    continue
            except Exception:  # noqa: BLE001
                continue
            out.append((h, self._pid(h), app, title or ""))
        return out

    def _check_away(self, now: int) -> None:
        try:
            idle_ms = int(float(self.inputs.idle_seconds()) * 1000)
        except Exception:  # noqa: BLE001
            return
        if idle_ms >= self.cfg.away_ms:
            if self._away_start is None:
                self._away_start = now - idle_ms
        elif self._away_start is not None:
            start, self._away_start = self._away_start, None
            self.counts["away"] += 1
            self._emit(AwayEvent(start, max(start, now - idle_ms)))
