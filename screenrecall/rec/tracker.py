"""每个窗口的快路径判断：块和位置、静止、候选帧、什么时候存证据帧。

每来一帧：
1. 和上一帧逐行比；完全一样、或只在一直变的小块（视频、时钟）里变 → 丢掉，静止计时不受影响；
2. motion.analyze 判断：平移（位置 += N）、原地变（位置不变）、接不上（开新块，位置归 0，
   最近的滚轮方向说明新块在旧块之前还是之后）；
3. 如果有候选帧，并且它身上"还没存进任何证据帧"的内容在新的一帧里找不到了（滚出屏幕 / 原地被换掉）
   → 发 evidence 事件，让集成方马上把候选帧存成证据帧；
4. 新的一帧只是"最新帧"，下一帧来了就被替换。

计时：距离最后一次有效变化 1 秒（最近 2 秒内按过键时为 2 秒）没有新帧 → "静止"：
- 和某张已存证据帧逐像素相同（一直变的小块除外）→ 只发 still(identical_to_saved=True)，集成方只更新"最后看到"；
- 否则发 still（集成方此时用 UIA 读字），有"还没存过"的内容时把它留作唯一的候选帧。

"有没有存过"按像素判断：每张已存证据帧记下它每条竖带每一行的指纹和它在块里的位置。候选帧的某个格子
（某行 × 某条竖带）算存过，当且仅当某张已存证据帧在同一块同一位置、或在同一帧行（标题栏、工具栏这种
不跟着滚的部分）有同样的指纹。纯色格子不算内容。

块号、静止编号都是本窗口内的整数，由集成方映射到数据库 id。所有事件都在调用 feed()/tick() 的线程里同步发出，
Sink 的实现应当只把事件放进队列，不要在回调里做耗时工作。
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Iterator, Protocol

import numpy as np

from .frames import Frame, FrameMap
from .inputmon import WheelEvent
from .motion import Fp, Motion, analyze, content_cells, rows_equal, tile_changes
from .volatile import VolatileConfig, VolatileTiles

log = logging.getLogger(__name__)


# ---------- 事件 ----------

@dataclass
class StillEvent:
    """一次"静止"。gray / bgr 是这一帧的灰度图和原图；fmap 把帧像素换算成屏幕坐标。

    still_id：本窗口内递增的编号；evidence 事件引用的就是某次 still。
    identical_to_saved：和 saved_ref 那次已存证据帧逐像素相同（一直变的小块除外）。
    new_cells / new_rows：还没存进任何证据帧的内容格子数、涉及的行数。
    """

    hwnd: int
    t_ms: int
    block: int
    pos_y: int
    pos_x: int
    frame: Frame
    fmap: FrameMap
    identical_to_saved: bool
    still_id: int
    saved_ref: int | None = None
    new_cells: int = 0
    new_rows: int = 0
    rejoined_from: int | None = None  # 认出是旧块的画面、换回旧块时，原来的块号

    @property
    def gray(self) -> np.ndarray:
        return self.frame.gray

    @property
    def bgr(self) -> np.ndarray:
        return self.frame.bgr


class Sink(Protocol):
    """录制核心发出的事件。全部是同步调用，实现方只应入队。"""

    def block_started(self, hwnd: int, block: int, t_ms: int, after_block: int | None,
                      direction: str | None) -> None:
        """开了新块。after_block 是之前所在的块；direction 'down' = 新块在它之后，'up' = 之前，None = 不知道。"""

    def block_anchor(self, hwnd: int, block: int, anchor: str, t_ms: int, pos_y: int) -> None:
        """滚轮在转、画面不动：anchor 'top'（往上滚，pos_y 就是顶）或 'bottom'（往下滚，到底了）。"""

    def still(self, ev: StillEvent) -> None:
        """画面静止了。"""

    def evidence(self, ev: StillEvent, reason: str) -> None:
        """马上把这次静止的画面存成证据帧。reason: 'leaving' 滚出屏幕 / 'replaced' 原地被换掉 / 'closing' 窗口不再跟踪
        / 'needs_ocr' 集成方读不到字、要求现在就存（RecorderCore.request_evidence）。"""

    def skim(self, hwnd: int, block: int, t0_ms: int, t1_ms: int, direction: str, distance_px: int,
             y0: int, y1: int) -> None:
        """没停下来的一段滚动：从块内位置 y0 滚到 y1，共滚过 distance_px 像素。"""

    def gap(self, hwnd: int, t0_ms: int, t1_ms: int | None, reason: str) -> None:
        """缺口开始（t1_ms=None）或结束。reason: minimized / locked / excluded / closed / cloaked / paused / stopped。"""


class NullSink:
    def block_started(self, *a, **k) -> None: ...
    def block_anchor(self, *a, **k) -> None: ...
    def still(self, ev: StillEvent) -> None: ...
    def evidence(self, ev: StillEvent, reason: str) -> None: ...
    def skim(self, *a, **k) -> None: ...
    def gap(self, *a, **k) -> None: ...


class ListSink:
    """把事件按顺序记成 (名字, 参数字典)，测试和调试用。"""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def block_started(self, hwnd, block, t_ms, after_block, direction):
        self.events.append(("block_started", dict(hwnd=hwnd, block=block, t_ms=t_ms, after_block=after_block,
                                                  direction=direction)))

    def block_anchor(self, hwnd, block, anchor, t_ms, pos_y):
        self.events.append(("block_anchor", dict(hwnd=hwnd, block=block, anchor=anchor, t_ms=t_ms, pos_y=pos_y)))

    def still(self, ev):
        self.events.append(("still", dict(ev=ev)))

    def evidence(self, ev, reason):
        self.events.append(("evidence", dict(ev=ev, reason=reason)))

    def skim(self, hwnd, block, t0_ms, t1_ms, direction, distance_px, y0, y1):
        self.events.append(("skim", dict(hwnd=hwnd, block=block, t0_ms=t0_ms, t1_ms=t1_ms, direction=direction,
                                         distance_px=distance_px, y0=y0, y1=y1)))

    def gap(self, hwnd, t0_ms, t1_ms, reason):
        self.events.append(("gap", dict(hwnd=hwnd, t0_ms=t0_ms, t1_ms=t1_ms, reason=reason)))

    def names(self) -> list[str]:
        return [n for n, _ in self.events]

    def of(self, name: str) -> list[dict]:
        return [d for n, d in self.events if n == name]

    def clear(self) -> None:
        self.events.clear()


# ---------- 设置 ----------

@dataclass(frozen=True)
class TrackerConfig:
    still_ms: int = 1000           # 多久没有新帧算静止
    typing_still_ms: int = 2000    # 最近按过键时多久算静止
    min_new_cells: int = 4         # 至少这么多"没存过的内容格子"才值得留候选 / 存证据
    max_saved: int = 64            # 每个窗口记住多少张已存证据帧的指纹（4K 下每张约 0.2 MB）
    anchor_wait_ms: int = 500      # 滚轮转了这么久画面都没动，才算到顶 / 到底
    anchor_min_wheel: int = 2      # 至少几次滚轮
    wheel_recent_ms: int = 1500    # 接不上时，这么近的滚轮方向才拿来判断前后
    volatile: VolatileConfig = field(default_factory=VolatileConfig)


# ---------- 内部 ----------

@dataclass
class _Saved:
    still_id: int
    block: int
    pos_y: int
    pos_x: int
    shape: tuple[int, int]
    v: np.ndarray        # (STRIPS, 高) 竖带指纹
    content: np.ndarray  # (STRIPS, 高) 有内容的格子


@dataclass
class _Candidate:
    ev: StillEvent
    new: np.ndarray      # (STRIPS, 高) 还没存过的内容格子


@dataclass
class _Run:
    t0: int
    t1: int
    y0: int
    y1: int
    x0: int
    x1: int
    distance: int = 0


class WindowTracker:
    """一个窗口的状态机。只在一个线程里调用（录制核心的分析线程）。"""

    def __init__(self, hwnd: int, sink: Sink, config: TrackerConfig | None = None, *,
                 block_ids: Iterator[int] | None = None, still_ids: Iterator[int] | None = None):
        """block_ids / still_ids：编号发生器。录制核心传共用的，使编号在整个进程里唯一；默认从 0 开始。"""
        self.hwnd = hwnd
        self.sink = sink
        self.config = config or TrackerConfig()
        self.block = -1
        self.pos_y = 0
        self.pos_x = 0
        self._block_ids = block_ids if block_ids is not None else itertools.count()
        self._still_ids = still_ids if still_ids is not None else itertools.count()
        self._latest: Frame | None = None
        self._last_change = 0
        self._stilled = True
        self._cand: _Candidate | None = None
        self._saved: list[_Saved] = []
        self._vol = VolatileTiles(self.config.volatile)
        self._run: _Run | None = None
        self._last_key: int | None = None
        self._wheel_t: int | None = None
        self._wheel_dir: str | None = None
        self._wheel_pending: list[int] = []
        self._anchored: tuple[int, str, int] | None = None
        self._gap: tuple[int, str] | None = None
        self._prompt = False  # prompt()：拿到下一帧就静止判断
        self._prompt_fed = False
        self.closed = False
        # 统计
        self.frames = 0
        self.dropped = 0
        self.last_motion: Motion | None = None

    # ---- 状态 ----

    @property
    def in_gap(self) -> str | None:
        return self._gap[1] if self._gap else None

    @property
    def candidate(self) -> StillEvent | None:
        return self._cand.ev if self._cand else None

    @property
    def latest(self) -> Frame | None:
        return self._latest

    @property
    def saved_count(self) -> int:
        return len(self._saved)

    @property
    def last_change_ms(self) -> int:
        """最后一次有效变化（被接受的那一帧）的时间。别的线程读一个整数是安全的。"""
        return self._last_change

    def request_evidence(self, still_id: int) -> bool:
        """集成方要求现在就把这次静止存成证据帧（例如 UIA 读不到字、要登记待 OCR）。

        只有它还是当前候选帧时才存（reason 'needs_ocr'），并记进"已存"，之后不会再因为离开屏幕存第二次；
        已经存过、被换掉或不值得存的，什么都不做，返回 False。
        """
        c = self._cand
        if c is None or self.closed or c.ev.still_id != still_id:
            return False
        self._save_candidate("needs_ocr")
        return True

    def drop_candidate(self) -> bool:
        """丢掉内存里的候选帧，不存（暂停、规则变化、隐私删除时的卫生处理）。"""
        had = self._cand is not None
        self._cand = None
        return had

    def prompt(self) -> None:
        """窗口刚变得能录（刚钉住、缺口刚结束）：拿到下一帧就马上做一次静止判断，不等 still_ms，
        画面和之前一样（"same"）也做 —— 一直不变的窗口没有"下一次变化"，第一屏要不然永远不读。
        下一帧是平移 / 原地变 / 接不上时按平常的计时（画面还在动）。"""
        if not self.closed:
            self._prompt = True
            self._prompt_fed = False

    def _typing(self, now: int) -> bool:
        return self._last_key is not None and now - self._last_key < self.config.typing_still_ms

    # ---- 输入 ----

    def on_key(self, t_ms: int) -> None:
        """前台窗口按过键（只有时间）。"""
        self._last_key = t_ms if self._last_key is None else max(self._last_key, t_ms)

    def on_wheel(self, ev: WheelEvent) -> None:
        # 缺口里的滚轮不算：否则缺口结束后，这些旧滚轮会被当成"滚轮在转、画面不动"，误报到顶 / 到底
        if ev.horizontal or ev.delta == 0 or self.closed or self._gap is not None:
            return
        self._wheel_t = ev.t_ms
        self._wheel_dir = "up" if ev.delta > 0 else "down"
        self._wheel_pending.append(ev.t_ms)

    # ---- 帧 ----

    def feed(self, frame: Frame) -> str:
        """喂一帧，返回这一帧的处理结果：'first' / 'same' / 'volatile' / 'shift' / 'inplace' / 'break' / 'ignored'。"""
        kind = self._feed(frame)
        if self._prompt and kind != "ignored":
            if kind in ("first", "same", "volatile"):
                self._prompt_fed = True
                if self._latest is not None:
                    self._stilled = False  # 下一次 tick 就做静止判断
            else:
                self._prompt = False  # 画面在动：按平常的计时
        return kind

    def _feed(self, frame: Frame) -> str:
        if self.closed or self._gap is not None:
            return "ignored"
        self.frames += 1
        t = frame.t_ms
        prev = self._latest
        if prev is None:
            self._start_block(t, None)
            self._accept(frame, t)
            return "first"
        tiles = None
        if prev.shape != frame.shape:
            motion = Motion("break", None, None, "尺寸变了", 0.0)
        else:
            rows_eq = rows_equal(prev.fp, frame.fp)
            if rows_eq.all():
                self.dropped += 1
                return "same"
            tiles = tile_changes(prev.fp, frame.fp)
            if self._vol.only_volatile(tiles, t):
                if not self._typing(t):
                    self._vol.look(tiles, t)
                self._latest = frame
                self.dropped += 1
                return "volatile"
            mask = self._vol.mask(t, *frame.shape)
            motion = analyze(prev.fp, frame.fp, mask, rows_eq=rows_eq, tiles=tiles)
        self.last_motion = motion
        if motion.kind in ("same", "inplace"):
            if tiles is not None and not self._typing(t):
                self._vol.look(tiles, t)
        else:
            self._vol.reset()
        if motion.kind == "same":
            self._latest = frame
            self.dropped += 1
            return "volatile"

        if motion.kind == "break":
            new_block, new_y, new_x = True, 0, 0
        else:
            new_block, new_y, new_x = False, self.pos_y + motion.dy, self.pos_x + motion.dx
        if self._cand is not None:
            self._check_candidate(frame, motion.kind, new_y, new_x, t)

        if motion.kind == "shift":
            self._extend_run(t, motion.dy, motion.dx, new_y, new_x)
            self.pos_y, self.pos_x = new_y, new_x
        elif motion.kind == "break":
            self._end_run()
            direction = self._wheel_dir if self._wheel_t is not None and t - self._wheel_t <= self.config.wheel_recent_ms else None
            self._start_block(t, direction)
        self._accept(frame, t)
        return motion.kind

    def _accept(self, frame: Frame, t: int) -> None:
        # 旧的最新帧不主动放原图：它可能已经随 still 事件交给了接收方，没人引用时自然回收
        self._latest = frame
        self._last_change = t
        self._stilled = False
        self._wheel_pending.clear()

    def _start_block(self, t: int, direction: str | None) -> None:
        after = self.block if self.block >= 0 else None
        self.block = next(self._block_ids)
        self.pos_y = self.pos_x = 0
        self._anchored = None
        self.sink.block_started(self.hwnd, self.block, t, after, direction)

    # ---- 滚动段 ----

    def _extend_run(self, t: int, dy: int, dx: int, new_y: int, new_x: int) -> None:
        if self._run is None:
            self._run = _Run(t, t, self.pos_y, new_y, self.pos_x, new_x)
        r = self._run
        r.t1, r.y1, r.x1 = t, new_y, new_x
        r.distance += abs(dy) + abs(dx)

    def _end_run(self) -> None:
        r, self._run = self._run, None
        if r is None or r.distance <= 0:
            return
        ny, nx = r.y1 - r.y0, r.x1 - r.x0
        if abs(ny) >= abs(nx):
            direction = "down" if ny > 0 else "up"
        else:
            direction = "right" if nx > 0 else "left"
        self.sink.skim(self.hwnd, self.block, r.t0, r.t1, direction, r.distance, r.y0, r.y1)

    # ---- 候选帧和已存证据帧 ----

    def _vol_cells(self, fp: Fp, t: int) -> np.ndarray | None:
        m = self._vol.mask(t, *fp.shape)
        return None if m is None else m.v

    def _uncovered(self, fp: Fp, content: np.ndarray, vol: np.ndarray | None) -> np.ndarray:
        """还没存进任何证据帧的内容格子 (STRIPS, 高)。"""
        h, w = fp.shape
        need = content.copy()
        if vol is not None:
            need &= ~vol
        if not need.any():
            return need
        covered = np.zeros_like(need)
        for sv in self._saved:
            if sv.shape[1] != w:
                continue
            if sv.shape[0] == h:
                covered |= sv.v == fp.v  # 同一帧行（不跟着滚的部分、原样回来的画面）
            if sv.block == self.block and sv.pos_x == self.pos_x:
                off = self.pos_y - sv.pos_y  # 本帧第 r 行 ↔ 那张的第 r + off 行
                r0, r1 = max(0, -off), min(h, sv.shape[0] - off)
                if r1 > r0:
                    covered[:, r0:r1] |= sv.v[:, r0 + off: r1 + off] == fp.v[:, r0:r1]
        return need & ~covered

    def _find_identical(self, fp: Fp, vol: np.ndarray | None) -> _Saved | None:
        """最近存过的、和这一帧逐像素相同（一直变的格子除外）的证据帧。"""
        for sv in reversed(self._saved):
            if sv.shape != fp.shape:
                continue
            diff = sv.v != fp.v
            if vol is not None:
                diff &= ~vol
            if not diff.any():
                return sv
        return None

    def _check_candidate(self, frame: Frame, kind: str, new_y: int, new_x: int, t: int) -> None:
        """候选帧里没存过的内容在新帧里还在不在；不在了就存。

        "还在"：新帧按新位置对上的那一行、或同一帧行（不跟着滚的部分）有同样的指纹。
        原因：画面在平移 → 'leaving'；原地变 → 'replaced'；接不上时滚轮刚转过 → 'leaving'，否则 'replaced'。
        """
        c = self._cand
        ev = c.ev
        fp, cfp = frame.fp, ev.frame.fp
        h = fp.shape[0]
        present = np.zeros_like(c.new)
        if fp.shape == cfp.shape:
            present |= fp.v == cfp.v
        if kind != "break" and ev.block == self.block and ev.pos_x == new_x and fp.shape[1] == cfp.shape[1]:
            off = ev.pos_y - new_y  # 候选第 r 行 ↔ 新帧第 r + off 行
            r0, r1 = max(0, -off), min(cfp.shape[0], h - off)
            if r1 > r0:
                present[:, r0:r1] |= fp.v[:, r0 + off: r1 + off] == cfp.v[:, r0:r1]
        lost = c.new & ~present
        if int(lost.sum()) < self.config.min_new_cells:
            return
        if kind == "shift":
            reason = "leaving"
        elif kind == "break" and self._wheel_t is not None and t - self._wheel_t <= self.config.wheel_recent_ms:
            reason = "leaving"
        else:
            reason = "replaced"
        self._save_candidate(reason)

    def _save_candidate(self, reason: str) -> None:
        c, self._cand = self._cand, None
        if c is None:
            return
        ev = c.ev
        fp = ev.frame.fp
        self._saved.append(_Saved(ev.still_id, ev.block, ev.pos_y, ev.pos_x, fp.shape, fp.v, content_cells(fp)))
        if len(self._saved) > self.config.max_saved:
            del self._saved[0]
        self.sink.evidence(ev, reason)

    # ---- 计时 ----

    def tick(self, now: int) -> None:
        """定时调用（录制核心每 25 ms 左右一次）：到顶/到底判断、静止判断。"""
        if self.closed or self._gap is not None or self._latest is None:
            return
        self._check_anchor(now)
        if self._stilled:
            return
        if self._prompt and self._prompt_fed:
            self._do_still(now)
            return
        need = self.config.typing_still_ms if self._typing(now) else self.config.still_ms
        if now - self._last_change >= need:
            self._do_still(now)

    def _check_anchor(self, now: int) -> None:
        p = self._wheel_pending
        if len(p) < self.config.anchor_min_wheel or self._wheel_dir is None:
            return
        wait = self.config.anchor_wait_ms
        if now - p[0] < wait or now - self._last_change < wait:
            return
        anchor = "top" if self._wheel_dir == "up" else "bottom"
        p.clear()
        key = (self.block, anchor, self.pos_y)
        if self._anchored == key:
            return
        self._anchored = key
        self.sink.block_anchor(self.hwnd, self.block, anchor, now, self.pos_y)

    def _do_still(self, now: int) -> None:
        self._stilled = True
        self._prompt = self._prompt_fed = False
        self._end_run()
        frame = self._latest
        fp = frame.fp
        vol = self._vol_cells(fp, now)
        sid = next(self._still_ids)
        same = self._find_identical(fp, vol)
        if same is not None:
            rejoined = None
            if same.block != self.block:
                rejoined = self.block
                self.block, self.pos_y, self.pos_x = same.block, same.pos_y, same.pos_x
                self._anchored = None
            _ = frame.bgr
            ev = StillEvent(self.hwnd, now, self.block, self.pos_y, self.pos_x, frame, frame.fmap, True, sid,
                            saved_ref=same.still_id, rejoined_from=rejoined)
            self._cand = None  # 这一帧的内容都存过了，旧候选里没存过的部分也在这一帧里 → 不用再留
            self.sink.still(ev)
            return
        new = self._uncovered(fp, content_cells(fp), vol)
        n = int(new.sum())
        _ = frame.bgr  # 在分析线程里转好原图，事件接收方只读，不会两个线程同时去转
        ev = StillEvent(self.hwnd, now, self.block, self.pos_y, self.pos_x, frame, frame.fmap, False, sid,
                        new_cells=n, new_rows=int(new.any(axis=0).sum()))
        self._cand = _Candidate(ev, new) if n >= self.config.min_new_cells else None
        self.sink.still(ev)

    # ---- 缺口和结束 ----

    def suspend(self, t_ms: int) -> None:
        """不再跟踪这个窗口（转到后台、会话停了）：候选帧里没存过的内容先存下。"""
        self._end_run()
        if self._cand is not None:
            self._save_candidate("closing")

    def gap_start(self, t_ms: int, reason: str) -> None:
        if self._gap is not None or self.closed:
            return
        self.suspend(t_ms)
        if reason == "excluded":
            # 命中排除规则：刚收到、还没静止过的画面可能已经是要排除的内容，直接丢掉
            self._latest = None
            self._stilled = True
        self._gap = (t_ms, reason)
        self._wheel_pending.clear()
        self.sink.gap(self.hwnd, t_ms, None, reason)

    def gap_end(self, t_ms: int) -> None:
        if self._gap is None:
            return
        t0, reason = self._gap
        self._gap = None
        self.sink.gap(self.hwnd, t0, t_ms, reason)

    def close(self, t_ms: int, reason: str = "closed") -> None:
        """窗口关了（或录制停止）：存下候选帧，记一个没有结束时间的缺口。"""
        if self.closed:
            return
        if self._gap is not None:
            self.gap_end(t_ms)
        self.suspend(t_ms)
        self.closed = True
        self.sink.gap(self.hwnd, t_ms, None, reason)
        self._latest = None
