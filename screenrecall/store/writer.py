"""单写线程：所有写库操作排队，由一个线程按批提交。

约定：
- 调用方（采集、运动分析线程）只把操作放进队列，立即拿到 Future（或预先生成的帧 ID），从不等数据库；
- 写线程每批开一个 BEGIN IMMEDIATE 事务，连续处理队列里的操作，单批最多 batch_budget_ms（默认 30 ms）
  就提交；每个操作在自己的 SAVEPOINT 里，出错只回滚这一个操作，异常放进它的 Future 并记日志；
- Future 在所在批次提交后才完成，所以拿到结果时别的连接已经能读到；
  同一个写线程里后面的操作可以直接引用前面操作的 Future（例如 save_frame(line_ids=record_lines(...))），
  写线程按先后顺序执行，取值不会等待；
- 证据帧的无损 WebP 编码在单独的编码线程里做，编码完再排队把帧状态改成 ok；
- 别的进程同时写（空闲整理进程、主进程）时靠 busy_timeout 排队，事务都很短。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from ..timeutil import utc_ms
from . import schema
from .fts import docs

log = logging.getLogger(__name__)

Rect = tuple[float, float, float, float]

GAP_REASONS = ("locked", "minimised", "excluded", "paused", "closed", "error")
ANCHORS = ("top", "bottom", "none")
SUMMARY_LEVELS = ("piece", "hour", "day")

_CONTROL_JSON = ("pinned_windows", "excluded_processes", "excluded_title_keywords", "hidden_from_agents")
_CONTROL_FIELDS = ("desired_state", "pause_until_ms", "agent_enabled") + _CONTROL_JSON
_HEARTBEAT_FIELDS = ("recorder_state", "recorder_pid", "recorder_note", "current_hwnd", "current_process",
                     "current_title", "current_since_ms")


class BlockGone(ValueError):
    """块不存在（通常是隐私删除把整个内容源删掉了）。录制方收到后应重新 segment_for + block_new。"""


@dataclass(frozen=True)
class LineIn:
    """一行文字，坐标为块标尺上的物理像素（y 以块第一帧顶端为 0）。"""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    source: str = "uia"
    confidence: float = 1.0


@dataclass(frozen=True)
class WriterConfig:
    segment_reuse_ms: int = 30 * 60_000  # 同一应用 + 标题在这段时间内再出现，复用内容源
    match_dy_px: float = 8.0  # 同一行：文字相同且 y0 相差不超过此值
    replace_overlap: float = 0.5  # 新行与刚消失的行纵向重叠比例（按较矮的一行）达到此值视为替换
    batch_budget_ms: float = 30.0  # 单个事务最多连续处理多久
    webp_threads: int = 1
    ocr_max_attempts: int = 3
    claim_stale_ms: int = 10 * 60_000  # running 超过此时间视为领取者已退出，可重新领取


class _Op:
    __slots__ = ("fn", "args", "future", "raw", "quiet")

    def __init__(self, fn: Callable, args: tuple, raw: bool = False, quiet: bool = False):
        self.fn = fn
        self.args = args
        self.future: Future = Future()
        self.raw = raw
        self.quiet = quiet  # 不改变可见内容（心跳）：整批都是这种时不递增 revision，页面不必刷新


_STOP = object()


def _value(x: Any) -> Any:
    """取同一写线程里前序操作的结果（Future 在批次提交后才完成，这里直接读暂存值）。"""
    if isinstance(x, Future):
        if hasattr(x, "_sr_value"):
            return x._sr_value  # type: ignore[attr-defined]
        return x.result(timeout=0)
    return x


def frame_rel_path(frame_id: str, captured_ms: int) -> str:
    """frames/YYYY/MM/DD/<id>.webp（本地日期）。"""
    d = datetime.fromtimestamp(captured_ms / 1000)
    return f"frames/{d:%Y}/{d:%m}/{d:%d}/{frame_id}.webp"


def encode_webp(bgr: np.ndarray) -> bytes:
    """无损 WebP（OpenCV：质量 > 100 即无损）。"""
    import cv2

    ok, buf = cv2.imencode(".webp", bgr, [cv2.IMWRITE_WEBP_QUALITY, 101])
    if not ok:
        raise RuntimeError("WebP 编码失败")
    return buf.tobytes()


class Writer:
    def __init__(self, data_dir: Path | str, config: WriterConfig | None = None,
                 *, clock: Callable[[], int] = utc_ms):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = schema.db_path(self.data_dir)
        self.cfg = config or WriterConfig()
        self.clock = clock
        self._q: queue.Queue = queue.Queue()
        self._encoder = ThreadPoolExecutor(max_workers=self.cfg.webp_threads, thread_name_prefix="store-webp")
        self._encoding: set[Future] = set()  # 还没编码完的帧（flush 要等它们）
        self._enc_lock = threading.Lock()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._closing = False  # 不再接受新的证据帧（编码线程正在收尾）
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="store-writer", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._start_error is not None:
            raise self._start_error

    # ---------------- 线程与批处理 ----------------

    def _submit(self, fn: Callable, *args: Any, raw: bool = False, quiet: bool = False) -> Future:
        if self._closed:
            raise RuntimeError("写线程已关闭")
        op = _Op(fn, args, raw, quiet)
        self._q.put(op)
        return op.future

    def _run(self) -> None:
        try:
            conn = schema.connect(self.db_path)
        except BaseException as e:  # noqa: BLE001
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        try:
            while True:
                item = self._q.get()
                if item is _STOP:
                    break
                if item.raw:
                    self._run_raw(conn, item)
                    continue
                if self._run_batch(conn, item):
                    break
        finally:
            conn.close()

    def _run_raw(self, conn, op: _Op) -> None:
        """事务之外执行；fn 自己开的事务若没结束，回滚并按失败处理（否则之后每批都开不了事务）。"""
        try:
            value = op.fn(conn, *op.args)
            err: BaseException | None = None
        except BaseException as e:  # noqa: BLE001
            value, err = None, e
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            err = err or RuntimeError("操作结束时留下了未提交的事务，已回滚")
        if err is None:
            op.future.set_result(value)
        else:
            log.warning("写库操作 %s 失败：%s", getattr(op.fn, "__name__", op.fn), err)
            op.future.set_exception(err)

    def _begin(self, conn) -> None:
        if conn.in_transaction:  # 不应发生；留着会让 BEGIN 永远失败
            log.warning("写连接上有未结束的事务，已回滚")
            conn.execute("ROLLBACK")
        delay = 0.05
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "locked" not in msg and "busy" not in msg:
                    raise  # 磁盘错误、库损坏等：不重试，交给本批的操作报错
                # 别的进程长时间占着写锁：重试，不丢操作
                log.warning("取得写锁失败，稍后重试：%s", e)
                time.sleep(delay)
                delay = min(delay * 2, 2.0)

    def _run_batch(self, conn, first: _Op) -> bool:
        """处理一批；返回 True 表示读到了停止标记。任何异常都落到本批各操作的 Future 上，写线程不会因此退出。"""
        done: list[tuple[_Op, Any, BaseException | None]] = []
        pending_raw: _Op | None = None
        stop = False
        op: Any = first
        op_err: BaseException | None = None  # 当前操作自己的异常（外层整批作废时仍报给它这个）
        try:
            self._begin(conn)
            t0 = time.perf_counter()
            while True:
                conn.execute("SAVEPOINT op")
                try:
                    value = op.fn(conn, *op.args)
                    conn.execute("RELEASE op")
                except BaseException as e:  # noqa: BLE001
                    op_err = e
                    log.warning("写库操作 %s 失败：%s", getattr(op.fn, "__name__", op.fn), e)
                    # 这两句再失败（严重错误时 SQLite 已回滚整个事务）就走外层：整批作废
                    conn.execute("ROLLBACK TO op")
                    conn.execute("RELEASE op")
                    done.append((op, None, e))
                    op_err = None
                else:
                    op.future._sr_value = value
                    done.append((op, value, None))
                op = None
                if (time.perf_counter() - t0) * 1000 >= self.cfg.batch_budget_ms:
                    break
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    stop = True
                    break
                if nxt.raw:
                    pending_raw = nxt
                    break
                op = nxt
            if any(err is None and not o.quiet for o, _, err in done):
                schema.bump_revision(conn)
            conn.execute("COMMIT")
        except BaseException as e:  # noqa: BLE001
            log.error("本批 %d 个操作作废：%s", len(done) + (op is not None), e)
            if conn.in_transaction:
                try:
                    conn.execute("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
            done = [(o, None, err or RuntimeError(f"同批操作出错，事务已回滚：{e}")) for o, _, err in done]
            if op is not None:
                done.append((op, None, op_err or e))
        self._finish(done)
        if pending_raw is not None:
            self._run_raw(conn, pending_raw)
        return stop

    @staticmethod
    def _finish(done: list) -> None:
        for o, value, err in done:
            if err is None:
                o.future.set_result(value)
            else:
                # 回滚了的结果不能再被后面引用这个 Future 的操作当成真的 ID 用
                if hasattr(o.future, "_sr_value"):
                    del o.future._sr_value
                o.future.set_exception(err)

    def flush(self, timeout: float | None = 30.0) -> None:
        """等到此前排队的操作（含已提交的帧编码）都写完。"""
        with self._enc_lock:
            pending = list(self._encoding)
        _, not_done = futures_wait(pending, timeout)
        if not_done:
            raise TimeoutError(f"还有 {len(not_done)} 张证据帧没编码完")
        self._submit(lambda conn: None, quiet=True).result(timeout)

    def close(self, timeout: float | None = 30.0) -> None:
        if self._closed:
            return
        self._closing = True
        self._encoder.shutdown(wait=True)  # 编码完的帧还要排队改状态，先等编码线程
        self._closed = True
        self._q.put(_STOP)
        self._thread.join(timeout)

    def __enter__(self) -> "Writer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def call(self, fn: Callable[..., Any], *args: Any) -> Future:
        """在写线程的事务里执行 fn(conn, *args)。"""
        return self._submit(fn, *args)

    def run_raw(self, fn: Callable[..., Any], *args: Any) -> Future:
        """在写线程里、事务之外执行 fn(conn, *args)（例如分批删除、检查点）。"""
        return self._submit(fn, *args, raw=True)

    # ---------------- 窗口焦点与缺口 ----------------

    def activity_start(self, t_ms: int, *, hwnd: int | None, pid: int | None, process: str | None,
                       title: str | None, excluded: bool = False) -> Future:
        """新的前台窗口；同时结束上一条未结束的记录。返回 activity_id。"""
        return self._submit(_activity_start, int(t_ms), hwnd, pid, process, title, int(bool(excluded)))

    def activity_end(self, t_ms: int) -> Future:
        return self._submit(_activity_end, int(t_ms))

    def gap_open(self, t_ms: int, reason: str, *, detail: str | None = None, hwnd: int | None = None,
                 process: str | None = None) -> Future:
        if reason not in GAP_REASONS:
            raise ValueError(f"未知缺口原因：{reason}")
        return self._submit(_gap_insert, int(t_ms), None, reason, detail, hwnd, process)

    def gap_close(self, gap_id: int | Future, t_ms: int) -> Future:
        return self._submit(_gap_close, gap_id, int(t_ms))

    def gap(self, start_ms: int, end_ms: int, reason: str, *, detail: str | None = None,
            hwnd: int | None = None, process: str | None = None) -> Future:
        if reason not in GAP_REASONS:
            raise ValueError(f"未知缺口原因：{reason}")
        return self._submit(_gap_insert, int(start_ms), int(end_ms), reason, detail, hwnd, process)

    # ---------------- 内容源与块 ----------------

    def segment_for(self, app: str, title: str, t_ms: int) -> Future:
        """同一应用 + 标题在 segment_reuse_ms 内出现过则复用，否则新建。返回 segment_id。"""
        return self._submit(_segment_for, app or "", title or "", int(t_ms), self.cfg.segment_reuse_ms)

    def segment_volatile(self, segment_id: int | Future, mask: Any) -> Future:
        return self._submit(_segment_volatile, segment_id, json.dumps(mask))

    def block_new(self, segment_id: int | Future, t_ms: int, anchor: str = "none") -> Future:
        if anchor not in ANCHORS:
            raise ValueError(f"未知锚点：{anchor}")
        return self._submit(_block_new, segment_id, int(t_ms), anchor)

    def block_anchor(self, block_id: int | Future, anchor: str) -> Future:
        if anchor not in ANCHORS:
            raise ValueError(f"未知锚点：{anchor}")
        return self._submit(_block_anchor, block_id, anchor)

    def block_merge(self, src: int | Future, dst: int | Future, offset_px: int) -> Future:
        """src 并入 dst：src 坐标 + offset_px = dst 坐标。"""
        return self._submit(_block_merge, src, dst, int(offset_px))

    # ---------------- 文字行 ----------------

    def record_lines(self, block_id: int | Future, t_ms: int, viewport: tuple[float, float],
                     lines: Sequence[LineIn]) -> Future:
        """一次"看清"的文字。返回与 lines 对齐的 line_id 列表（空文字的行为 None）。

        - 与本块仍在的行比：规范化文字相同且 |y0 差| <= match_dy_px → 只更新 last_seen；
        - 没匹配上 → 新插入；
        - 完全落在视口 (top, bottom) 内、这次没出现、且之后没再被看到的行 → 记 disappeared_ms；
        - 新行与这次刚消失的行纵向重叠达到 replace_overlap → replaces_line_id（文字被改写）。
        视口必须是这次真正读过的范围（OCR 只读了一个区域时传该区域在块上的范围）；读失败时不要调用。
        """
        lines = list(lines)
        for ln in lines:
            if ln.source not in ("uia", "ocr"):
                raise ValueError(f"未知文字来源：{ln.source}")
        top, bottom = float(viewport[0]), float(viewport[1])
        return self._submit(_record_lines, block_id, int(t_ms), top, bottom, lines, self.cfg)

    # ---------------- 证据帧 ----------------

    def save_frame(self, bgr: np.ndarray, *, captured_ms: int, segment_id: int | Future | None = None,
                   block_id: int | Future | None = None, pos_y: int | None = None, hwnd: int | None = None,
                   app: str | None = None, title: str | None = None, keep: bool = False,
                   pixel_hash: str | None = None, line_ids: Sequence[int | None] | Future | None = None,
                   line_rects: Sequence[Rect] | None = None, ocr_rects: Sequence[Rect] | None = None) -> str:
        """登记一张证据帧并在编码线程里写无损 WebP；立即返回 frame_id。

        bgr: HxWx3（或 HxWx4，取前 3 通道）uint8，会复制一份，调用方之后可以改写原数组。
        line_ids / line_rects：这帧上可见的行（帧像素坐标），用于高亮；line_ids 可直接传 record_lines 的 Future。
        ocr_rects：UIA 读不到字的区域（帧像素坐标），登记为待 OCR。
        """
        if self._closing or self._closed:
            raise RuntimeError("写线程已关闭")
        arr = np.ascontiguousarray(np.asarray(bgr)[..., :3], dtype=np.uint8).copy()
        if arr.ndim != 3:
            raise ValueError("bgr 必须是 HxWx3 数组")
        h, w = arr.shape[:2]
        frame_id = uuid.uuid4().hex
        rel = frame_rel_path(frame_id, int(captured_ms))
        self._submit(_frame_insert, frame_id, rel, w, h, int(captured_ms), segment_id, block_id, pos_y, hwnd,
                     app, title, int(bool(keep)), pixel_hash)
        if line_ids is not None:
            self._submit(_link_lines, frame_id, line_ids, list(line_rects or []))
        for rect in ocr_rects or ():
            self._submit(_ocr_add, frame_id, tuple(int(v) for v in rect), block_id, pos_y, self.clock())
        fut = self._encoder.submit(self._encode, frame_id, arr, rel)
        with self._enc_lock:
            self._encoding.add(fut)
        fut.add_done_callback(self._encoded)
        return frame_id

    def _encoded(self, fut: Future) -> None:
        with self._enc_lock:
            self._encoding.discard(fut)

    def sweep_frames(self, older_than_ms: int = 10 * 60_000) -> Future:
        """收拾上次崩溃留下的"编码中"证据帧：文件已写好的改成 ok，没有文件的改成 failed，并抹掉残留的 .tmp。

        录制进程启动时、保存任何新帧之前调用一次（只处理 captured_ms 早于 now - older_than_ms 的）。返回处理的帧数。
        """
        return self._submit(_sweep_frames, str(self.data_dir), self.clock(), self.clock() - int(older_than_ms))

    def _encode(self, frame_id: str, arr: np.ndarray, rel: str) -> None:
        path = self.data_dir / rel
        try:
            data = encode_webp(arr)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException as e:  # noqa: BLE001
            log.warning("证据帧 %s 编码/写入失败：%s", frame_id, e)
            self._submit(_frame_failed, frame_id)
            return
        self._submit(_frame_done, frame_id, len(data), self.clock(), str(path))

    def frame_seen(self, frame_id: str, t_ms: int) -> Future:
        """又看到与这张证据帧像素完全相同的画面：只更新帧和帧上各行（仍在的）的 last_seen，不再读字。"""
        return self._submit(_frame_seen, frame_id, int(t_ms))

    def link_lines(self, frame_id: str, line_ids: Sequence[int | None] | Future, rects: Sequence[Rect]) -> Future:
        return self._submit(_link_lines, frame_id, line_ids, list(rects))

    def frame_keep(self, frame_id: str, keep: bool = True) -> Future:
        return self._submit(lambda c: c.execute("UPDATE frames SET keep = ? WHERE frame_id = ?",
                                                (int(bool(keep)), frame_id)).rowcount)

    # ---------------- 待 OCR 区域 ----------------

    def ocr_region_add(self, frame_id: str, rect: Rect, *, block_id: int | Future | None = None,
                       pos_y: int | None = None) -> Future:
        return self._submit(_ocr_add, frame_id, tuple(int(v) for v in rect), block_id, pos_y, self.clock())

    def ocr_claim(self, limit: int = 4) -> Future:
        """领取待 OCR 区域（pending，或领取超时的 running）。返回 dict 列表，含图片绝对路径。"""
        return self._submit(_ocr_claim, int(limit), self.clock(), self.cfg.claim_stale_ms,
                            self.cfg.ocr_max_attempts, str(self.data_dir))

    def ocr_complete(self, region_id: int, *, ok: bool = True, error: str | None = None) -> Future:
        """OCR 结束（只改状态）。成功时一般直接用 ocr_result；失败且没到最大次数时回到 pending。"""
        return self._submit(_ocr_complete, int(region_id), bool(ok), error, self.clock(),
                            self.cfg.ocr_max_attempts)

    def ocr_result(self, region_id: int, lines: Sequence[LineIn]) -> Future:
        """登记一个区域的 OCR 结果，一步完成：record_lines（source='ocr'，时间 = 帧的 captured_ms，
        视口 = 该区域在块上的范围）+ 帧内位置 + 区域标记 done。

        lines 用帧像素坐标（不是裁剪图坐标，也不是块坐标），这里换算到块标尺。
        区域或帧已不存在（OCR 期间被隐私删除）时什么都不写，返回 None；否则返回与 lines 对齐的 line_id 列表。
        """
        lines = [LineIn(ln.text, ln.x0, ln.y0, ln.x1, ln.y1, "ocr", ln.confidence) for ln in lines]
        return self._submit(_ocr_result, int(region_id), lines, self.clock(), self.cfg)

    # ---------------- 划过、片段、概括 ----------------

    def skim(self, segment_id: int | Future, block_id: int | Future | None, start_ms: int, end_ms: int,
             y_from: int | None = None, y_to: int | None = None) -> Future:
        return self._submit(_skim, segment_id, block_id, int(start_ms), int(end_ms), y_from, y_to)

    def piece_put(self, start_ms: int, end_ms: int, *, app: str | None, title: str | None,
                  segment_ids: Sequence[int], task_label: str | None = None,
                  piece_id: int | None = None) -> Future:
        """新建或改写片段（piece_id 给出时改写），同时重写它关联的内容源。返回 piece_id。"""
        return self._submit(_piece_put, int(start_ms), int(end_ms), app, title, [int(s) for s in segment_ids],
                            task_label, piece_id, self.clock())

    def piece_label(self, piece_id: int, task_label: str | None) -> Future:
        return self._submit(lambda c: c.execute(
            "UPDATE pieces SET task_label = ?, updated_ms = ? WHERE piece_id = ?",
            (task_label, self.clock(), int(piece_id))).rowcount)

    def summary_put(self, level: str, key: str, text: str, *, refs: Any = (), model: str | None = None,
                    endpoint_host: str | None = None, start_ms: int | None = None,
                    end_ms: int | None = None) -> Future:
        if level not in SUMMARY_LEVELS:
            raise ValueError(f"未知概括层级：{level}")
        return self._submit(_summary_put, level, str(key), text, json.dumps(list(refs), ensure_ascii=False),
                            model, endpoint_host, start_ms, end_ms, self.clock())

    def summary_stale(self, level: str, key: str) -> Future:
        return self._submit(lambda c: c.execute(
            "UPDATE summaries SET status = 'stale' WHERE level = ? AND key = ?", (level, str(key))).rowcount)

    # ---------------- 待办队列 ----------------

    def job_add(self, kind: str, target: str = "", *, not_before_ms: int = 0) -> Future:
        """幂等登记：已存在且已结束（done/failed）的重新排队；pending/running 的不动。返回 job_id。"""
        return self._submit(_job_add, kind, str(target), int(not_before_ms), self.clock())

    def job_claim(self, kinds: Sequence[str] | None = None, limit: int = 1) -> Future:
        return self._submit(_job_claim, list(kinds) if kinds else None, int(limit), self.clock(),
                            self.cfg.claim_stale_ms)

    def job_done(self, job_id: int) -> Future:
        return self._submit(lambda c: c.execute(
            "UPDATE jobs SET status = 'done', last_error = NULL, updated_ms = ? WHERE job_id = ?",
            (self.clock(), int(job_id))).rowcount)

    def job_fail(self, job_id: int, error: str, *, retry_ms: int = 60_000, max_attempts: int = 5) -> Future:
        return self._submit(_job_fail, int(job_id), str(error)[:500], int(retry_ms), int(max_attempts),
                            self.clock())

    # ---------------- 控制 ----------------

    def control_set(self, **fields: Any) -> Future:
        """界面写期望状态与规则；request_seq 递增，录制进程据此知道有新请求。"""
        bad = set(fields) - set(_CONTROL_FIELDS)
        if bad:
            raise ValueError(f"不能由界面写的控制字段：{sorted(bad)}")
        if "desired_state" in fields and fields["desired_state"] not in ("recording", "paused"):
            raise ValueError("desired_state 只能是 recording / paused")
        return self._submit(_control_update, _control_values(fields), True, self.clock())

    def heartbeat(self, **fields: Any) -> Future:
        """录制进程写回状态与当前窗口（recorder_state / recorder_pid / recorder_note / current_*）。"""
        bad = set(fields) - set(_HEARTBEAT_FIELDS)
        if bad:
            raise ValueError(f"不能由录制进程写的控制字段：{sorted(bad)}")
        values = dict(fields)
        values["recorder_heartbeat_ms"] = self.clock()
        return self._submit(_control_update, values, False, self.clock(), quiet=True)


# ======================= 写线程里执行的操作（conn 已在事务内） =======================

def _purge_end(conn, t: int, app: str | None) -> int | None:
    """t 落在某次隐私删除的范围里（应用匹配或那次删除不限应用）→ 返回该范围的结束时间，否则 None。

    隐私删除由另一个进程执行时，本进程队列里晚到的写入（时间早于删除）会在删除之后提交，
    这里据此丢弃，删掉的内容不会"复活"。
    """
    row = conn.execute(
        "SELECT MAX(time_to) FROM purges WHERE time_from <= ? AND ? < time_to "
        "AND (app IS NULL OR sr_fold(app) = sr_fold(?))", (t, t, app)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _segment_app(conn, segment_id) -> str | None:
    if segment_id is None:
        return None
    row = conn.execute("SELECT app FROM segments WHERE segment_id = ?", (int(segment_id),)).fetchone()
    return None if row is None else row[0]


def _control_values(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in _CONTROL_JSON:
            v = json.dumps(v, ensure_ascii=False)
        elif k == "agent_enabled":
            v = int(bool(v))
        out[k] = v
    return out


def _control_update(conn, values: dict, bump_seq: bool, now: int) -> None:
    sets = [f"{k} = :{k}" for k in values]
    sets.append("updated_ms = :_now")
    if bump_seq:
        sets.append("request_seq = request_seq + 1")
    conn.execute(f"UPDATE control SET {', '.join(sets)} WHERE id = 1", {**values, "_now": now})


def _activity_start(conn, t, hwnd, pid, process, title, excluded) -> int:
    end = _purge_end(conn, t, process)
    if end is not None:  # 晚到的、落在已删范围里：从删除范围结束时算起（窗口那时仍在前台）
        t = max(t, end)
    conn.execute("UPDATE activity SET end_ms = max(start_ms, ?) WHERE end_ms IS NULL", (t,))
    cur = conn.execute(
        "INSERT INTO activity (start_ms, hwnd, pid, process, title, excluded) VALUES (?, ?, ?, ?, ?, ?)",
        (t, hwnd, pid, process, title, excluded),
    )
    return int(cur.lastrowid)


def _activity_end(conn, t) -> int:
    return conn.execute("UPDATE activity SET end_ms = max(start_ms, ?) WHERE end_ms IS NULL", (t,)).rowcount


def _gap_insert(conn, start, end, reason, detail, hwnd, process) -> int:
    cur = conn.execute(
        "INSERT INTO gaps (start_ms, end_ms, reason, detail, hwnd, process) VALUES (?, ?, ?, ?, ?, ?)",
        (start, end, reason, detail, hwnd, process),
    )
    return int(cur.lastrowid)


def _gap_close(conn, gap_id, t) -> int:
    return conn.execute("UPDATE gaps SET end_ms = max(start_ms, ?) WHERE gap_id = ? AND end_ms IS NULL",
                        (t, int(_value(gap_id)))).rowcount


def _segment_for(conn, app, title, t, reuse_ms) -> int:
    row = conn.execute(
        "SELECT segment_id FROM segments WHERE app = ? AND title = ? AND last_seen_ms >= ? "
        "ORDER BY last_seen_ms DESC LIMIT 1",
        (app, title, t - reuse_ms),
    ).fetchone()
    if row is not None:
        conn.execute("UPDATE segments SET last_seen_ms = max(last_seen_ms, ?), first_seen_ms = min(first_seen_ms, ?) "
                     "WHERE segment_id = ?", (t, t, row[0]))
        return int(row[0])
    cur = conn.execute("INSERT INTO segments (app, title, first_seen_ms, last_seen_ms, app_fold, title_fold) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (app, title, t, t, docs.fold(app), docs.fold(title)))
    return int(cur.lastrowid)


def _segment_volatile(conn, segment_id, mask_json) -> int:
    return conn.execute("UPDATE segments SET volatile_mask = ? WHERE segment_id = ?",
                        (mask_json, int(_value(segment_id)))).rowcount


def _block_new(conn, segment_id, t, anchor) -> int:
    cur = conn.execute("INSERT INTO blocks (segment_id, created_ms, last_seen_ms, anchor) VALUES (?, ?, ?, ?)",
                       (int(_value(segment_id)), t, t, anchor))
    return int(cur.lastrowid)


def _block_anchor(conn, block_id, anchor) -> int:
    return conn.execute("UPDATE blocks SET anchor = ? WHERE block_id = ?", (anchor, int(_value(block_id)))).rowcount


def block_root(conn, block_id: int) -> tuple[int, int]:
    """沿 merged_into 找到最终的块，返回 (根块, 本块坐标到根块坐标的累计偏移)。"""
    offset = 0
    seen = set()
    cur = int(block_id)
    while True:
        if cur in seen:
            raise ValueError(f"块合并出现环：{block_id}")
        seen.add(cur)
        row = conn.execute("SELECT merged_into, offset_px FROM blocks WHERE block_id = ?", (cur,)).fetchone()
        if row is None:
            raise ValueError(f"块 {cur} 不存在")
        if row[0] is None:
            return cur, offset
        offset += int(row[1])
        cur = int(row[0])


def _block_merge(conn, src, dst, offset) -> None:
    src, dst = int(_value(src)), int(_value(dst))
    if src == dst:
        raise ValueError("不能把块并入自己")
    root, _ = block_root(conn, dst)
    if root == src:
        raise ValueError("合并会形成环")
    block_root(conn, src)  # 确认存在
    conn.execute("UPDATE blocks SET merged_into = ?, offset_px = ? WHERE block_id = ?", (dst, offset, src))


def _order_key(y0: float, x0: float) -> float:
    return float(y0) + max(0.0, min(float(x0), 99_999.0)) * 1e-6


def _record_lines(conn, block_id, t, top, bottom, lines: list[LineIn], cfg: WriterConfig) -> list[int | None]:
    block_id = int(_value(block_id))
    brow = conn.execute("SELECT segment_id FROM blocks WHERE block_id = ?", (block_id,)).fetchone()
    if brow is None:
        raise BlockGone(f"块 {block_id} 不存在")
    segment_id = int(brow[0])
    if _purge_end(conn, t, _segment_app(conn, segment_id)) is not None:
        return [None] * len(lines)  # 这次"看清"落在已删除的范围里：整个丢弃
    dy = cfg.match_dy_px

    incoming = [(i, ln, docs.norm(ln.text)) for i, ln in enumerate(lines)]
    incoming = [x for x in incoming if x[2]]
    ys = [ln.y0 for _, ln, _ in incoming]
    lo = min([top] + ys) - dy - 1
    hi = max([bottom] + ys) + dy + 1
    live = conn.execute(
        "SELECT line_id, norm, y0, y1, last_seen_ms FROM lines "
        "WHERE block_id = ? AND disappeared_ms IS NULL AND order_key BETWEEN ? AND ?",
        (block_id, lo, hi),
    ).fetchall()
    by_norm: dict[str, list] = {}
    for r in live:
        by_norm.setdefault(r[1], []).append(r)

    ids: list[int | None] = [None] * len(lines)
    matched: set[int] = set()
    unmatched: list[tuple[int, LineIn, str]] = []
    for i, ln, n in sorted(incoming, key=lambda x: (x[1].y0, x[1].x0)):
        best = None
        for r in by_norm.get(n, ()):
            d = abs(r[2] - ln.y0)
            if r[0] not in matched and d <= dy and (best is None or d < abs(best[2] - ln.y0)):
                best = r
        if best is None:
            unmatched.append((i, ln, n))
        else:
            matched.add(best[0])
            ids[i] = int(best[0])
    if matched:
        conn.executemany(
            "UPDATE lines SET last_seen_ms = max(last_seen_ms, ?), first_seen_ms = min(first_seen_ms, ?) "
            "WHERE line_id = ?",
            [(t, t, lid) for lid in matched],
        )

    gone = [r for r in live if r[0] not in matched and r[2] >= top and r[3] <= bottom and r[4] <= t]
    if gone:
        conn.executemany("UPDATE lines SET disappeared_ms = ? WHERE line_id = ?", [(t, r[0]) for r in gone])

    new_rows: list[tuple[int, str, str, float]] = []  # (line_id, text, norm, order_key)
    for i, ln, n in unmatched:
        replaces = None
        best_ov = 0.0
        h_new = max(1e-6, ln.y1 - ln.y0)
        for r in gone:
            ov = min(ln.y1, r[3]) - max(ln.y0, r[2])
            ratio = ov / max(1e-6, min(h_new, r[3] - r[2]))
            if ov > 0 and ratio >= cfg.replace_overlap and ratio > best_ov:
                replaces, best_ov = int(r[0]), ratio
        ok = _order_key(ln.y0, ln.x0)
        cur = conn.execute(
            "INSERT INTO lines (block_id, segment_id, x0, y0, x1, y1, order_key, text, norm, source, confidence, "
            "first_seen_ms, last_seen_ms, replaces_line_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (block_id, segment_id, ln.x0, ln.y0, ln.x1, ln.y1, ok, ln.text, n, ln.source, float(ln.confidence),
             t, t, replaces),
        )
        lid = int(cur.lastrowid)
        ids[i] = lid
        new_rows.append((lid, ln.text, n, ok))

    if new_rows:
        _link_tails(conn, block_id, new_rows)
    conn.execute("UPDATE blocks SET last_seen_ms = max(last_seen_ms, ?) WHERE block_id = ?", (t, block_id))
    conn.execute("UPDATE segments SET last_seen_ms = max(last_seen_ms, ?) WHERE segment_id = ?", (t, segment_id))
    return ids


def _neighbour(conn, block_id: int, key: float, line_id: int, after: bool):
    # 写成 order_key 的范围条件（而不是 OR），才能走 idx_lines_live 的范围扫描；块里行多时差几十倍
    if after:
        sql = ("SELECT line_id, norm FROM lines WHERE block_id = ? AND disappeared_ms IS NULL "
               "AND order_key >= ? AND NOT (order_key = ? AND line_id <= ?) ORDER BY order_key, line_id LIMIT 1")
    else:
        sql = ("SELECT line_id, norm FROM lines WHERE block_id = ? AND disappeared_ms IS NULL "
               "AND order_key <= ? AND NOT (order_key = ? AND line_id >= ?) "
               "ORDER BY order_key DESC, line_id DESC LIMIT 1")
    return conn.execute(sql, (block_id, key, key, line_id)).fetchone()


def _link_tails(conn, block_id: int, new_rows: list[tuple[int, str, str, float]]) -> None:
    """新行的 tail 取下一行；上一行（若不是新行）的 tail 改成指向新行。最后写全文索引。"""
    new_ids = {r[0] for r in new_rows}
    index: list[docs.Row] = []
    changes: dict[int, tuple[int, str, int]] = {}
    for lid, text, n, key in new_rows:
        nxt = _neighbour(conn, block_id, key, lid, after=True)
        tail, tail_line = (docs.tail_of(nxt[1]), int(nxt[0])) if nxt else ("", None)
        if tail or tail_line is not None:
            conn.execute("UPDATE lines SET tail = ?, tail_line_id = ? WHERE line_id = ?", (tail, tail_line, lid))
        index.append((lid, text, n, tail))
        prv = _neighbour(conn, block_id, key, lid, after=False)
        if prv is not None and int(prv[0]) not in new_ids:
            changes[int(prv[0])] = (int(prv[0]), docs.tail_of(n), lid)
    docs.index_rows(conn, index)
    docs.set_tails(conn, list(changes.values()))


def _frame_insert(conn, frame_id, rel, w, h, captured, segment_id, block_id, pos_y, hwnd, app, title, keep,
                  pixel_hash) -> str:
    if _purge_end(conn, captured, app or _segment_app(conn, _opt_int(segment_id))) is not None:
        return frame_id  # 落在已删除的范围里：不登记；编码完成时发现没有这一行，会把文件抹掉
    frame_no = int(conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'frame_seq' RETURNING value"
    ).fetchone()[0])
    conn.execute(
        "INSERT INTO frames (frame_id, frame_no, image_path, width, height, captured_ms, last_seen_ms, segment_id, "
        "block_id, pos_y, hwnd, app, title, keep, pixel_hash, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'encoding')",
        (frame_id, frame_no, rel, w, h, captured, captured, _opt_int(segment_id), _opt_int(block_id), pos_y, hwnd,
         app, title, keep, pixel_hash),
    )
    return frame_id


def _opt_int(x) -> int | None:
    x = _value(x)
    return None if x is None else int(x)


def _frame_done(conn, frame_id, nbytes, now, abs_path) -> None:
    n = conn.execute("UPDATE frames SET status = 'ok', bytes = ?, saved_ms = ? WHERE frame_id = ? "
                     "AND status = 'encoding'", (nbytes, now, frame_id)).rowcount
    if n == 0:
        # 编码期间这帧已被删除（隐私删除与编码赛跑）：文件也不能留
        from .delete import wipe_file

        wipe_file(Path(abs_path))


def _frame_seen(conn, frame_id, t) -> int:
    row = conn.execute("SELECT segment_id, block_id, app FROM frames WHERE frame_id = ?", (frame_id,)).fetchone()
    if row is None or _purge_end(conn, t, row[2] or _segment_app(conn, row[0])) is not None:
        return 0
    conn.execute("UPDATE frames SET last_seen_ms = max(last_seen_ms, ?) WHERE frame_id = ?", (t, frame_id))
    n = conn.execute(
        "UPDATE lines SET last_seen_ms = max(last_seen_ms, ?) WHERE disappeared_ms IS NULL AND line_id IN "
        "(SELECT line_id FROM line_in_frame WHERE frame_id = ?)", (t, frame_id)).rowcount
    if row[1] is not None:
        conn.execute("UPDATE blocks SET last_seen_ms = max(last_seen_ms, ?) WHERE block_id = ?", (t, row[1]))
    if row[0] is not None:
        conn.execute("UPDATE segments SET last_seen_ms = max(last_seen_ms, ?) WHERE segment_id = ?", (t, row[0]))
    return n


def _frame_failed(conn, frame_id) -> None:
    conn.execute("UPDATE frames SET status = 'failed' WHERE frame_id = ? AND status = 'encoding'", (frame_id,))


def _frame_exists(conn, frame_id) -> bool:
    return conn.execute("SELECT 1 FROM frames WHERE frame_id = ?", (frame_id,)).fetchone() is not None


def _link_lines(conn, frame_id, line_ids, rects) -> int:
    ids = _value(line_ids)
    if not _frame_exists(conn, frame_id):  # 帧落在已删范围里没登记，或已被删除
        return 0
    rows = []
    for lid, rect in zip(ids, rects):
        if lid is None:
            continue
        rows.append((frame_id, int(lid), *[float(v) for v in rect]))
    conn.executemany(
        "INSERT OR REPLACE INTO line_in_frame (frame_id, line_id, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def _ocr_add(conn, frame_id, rect, block_id, pos_y, now) -> int | None:
    if not _frame_exists(conn, frame_id):
        return None
    cur = conn.execute(
        "INSERT INTO ocr_regions (frame_id, x0, y0, x1, y1, block_id, pos_y, created_ms, updated_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (frame_id, *rect, _opt_int(block_id), pos_y, now, now),
    )
    return int(cur.lastrowid)


def _ocr_claim(conn, limit, now, stale_ms, max_attempts, data_dir) -> list[dict]:
    rows = conn.execute(
        "SELECT r.region_id, r.frame_id, r.x0, r.y0, r.x1, r.y1, r.block_id, r.pos_y, r.attempts, "
        "f.image_path, f.captured_ms, f.segment_id, f.app, f.title, f.width, f.height "
        "FROM ocr_regions r JOIN frames f ON f.frame_id = r.frame_id "
        "WHERE f.status = 'ok' AND r.attempts < ? AND (r.status = 'pending' OR (r.status = 'running' AND r.claimed_ms < ?)) "
        "ORDER BY r.region_id LIMIT ?",
        (max_attempts, now - stale_ms, limit),
    ).fetchall()
    out = []
    for r in rows:
        conn.execute("UPDATE ocr_regions SET status = 'running', attempts = attempts + 1, claimed_ms = ?, "
                     "updated_ms = ? WHERE region_id = ?", (now, now, r["region_id"]))
        d = dict(r)
        d["rect"] = (r["x0"], r["y0"], r["x1"], r["y1"])
        d["image_abs_path"] = str(Path(data_dir) / r["image_path"])
        d["attempts"] = r["attempts"] + 1
        out.append(d)
    return out


def _ocr_complete(conn, region_id, ok, error, now, max_attempts) -> None:
    if ok:
        conn.execute("UPDATE ocr_regions SET status = 'done', last_error = NULL, updated_ms = ? WHERE region_id = ?",
                     (now, region_id))
        return
    conn.execute(
        "UPDATE ocr_regions SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END, "
        "last_error = ?, updated_ms = ? WHERE region_id = ?",
        (max_attempts, (error or "")[:500], now, region_id),
    )


def _ocr_result(conn, region_id, lines: list[LineIn], now, cfg: WriterConfig) -> list[int | None] | None:
    r = conn.execute(
        "SELECT r.frame_id, r.y0, r.y1, COALESCE(r.block_id, f.block_id) AS block_id, "
        "COALESCE(r.pos_y, f.pos_y) AS pos_y, f.captured_ms "
        "FROM ocr_regions r JOIN frames f ON f.frame_id = r.frame_id WHERE r.region_id = ?", (region_id,)
    ).fetchone()
    if r is None:
        return None  # 区域或帧已被删除：识别结果丢弃，删掉的内容不能借 OCR 回来
    if r["block_id"] is None or r["pos_y"] is None:
        conn.execute("UPDATE ocr_regions SET status = 'failed', last_error = ?, updated_ms = ? WHERE region_id = ?",
                     ("区域没有块坐标，文字无法登记", now, region_id))
        return None
    pos = float(r["pos_y"])
    block_lines = [LineIn(ln.text, ln.x0, ln.y0 + pos, ln.x1, ln.y1 + pos, ln.source, ln.confidence) for ln in lines]
    ids = _record_lines(conn, r["block_id"], int(r["captured_ms"]), pos + r["y0"], pos + r["y1"], block_lines, cfg)
    _link_lines(conn, r["frame_id"], ids, [(ln.x0, ln.y0, ln.x1, ln.y1) for ln in lines])
    conn.execute("UPDATE ocr_regions SET status = 'done', last_error = NULL, updated_ms = ? WHERE region_id = ?",
                 (now, region_id))
    return ids


def _sweep_frames(conn, data_dir, now, cutoff) -> int:
    from .delete import wipe_file

    rows = conn.execute("SELECT frame_id, image_path FROM frames WHERE status = 'encoding' AND captured_ms < ?",
                        (cutoff,)).fetchall()
    for fid, rel in rows:
        path = Path(data_dir) / rel
        try:
            wipe_file(path.with_suffix(".tmp"))  # 写到一半的临时文件也是屏幕内容
        except OSError as e:
            log.warning("残留临时文件删除失败 %s：%s", rel, e)
        if path.is_file():  # 先写临时文件再改名：正式文件存在就是完整的
            conn.execute("UPDATE frames SET status = 'ok', bytes = ?, saved_ms = ? WHERE frame_id = ?",
                         (path.stat().st_size, now, fid))
        else:
            conn.execute("UPDATE frames SET status = 'failed' WHERE frame_id = ?", (fid,))
    return len(rows)


def _skim(conn, segment_id, block_id, start, end, y_from, y_to) -> int | None:
    segment_id = int(_value(segment_id))
    if _purge_end(conn, start, _segment_app(conn, segment_id)) is not None:
        return None
    cur = conn.execute(
        "INSERT INTO skims (segment_id, block_id, start_ms, end_ms, y_from, y_to) VALUES (?, ?, ?, ?, ?, ?)",
        (segment_id, _opt_int(block_id), start, max(start, end), y_from, y_to),
    )
    return int(cur.lastrowid)


def _piece_put(conn, start, end, app, title, segment_ids, task_label, piece_id, now) -> int:
    if piece_id is None:
        cur = conn.execute(
            "INSERT INTO pieces (start_ms, end_ms, app, title, task_label, created_ms, updated_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (start, max(start, end), app, title, task_label, now, now))
        piece_id = int(cur.lastrowid)
    else:
        n = conn.execute("UPDATE pieces SET start_ms = ?, end_ms = ?, app = ?, title = ?, task_label = ?, "
                         "updated_ms = ? WHERE piece_id = ?",
                         (start, max(start, end), app, title, task_label, now, piece_id)).rowcount
        if n == 0:
            raise ValueError(f"片段 {piece_id} 不存在")
        conn.execute("DELETE FROM piece_segments WHERE piece_id = ?", (piece_id,))
    conn.executemany("INSERT OR IGNORE INTO piece_segments (piece_id, segment_id) VALUES (?, ?)",
                     [(piece_id, s) for s in segment_ids])
    return int(piece_id)


def _summary_put(conn, level, key, text, refs, model, host, start, end, now) -> int:
    cur = conn.execute(
        "INSERT INTO summaries (level, key, start_ms, end_ms, text, refs, status, model, endpoint_host, created_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?) ON CONFLICT (level, key) DO UPDATE SET "
        "start_ms = excluded.start_ms, end_ms = excluded.end_ms, text = excluded.text, refs = excluded.refs, "
        "status = 'ok', model = excluded.model, endpoint_host = excluded.endpoint_host, "
        "created_ms = excluded.created_ms RETURNING summary_id",
        (level, key, start, end, text, refs, model, host, now),
    )
    return int(cur.fetchone()[0])


def _job_add(conn, kind, target, not_before, now) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (kind, target, not_before_ms, created_ms, updated_ms) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (kind, target) DO UPDATE SET status = 'pending', attempts = 0, last_error = NULL, "
        "not_before_ms = excluded.not_before_ms, updated_ms = excluded.updated_ms "
        "WHERE jobs.status IN ('done', 'failed') RETURNING job_id",
        (kind, target, not_before, now, now),
    )
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    return int(conn.execute("SELECT job_id FROM jobs WHERE kind = ? AND target = ?", (kind, target)).fetchone()[0])


def _job_claim(conn, kinds, limit, now, stale_ms) -> list[dict]:
    params: list = [now, now - stale_ms]
    kind_sql = ""
    if kinds:
        kind_sql = f" AND kind IN ({', '.join('?' * len(kinds))})"
        params.extend(kinds)
    params.append(limit)
    rows = conn.execute(
        "SELECT job_id, kind, target, attempts FROM jobs WHERE ((status = 'pending' AND not_before_ms <= ?) "
        f"OR (status = 'running' AND updated_ms < ?)){kind_sql} ORDER BY job_id LIMIT ?",
        params,
    ).fetchall()
    out = []
    for r in rows:
        conn.execute("UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_ms = ? WHERE job_id = ?",
                     (now, r["job_id"]))
        out.append({"job_id": r["job_id"], "kind": r["kind"], "target": r["target"], "attempts": r["attempts"] + 1})
    return out


def _job_fail(conn, job_id, error, retry_ms, max_attempts, now) -> None:
    conn.execute(
        "UPDATE jobs SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END, "
        "not_before_ms = ?, last_error = ?, updated_ms = ? WHERE job_id = ?",
        (max_attempts, now + retry_ms, error, now, job_id),
    )
