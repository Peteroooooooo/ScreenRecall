"""录制进程：把录制核心的事件（rec.tracker.Sink）落到 v2 数据库（store.writer.Writer）。

全部回调都在录制核心的分析线程里同步调用，这里只做两件事，都不等数据库：
- 往写线程排队（Writer 的方法立即返回 Future，后面的操作可以直接引用前面的 Future）；
- 把"静止"和"证据帧"交给读字线程（rec_text.TextWorker），UIA 读字和存图都在那边做。

映射：
- 核心里的块号只在本进程内唯一 → 数据库里的 (segment_id, block_id)。第一次真正要写（静止、划过、到顶/到底）时
  才 segment_for(应用, 标题) + block_new，滚动途中一闪而过、没停下过的块不进库；
- 同一个核心块里标题变了（聊天未读数、网页内跳转但画面接得上）→ 新内容源 + 新的库块，位置沿用核心块的坐标；
- 写库时发现块没了（隐私删除，BlockGone）或检测到新的隐私删除 → 丢掉映射，下次重新建。

缺口：
- 锁屏 / 暂停是全局的：每个窗口的跟踪器都会报一次，这里合成一行（hwnd 为空）；暂停由控制循环自己开关，
  跟踪器报的 paused 不用；
- 最小化 / 被隐藏 / 命中排除：每个窗口一行（带 hwnd、进程名，从不带标题）；
- 窗口关了 / 录制停止：不记成窗口缺口（活动日志已经说明了），录制停止由录制进程记一条全局缺口。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

from ..rec.tracker import StillEvent
from ..store.writer import Writer

log = logging.getLogger(__name__)

EXCLUDED_TITLE = "[已排除]"  # 活动日志 / 心跳里代替被排除窗口的标题
STOPPED_DETAIL = "recorder stopped"           # 缺口 closed：录制进程正常退出到下次启动
NOT_RUNNING_DETAIL = "recorder not running"   # 缺口 error：录制进程没正常退出（崩溃、被杀）到下次启动

# 跟踪器的缺口原因 → (库里的 reason, detail)；不在表里的不记
_WINDOW_GAPS = {"minimized": ("minimised", None), "cloaked": ("minimised", "cloaked"), "excluded": ("excluded", None)}
_GLOBAL_GAPS = ("locked", "paused")


@dataclass
class BlockRef:
    """一个核心块（在某个标题下）对应的库里的内容源和块。segment / db_block 可能还是写线程里的 Future。"""

    hwnd: int
    block: int
    app: str
    title: str
    segment: Any
    db_block: Any


@dataclass
class StillCtx:
    """一次静止交给读字线程时带上的东西：分析线程当时看到的应用、标题和库块。"""

    ev: StillEvent
    app: str
    title: str
    ref: BlockRef
    skip_uia: bool = False  # 读字线程积压了：不再去读（画面早变了），整帧登记待 OCR


def _failed(ref: BlockRef) -> bool:
    """库块映射里的 Future 已经失败（建内容源 / 块的那批写入回滚了）。"""
    for f in (ref.segment, ref.db_block):
        if isinstance(f, Future) and f.done() and (f.cancelled() or f.exception() is not None):
            return True
    return False


class GlobalGaps:
    """锁屏 / 暂停 / 录制停止这类全局缺口：每种原因同时最多一行，开关都是幂等的。任何线程都可以调。"""

    def __init__(self, writer: Writer):
        self.writer = writer
        self._open: dict[str, tuple[int, Future]] = {}
        self._lock = threading.Lock()

    def open(self, reason: str, t_ms: int, detail: str | None = None) -> bool:
        with self._lock:
            if reason in self._open:
                return False
            self._open[reason] = (int(t_ms), self.writer.gap_open(int(t_ms), reason, detail=detail))
            return True

    def close(self, reason: str, t_ms: int) -> bool:
        with self._lock:
            hit = self._open.pop(reason, None)
            if hit is None:
                return False
            self.writer.gap_close(hit[1], max(hit[0], int(t_ms)))
            return True

    def is_open(self, reason: str) -> bool:
        with self._lock:
            return reason in self._open

    def close_all(self, t_ms: int) -> None:
        with self._lock:
            reasons = list(self._open)
        for r in reasons:
            self.close(r, t_ms)


class StoreSink:
    """rec.tracker.Sink 的实现，外加 foreground（活动日志）。text 是读字线程（put_still / put_evidence）。"""

    def __init__(self, writer: Writer, text, gaps: GlobalGaps, *,
                 describe: Callable[[int], tuple[str, str] | None] | None = None,
                 excluded: Callable[[str, str], bool] | None = None,
                 frame_height: Callable[[int], int | None] | None = None, max_blocks: int = 4096):
        """describe(hwnd) -> (进程, 标题)；excluded(进程, 标题) -> 是否命中排除规则；
        frame_height(hwnd) -> 窗口最新一帧的高（划过用）。录制进程把它们接到录制核心上。"""
        self.writer = writer
        self.text = text
        self.gaps = gaps
        self.describe = describe or (lambda _h: None)
        self.excluded = excluded or (lambda _a, _t: False)
        self.frame_height = frame_height or (lambda _h: None)
        self.max_blocks = max_blocks
        self._lock = threading.Lock()
        self._blocks: OrderedDict[tuple[int, int], BlockRef] = OrderedDict()
        self._names: OrderedDict[int, tuple[str, str]] = OrderedDict()  # 窗口 → 最后看到的 (进程, 标题)
        self._window_gaps: dict[tuple[int, int, str], Future] = {}
        self._frame_h: dict[int, int] = {}
        self._cur_lock = threading.Lock()
        # 当前前台（给心跳用）：(hwnd, 进程, 标题 | None, 是否排除, 起始时间)；None = 没有可录的前台窗口
        self._current: tuple[int, str, str | None, bool, int] | None = None
        self.counts = {"blocks": 0, "segments_renewed": 0, "skims": 0, "anchors": 0, "gaps": 0, "activity": 0,
                       "excluded_drops": 0, "failed_refs": 0}

    # ---------------- 名字和块映射 ----------------

    def name_of(self, hwnd: int) -> tuple[str, str]:
        """窗口的 (进程, 标题)；窗口刚关、取不到时用最后一次看到的。"""
        try:
            desc = self.describe(hwnd)
        except Exception:  # noqa: BLE001
            desc = None
        with self._lock:
            if desc is not None:
                self._names[hwnd] = (desc[0] or "", desc[1] or "")
                self._names.move_to_end(hwnd)
                while len(self._names) > 256:
                    self._names.popitem(last=False)
                return self._names[hwnd]
            return self._names.get(hwnd, ("unknown", ""))

    def block_ref(self, hwnd: int, block: int, t_ms: int, app: str | None = None,
                  title: str | None = None) -> BlockRef | None:
        """核心块在当前标题下的库块；还没有（或标题变了）就排队新建。

        标题此刻命中排除规则时返回 None，调用方什么都不写：核心只在窗口是前台 / 钉住时查规则，
        转到后台的窗口晚一秒才到的静止、到顶到底事件可能已经带着要排除的标题。
        """
        if app is None or title is None:
            app, title = self.name_of(hwnd)
        try:
            excluded = bool(self.excluded(app, title))
        except Exception:  # noqa: BLE001 - 查不了规则就当排除
            excluded = True
        if excluded:
            self.counts["excluded_drops"] += 1
            return None
        key = (hwnd, block)
        with self._lock:
            ref = self._blocks.get(key)
            if ref is not None and _failed(ref):
                # 建内容源 / 块的那批写入失败了（整批回滚）：不能一直引用失败的 Future，重新建
                self.counts["failed_refs"] += 1
                ref = None
            if ref is not None and ref.app == app and ref.title == title:
                self._blocks.move_to_end(key)
                return ref
            seg = self.writer.segment_for(app, title, int(t_ms))
            ref = BlockRef(hwnd, block, app, title, seg, self.writer.block_new(seg, int(t_ms)))
            self._blocks[key] = ref
            self._blocks.move_to_end(key)
            while len(self._blocks) > self.max_blocks:
                self._blocks.popitem(last=False)
            self.counts["blocks"] += 1
            return ref

    def renew(self, old: BlockRef, t_ms: int) -> BlockRef | None:
        """库里的块没了（BlockGone）：重新建内容源和块。别的线程已经换过时直接用换好的。"""
        key = (old.hwnd, old.block)
        with self._lock:
            cur = self._blocks.get(key)
            if cur is not None and cur is not old:
                return cur
            self._blocks.pop(key, None)
            self.counts["segments_renewed"] += 1
        return self.block_ref(old.hwnd, old.block, t_ms, old.app, old.title)

    def reset_blocks(self) -> None:
        """有新的隐私删除：丢掉全部映射（被删的内容源可能只剩空壳、标题已清空），之后重新建。"""
        with self._lock:
            self._blocks.clear()

    # ---------------- Sink ----------------

    def block_started(self, hwnd: int, block: int, t_ms: int, after_block: int | None,
                      direction: str | None) -> None:
        # 库块等到第一次要写时才建；前后关系（after_block / direction）库里暂时没有字段，空闲拼块靠文字
        pass

    def block_anchor(self, hwnd: int, block: int, anchor: str, t_ms: int, pos_y: int) -> None:
        ref = self.block_ref(hwnd, block, t_ms)
        if ref is None:
            return
        self.writer.block_anchor(ref.db_block, anchor)
        self.counts["anchors"] += 1

    def still(self, ev: StillEvent) -> None:
        app, title = self.name_of(ev.hwnd)
        self._frame_h[ev.hwnd] = int(ev.frame.shape[0])
        while len(self._frame_h) > 256:
            self._frame_h.pop(next(iter(self._frame_h)))
        ref = self.block_ref(ev.hwnd, ev.block, ev.t_ms, app, title)
        if ref is None:  # 标题命中排除：这次静止不读字、不存图（它的候选帧之后到读字线程时也会被丢掉）
            return
        self.text.put_still(StillCtx(ev, app, title, ref))

    def evidence(self, ev: StillEvent, reason: str) -> None:
        self.text.put_evidence(ev, reason)

    def skim(self, hwnd: int, block: int, t0_ms: int, t1_ms: int, direction: str, distance_px: int,
             y0: int, y1: int) -> None:
        """划过：库里只记"起止两屏都没盖到"的那段内容 [靠上那屏的底, 靠下那屏的顶)，块坐标。

        y0 / y1 是起止两屏顶端的位置，帧高用这个窗口此刻最新那一帧的（= 停下来那一屏；划过在静止 / 断开时、
        新帧收下之前发出），取不到时用最后一次静止的。滚动距离不到一屏时两屏连着，没有漏看的内容，不记。
        两个都没有时退回记两屏顶端之间。
        """
        try:
            h = self.frame_height(hwnd)
        except Exception:  # noqa: BLE001
            h = None
        h = int(h) if h else self._frame_h.get(hwnd, 0)
        lo, hi = min(y0, y1) + h, max(y0, y1)
        if hi <= lo:
            return
        ref = self.block_ref(hwnd, block, t0_ms)
        if ref is None:
            return
        self.writer.skim(ref.segment, ref.db_block, int(t0_ms), int(t1_ms), int(lo), int(hi))
        self.counts["skims"] += 1

    def gap(self, hwnd: int, t0_ms: int, t1_ms: int | None, reason: str) -> None:
        if reason in _GLOBAL_GAPS:
            if reason == "locked":  # 暂停由控制循环开关
                if t1_ms is None:
                    self.gaps.open("locked", t0_ms)
                else:
                    self.gaps.close("locked", t1_ms)
            return
        mapped = _WINDOW_GAPS.get(reason)
        if mapped is None:
            return
        db_reason, detail = mapped
        key = (hwnd, int(t0_ms), reason)
        if t1_ms is None:
            if key in self._window_gaps:
                return
            process = self.name_of(hwnd)[0]
            self._window_gaps[key] = self.writer.gap_open(int(t0_ms), db_reason, detail=detail, hwnd=hwnd,
                                                          process=process)
            self.counts["gaps"] += 1
            return
        fut = self._window_gaps.pop(key, None)
        if fut is not None:
            self.writer.gap_close(fut, int(t1_ms))
        else:
            self.writer.gap(int(t0_ms), int(t1_ms), db_reason, detail=detail, hwnd=hwnd,
                            process=self.name_of(hwnd)[0])
            self.counts["gaps"] += 1

    def foreground(self, t_ms: int, hwnd: int | None, pid: int | None, process: str | None, title: str | None,
                   excluded: bool) -> None:
        """活动日志：新的前台窗口（hwnd None = 桌面 / 锁屏 / 暂停，没有可录的前台）。被排除的窗口不记标题。"""
        if hwnd is None:
            self.writer.activity_end(int(t_ms))
            with self._cur_lock:
                self._current = None
            return
        shown = EXCLUDED_TITLE if excluded else (title or "")
        self.writer.activity_start(int(t_ms), hwnd=hwnd, pid=pid, process=process, title=shown, excluded=excluded)
        self.counts["activity"] += 1
        with self._cur_lock:
            self._current = (hwnd, process or "", None if excluded else (title or ""), bool(excluded), int(t_ms))

    def close_window_gaps(self, t_ms: int) -> None:
        """收尾：还开着的窗口缺口一律在 t_ms 结束（正常情况下跟踪器关闭时已经结束了）。"""
        for key, fut in list(self._window_gaps.items()):
            self.writer.gap_close(fut, int(t_ms))
        self._window_gaps.clear()

    @property
    def current(self) -> tuple[int, str, str | None, bool, int] | None:
        with self._cur_lock:
            return self._current
