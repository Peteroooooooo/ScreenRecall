"""录制进程的读字线程：静止 → UIA 读字、写文字行、算待 OCR 区域；证据帧 → 存图、关联行、登记待 OCR。

分析线程只把事件放进这里的队列（StoreSink），这个线程按先后顺序处理，所以一次静止总是先于它自己的证据帧处理。

静止（不是"和已存的图一模一样"的那种）：
1. 画面在静止之后又变了（核心里这个窗口最后一次变化晚于这一帧），或者队列积压太多 → 不读（读到的是别的画面），
   整帧算待 OCR；
2. 否则 UiaReader.read(hwnd)（对这个窗口的第一次读取没来得及重读时，稍等再读一次）；读完再核对一次画面没变，
   变了就丢掉这次结果；
3. 只收 role='text' 的行；屏幕坐标 → 帧坐标（ev.fmap）→ 块坐标（+ pos_y）。正文（TextPattern 的行）上方的
   固定区域（标签页、地址栏这类）不收，否则每滚一次就在新位置多出一行；
4. 写文字行：只在"读到了可用文字"时写，并且视口只取真正被 UIA 覆盖的横条（去掉 uncovered），
   否则读不到字的区域里之前 OCR 出来的行会被当成"消失了"；
5. uia.ocr_regions 算待 OCR 区域；需要 OCR 且这次静止还是候选帧 → 请核心现在就把它存成证据帧（needs_ocr）。

证据帧：save_frame（无损 WebP，captured_ms = 静止时间）+ 这次静止读到的行（帧坐标）+ 这次静止算出的待 OCR 区域。
和已存的图一模一样的静止：frame_seen(那张图)。

每次静止的结果（行号、帧坐标、区域）按 still_id 暂存，证据帧来了取走；数量有上限，最旧的先丢。
屏幕文字从不写进日志，只记次数和耗时。
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..rec.tracker import StillEvent
from ..store.writer import BlockGone, LineIn, Writer
from ..text import uia
from .rec_sink import StillCtx, StoreSink, _failed

log = logging.getLogger(__name__)

_NO_READ = uia.UiaResult("error", [], False, 0.0, error="not read")


class CoreLink(Protocol):
    def last_change_ms(self, hwnd: int) -> int | None: ...
    def request_evidence(self, hwnd: int, still_id: int) -> None: ...
    def can_read(self, hwnd: int) -> bool: ...


@dataclass
class StillInfo:
    line_ids: list[int | None]
    rects: list[tuple[int, int, int, int]]  # 帧坐标，和 line_ids 对齐
    regions: list[tuple[int, int, int, int]]  # 待 OCR，帧坐标
    app: str
    title: str
    ref: Any


@dataclass
class TextStats:
    stills: int = 0
    identical: int = 0
    uia_reads: int = 0
    uia_usable: int = 0
    uia_not_ok: int = 0  # busy / timeout / error
    first_touch_rereads: int = 0  # 第一次读一个窗口时稍等又读了一次
    stale: int = 0  # 画面已经变了，没读或丢掉了结果
    blocked: int = 0  # 暂停 / 锁屏 / 窗口此刻不能录（最小化、被隐藏、命中排除），没读或丢掉了结果
    excluded_drops: int = 0  # 证据帧的窗口标题此刻命中排除，没存
    backlog_skips: int = 0
    no_hint: int = 0
    lines_sent: int = 0
    ocr_stills: int = 0
    evidence_requested: int = 0
    frames_saved: int = 0
    regions_registered: int = 0
    frame_seen: int = 0
    block_gone: int = 0
    errors: int = 0
    uia_ms: list = field(default_factory=list)
    db_ms: list = field(default_factory=list)
    still_ms: list = field(default_factory=list)

    def summary(self) -> dict:
        out = {k: v for k, v in dataclasses.asdict(self).items() if not isinstance(v, list)}
        for name in ("uia_ms", "db_ms", "still_ms"):
            xs = sorted(getattr(self, name))
            if xs:
                out[name] = {"n": len(xs), "p50": round(xs[len(xs) // 2], 1),
                             "p95": round(xs[min(len(xs) - 1, int(len(xs) * 0.95))], 1), "max": round(xs[-1], 1)}
        return out

    def add(self, name: str, v: float) -> None:
        xs = getattr(self, name)
        xs.append(v)
        if len(xs) > 4096:
            del xs[:2048]


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out if b > a]


def covered_spans(top: int, bottom: int, holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[top, bottom) 去掉 holes 后剩下的纵向区间。"""
    out = []
    cur = top
    for a, b in _merge([(max(top, a), min(bottom, b)) for a, b in holes]):
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if bottom > cur:
        out.append((cur, bottom))
    return out


def stored_lines(res, fmap) -> tuple[list[tuple[str, tuple[int, int, int, int], bool]], int]:
    """一次读字结果里要存成文字行的那些：[(文字, 帧坐标矩形, 是否 TextPattern 可见行)]，以及正文顶边（帧 y）。

    只收 role 'text'（界面文字由 uia.collect_lines 标成 'ui'）；正文上方的固定区域（标签页、地址栏）不收：
    有 TextPattern 正文时，正文第一行以上的都算固定区域。"""
    conv: list[tuple[str, tuple[int, int, int, int], bool]] = []
    for ln in res.lines:
        if ln.role != "text" or not ln.text.strip():
            continue
        r = fmap.rect_to_frame(*ln.rect)
        if r is None:
            continue
        conv.append((ln.text, r, ln.source_detail.startswith("textpattern")))
    tp_idx = [i for i, (_, _, tp) in enumerate(conv) if tp]
    content_top = min(conv[i][1][1] for i in tp_idx) if tp_idx else 0
    return [c for c in conv if c[1][1] >= content_top], content_top


class TextWorker:
    def __init__(self, writer: Writer, reader, *, core: CoreLink | None = None, sink: StoreSink | None = None,
                 max_pending_stills: int = 8, keep_stills: int = 256, db_timeout_s: float = 10.0,
                 min_gap_px: int = 48, caption_px: Callable[[int], int] | None = None):
        self.writer = writer
        self.reader = reader
        self.core = core
        self.sink = sink
        self.max_pending_stills = max_pending_stills
        self.keep_stills = keep_stills
        self.db_timeout_s = db_timeout_s
        self.min_gap_px = min_gap_px
        # 系统画的标题栏高度（屏幕物理像素）：待 OCR 的区域从它下面开始（见 uia.ocr_regions 的 top）
        self.caption_px = caption_px if caption_px is not None else (lambda _hwnd: 0)
        self.stats = TextStats()
        self._q: deque[tuple[str, Any, Any]] = deque()
        self._cv = threading.Condition()
        self._pending_stills = 0
        self._info: OrderedDict[int, StillInfo] = OrderedDict()
        self._frames: OrderedDict[int, str] = OrderedDict()  # still_id → 存下的 frame_id（frame_seen 用）
        self._thread: threading.Thread | None = None
        self._stop = False
        self._stopping = False  # 收尾：不再读 UIA，剩下的静止整帧待 OCR、证据帧照存
        self._busy = False

    # ---------------- 分析线程调用 ----------------

    def put_still(self, ctx: StillCtx) -> None:
        with self._cv:
            if not ctx.ev.identical_to_saved:
                self._pending_stills += 1
                if self._pending_stills > self.max_pending_stills:
                    # 积压了：最旧的、还没处理的那次静止不再去读（读到的也早不是那个画面了）
                    for kind, item, _ in self._q:
                        if kind == "still" and not item.skip_uia and not item.ev.identical_to_saved:
                            item.skip_uia = True
                            self.stats.backlog_skips += 1
                            break
            self._q.append(("still", ctx, None))
            self._cv.notify()

    def put_evidence(self, ev: StillEvent, reason: str) -> None:
        with self._cv:
            self._q.append(("evidence", ev, reason))
            self._cv.notify()

    # ---------------- 线程 ----------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sr-recorder-text", daemon=True)
            self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """处理完队列里剩下的事件（不再读 UIA）后停下。"""
        with self._cv:
            self._stopping = True
            self._stop = True
            self._cv.notify_all()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(timeout_s)
            if th.is_alive():
                log.warning("读字线程 %.1f 秒内没有处理完，还剩 %d 个事件", timeout_s, len(self._q))
        else:
            self.drain()

    def pending(self) -> int:
        with self._cv:
            return len(self._q) + (1 if self._busy else 0)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._stop:
                    self._cv.wait()
                if not self._q:
                    return
            self.process_one()

    def drain(self) -> int:
        n = 0
        while self.process_one():
            n += 1
        return n

    def process_one(self) -> bool:
        """处理队列里的一个事件（测试可以不开线程直接调）。队列空时返回 False。"""
        with self._cv:
            if not self._q:
                return False
            kind, item, extra = self._q.popleft()
            if kind == "still" and not item.ev.identical_to_saved:
                self._pending_stills -= 1
            self._busy = True
        try:
            if kind == "still":
                self._still(item)
            else:
                self._evidence(item, extra)
        except Exception:  # noqa: BLE001 - 一次出错不能让读字线程停掉
            self.stats.errors += 1
            log.exception("处理%s事件出错", "静止" if kind == "still" else "证据帧")
        finally:
            with self._cv:
                self._busy = False
        return True

    # ---------------- 静止 ----------------

    def _changed_since(self, ev: StillEvent) -> bool:
        if self.core is None:
            return False
        last = self.core.last_change_ms(ev.hwnd)
        return last is not None and last > ev.frame.t_ms

    def _newer_queued(self, hwnd: int) -> bool:
        with self._cv:
            return any(k == "still" and it.ev.hwnd == hwnd for k, it, _ in self._q)

    def _still(self, ctx: StillCtx) -> None:
        ev = ctx.ev
        t_start = time.perf_counter()
        if ev.identical_to_saved:
            self.stats.identical += 1
            fid = self._frames.get(ev.saved_ref) if ev.saved_ref is not None else None
            if fid is not None:
                self.writer.frame_seen(fid, ev.t_ms)
                self.stats.frame_seen += 1
            return
        self.stats.stills += 1
        res = None
        fmap = ev.fmap
        stale = ctx.skip_uia or self._stopping or self._changed_since(ev) or self._newer_queued(ev.hwnd)
        if stale:
            if not (ctx.skip_uia or self._stopping):  # 积压另有计数；收尾时本来就不读
                self.stats.stale += 1
        elif not self._can_read(ev.hwnd):
            # 暂停了 / 锁屏 / 窗口此刻最小化、被隐藏或命中排除：屏幕上的已经不是能录的内容，不读
            self.stats.blocked += 1
        elif not self._hint(ev.hwnd):
            self.stats.no_hint += 1
        else:
            res = self._read(ev)
            if res.status not in ("ok", "empty"):
                self.stats.uia_not_ok += 1
            if not self._can_read(ev.hwnd):  # 读的这段时间里暂停了 / 命中排除：结果不要
                self.stats.blocked += 1
                res = None
            elif self._changed_since(ev):
                self.stats.stale += 1
                res = None
            elif res.status in ("ok", "empty"):
                fmap = self._read_fmap(res, ev)
                if fmap is None:  # 窗口大小变了：读到的和这一帧对不上
                    self.stats.stale += 1
                    res = None
        bgr = ev.bgr
        fh, fw = bgr.shape[:2]
        line_ids: list[int | None] = []
        rects: list[tuple[int, int, int, int]] = []
        if res is not None and res.status in ("ok", "empty"):
            uncovered = self._uncovered_frame(res, fmap)
            res_f = dataclasses.replace(res, uncovered=tuple(uncovered))
            top = self._top(ev, res, fmap)
            regions = uia.ocr_regions(res_f, bgr, (0, 0), min_gap_px=self.min_gap_px, top=top)
            if res.usable:
                self.stats.uia_usable += 1
                try:
                    line_ids, rects = self._write_lines(ctx, res, uncovered, fh, fmap)
                except Exception:  # noqa: BLE001 - 字没写进去：这一帧整帧待 OCR，别把文字丢了
                    self.stats.errors += 1
                    log.exception("写文字行失败，改为整帧待 OCR")
                    regions = uia.ocr_regions(_NO_READ, bgr, (0, 0), min_gap_px=self.min_gap_px, top=top)
        else:
            regions = uia.ocr_regions(_NO_READ, bgr, (0, 0), min_gap_px=self.min_gap_px, top=self._top(ev))
        self._info[ev.still_id] = StillInfo(line_ids, rects, [tuple(map(int, r)) for r in regions],
                                            ctx.app, ctx.title, ctx.ref)
        while len(self._info) > self.keep_stills:
            self._info.popitem(last=False)
        if regions:
            self.stats.ocr_stills += 1
            if self.core is not None and not self._stopping:
                self.core.request_evidence(ev.hwnd, ev.still_id)
                self.stats.evidence_requested += 1
        self.stats.add("still_ms", (time.perf_counter() - t_start) * 1000)

    def _read(self, ev: StillEvent):
        """读一次 UIA。读取器说这是对这个窗口的第一次读、预算里没能重读（first_touch：浏览器这时常常只给外框，
        页面还不在辅助功能树里）时，这里稍等再读一次，取元素多的那次 —— 不然一直不变的窗口（刚钉住的）
        第一屏的字永远读不到。"""
        t0 = time.perf_counter()
        res = self.reader.read(ev.hwnd)
        self.stats.add("uia_ms", (time.perf_counter() - t0) * 1000)
        self.stats.uia_reads += 1
        if not getattr(res, "first_touch", False) or res.status not in ("ok", "empty") or self._stopping:
            return res
        cfg = getattr(self.reader, "cfg", None)
        time.sleep(float(getattr(cfg, "first_touch_retry_s", 0.2) or 0.2))
        if self._changed_since(ev) or not self._can_read(ev.hwnd):
            return res
        t0 = time.perf_counter()
        res2 = self.reader.read(ev.hwnd)
        self.stats.add("uia_ms", (time.perf_counter() - t0) * 1000)
        self.stats.uia_reads += 1
        self.stats.first_touch_rereads += 1
        if res2.status in ("ok", "empty") and res2.element_count >= res.element_count:
            return res2
        return res

    def _top(self, ev, res=None, fmap=None) -> int:
        """帧里从第几行开始登记待 OCR（帧像素）：系统标题栏以下；这次读字认出了窗口上部的界面条（标签页条、
        工具栏，uia.chrome_bottom）时从它下面开始，否则页面还没进辅助功能树时整帧识别会把标签页、地址栏识别成正文。"""
        scale = getattr(ev.fmap, "scale_y", 1.0) or 1.0
        try:
            top = max(0, int(int(self.caption_px(ev.hwnd) or 0) / scale))
        except Exception:  # noqa: BLE001
            top = 0
        cb = getattr(res, "chrome_bottom", None) if res is not None else None
        fm = fmap or ev.fmap
        if cb is not None and fm is not None:
            try:
                y = fm.to_frame(fm.left, cb)[1]
            except Exception:  # noqa: BLE001
                y = 0
            if 0 < y < fm.height:
                top = max(top, int(y))
        return top

    def _hint(self, hwnd: int) -> bool:
        fn = getattr(self.reader, "has_text_hint", None)
        try:
            return True if fn is None else bool(fn(hwnd))
        except Exception:  # noqa: BLE001
            return True

    def _can_read(self, hwnd: int) -> bool:
        fn = getattr(self.core, "can_read", None) if self.core is not None else None
        if fn is None:
            return True
        try:
            return bool(fn(hwnd))
        except Exception:  # noqa: BLE001 - 查不了就当不能读
            return False

    @staticmethod
    def _read_fmap(res, ev: StillEvent):
        """UIA 矩形是读字那一刻的屏幕坐标，ev.fmap 是这一帧截到时的窗口位置。窗口在这之间被拖动过
        （内容没变，核心不会收新帧）就按读字时的窗口左上角换算；窗口大小变了返回 None（对不上）。"""
        fmap = ev.fmap
        wr = res.window_rect
        if wr is None:
            return fmap
        bw, bh = round(fmap.width * fmap.scale_x), round(fmap.height * fmap.scale_y)
        if abs((wr[2] - wr[0]) - bw) > 3 or abs((wr[3] - wr[1]) - bh) > 3:
            return None
        if (wr[0], wr[1]) != (fmap.left, fmap.top):
            return dataclasses.replace(fmap, left=int(wr[0]), top=int(wr[1]))
        return fmap

    @staticmethod
    def _uncovered_frame(res, fmap) -> list[tuple[int, int, int, int]]:
        out = []
        for band in res.uncovered:
            r = fmap.rect_to_frame(*band)
            if r is not None:
                out.append(r)
        return out

    @staticmethod
    def _line_ys(conv) -> list[tuple[int, int]]:
        """每行在帧里的纵向范围，用来换算块坐标。

        屏幕边上只露出半截的行：UIA 给的是整行文字、但矩形只有露出的那部分，按它算块里位置会和完整出现时
        差十几像素，同一行就会被当成新的一行。正文最上 / 最下那一行明显比别的行矮时，按常见行高补齐。
        高亮用的帧坐标矩形不变（还是露出的那部分）。
        """
        ys = [(r[1], r[3]) for _, r, _ in conv]
        tp = [i for i, (_, _, is_tp) in enumerate(conv) if is_tp]
        if len(tp) < 3:
            return ys
        hs = sorted(ys[i][1] - ys[i][0] for i in tp)
        med = hs[len(hs) // 2]
        top_i = min(tp, key=lambda i: ys[i][0])
        bot_i = max(tp, key=lambda i: ys[i][1])
        if ys[top_i][1] - ys[top_i][0] < 0.8 * med:
            ys[top_i] = (ys[top_i][1] - med, ys[top_i][1])
        if bot_i != top_i and ys[bot_i][1] - ys[bot_i][0] < 0.8 * med:
            ys[bot_i] = (ys[bot_i][0], ys[bot_i][0] + med)
        return ys

    def _write_lines(self, ctx: StillCtx, res, uncovered, fh: int, fmap) -> tuple[list, list]:
        ev = ctx.ev
        conv, content_top = stored_lines(res, fmap)
        spans = covered_spans(content_top, fh, [(r[1], r[3]) for r in uncovered])
        if not spans:
            return [], []
        ys = self._line_ys(conv)
        pos = ev.pos_y
        groups: list[tuple[tuple[int, int], list[int]]] = [(s, []) for s in spans]
        for i, (_, r, _) in enumerate(conv):
            mid = (r[1] + r[3]) / 2
            for s, idxs in groups:
                if s[0] <= mid < s[1]:
                    idxs.append(i)
                    break
        ids: list[int | None] = [None] * len(conv)
        t0 = time.perf_counter()
        ref = ctx.ref
        for attempt in range(2):
            try:
                futs = []
                for (a, b), idxs in groups:
                    lines = [LineIn(conv[i][0], conv[i][1][0], ys[i][0] + pos, conv[i][1][2], ys[i][1] + pos)
                             for i in idxs]
                    futs.append((idxs, self.writer.record_lines(ref.db_block, ev.t_ms, (a + pos, b + pos), lines)))
                for idxs, f in futs:
                    got = f.result(self.db_timeout_s)
                    for i, lid in zip(idxs, got):
                        ids[i] = lid
                break
            except BlockGone:
                self.stats.block_gone += 1
                if self.sink is None or attempt:
                    raise
                ref = self.sink.renew(ref, ev.t_ms)
                if ref is None:  # 标题这时命中排除：不重建、不写
                    return [], []
                ctx.ref = ref
            except FutureTimeout:
                log.warning("写文字行超过 %.0f 秒没有完成", self.db_timeout_s)
                break
        self.stats.add("db_ms", (time.perf_counter() - t0) * 1000)
        self.stats.lines_sent += len(conv)
        keep = [(lid, conv[i][1]) for i, lid in enumerate(ids) if lid is not None]
        return [k[0] for k in keep], [k[1] for k in keep]

    # ---------------- 证据帧 ----------------

    def _evidence(self, ev: StillEvent, reason: str) -> None:
        info = self._info.pop(ev.still_id, None)
        if info is None:
            # 这次静止没处理过（被丢了）：整帧待 OCR，应用 / 标题 / 库块现取
            if self.sink is None:
                return
            app, title = self.sink.name_of(ev.hwnd)
            ref = self.sink.block_ref(ev.hwnd, ev.block, ev.t_ms, app, title)
            if ref is None:  # 标题此刻命中排除：不存
                self.stats.excluded_drops += 1
                return
            regions = [tuple(map(int, r)) for r in uia.ocr_regions(_NO_READ, ev.bgr, (0, 0),
                                                                   min_gap_px=self.min_gap_px, top=self._top(ev))]
            info = StillInfo([], [], regions, app, title, ref)
        ref = info.ref
        if _failed(ref) and self.sink is not None:
            # 那次静止时建内容源 / 块的写入失败了：图照存，挂到重新建的块上（行号也都无效了，整帧待 OCR）
            ref = self.sink.block_ref(ev.hwnd, ev.block, ev.t_ms, info.app, info.title)
            if ref is None:
                self.stats.excluded_drops += 1
                return
            info = StillInfo([], [], [tuple(map(int, r)) for r in uia.ocr_regions(
                _NO_READ, ev.bgr, (0, 0), min_gap_px=self.min_gap_px, top=self._top(ev))], info.app, info.title, ref)
        fid = self.writer.save_frame(
            ev.bgr, captured_ms=ev.t_ms, segment_id=ref.segment, block_id=ref.db_block, pos_y=ev.pos_y,
            hwnd=ev.hwnd, app=info.app, title=info.title,
            line_ids=info.line_ids or None, line_rects=info.rects or None, ocr_rects=info.regions or None)
        self._frames[ev.still_id] = fid
        while len(self._frames) > 4096:
            self._frames.popitem(last=False)
        self.stats.frames_saved += 1
        self.stats.regions_registered += len(info.regions)
        log.debug("证据帧 still=%d reason=%s regions=%d lines=%d", ev.still_id, reason, len(info.regions),
                  len(info.line_ids))
