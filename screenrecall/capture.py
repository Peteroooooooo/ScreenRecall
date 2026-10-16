"""采集：按固定间隔采样指定窗口，精确重复跳过或复用，接受新画面并登记 OCR 待办。

P0 范围（方案 1.1、4.1、4.2）：
- 普通模式每 interval 秒检查一次；任何像素变化都保存为新内容（P0 尚无等稳与排错模式）；
- 只有原始像素完全一致、且捕获上下文（窗口、进程、尺寸）一致才复用已保存内容；
- 连续观察到同一内容只延长当前出现记录；A→B→A 复用 frame_A 并新增一次出现；
- 锁屏、最小化、捕获失败记为缺口并中断出现连续性；目标窗口关闭则自动暂停。

接受点（方案 4.2）：图片可靠落盘，且内容、出现与 OCR 待办在同一 SQLite 事务提交。
控制切换与接受提交用同一把锁串行化：暂停 ACK 只在在途提交完成后写出，
在途采样在提交前核对 epoch，控制已切换则丢弃（方案 4.1）。
所有内存状态只在事务提交成功后更新，避免与数据库不一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from . import control, db, storage, winapi
from .config import Config
from .timeutil import local_tz, utc_ms

GAP_TEXT = {
    "locked": "屏幕已锁定，暂不采集",
    "minimized": "目标窗口已最小化，暂不采集",
    "hidden": "目标窗口已隐藏，暂不采集",
    "blank_capture": "取不到窗口内容（可能是硬件加速窗口且不在前台）",
    "empty_rect": "窗口尺寸为空",
    "rect_failed": "无法读取窗口位置",
    "gdi_failed": "截图调用失败",
    "capture_error": "截图出错",
}


@dataclass(frozen=True)
class _Target:
    hwnd: int
    process: str
    title: str


@dataclass(frozen=True)
class _Current:
    """当前出现记录及其内容，用于判断"连续观察到同一画面"。"""

    occurrence_id: str
    frame_id: str
    occ_ctx: tuple
    digest: str
    bgr: bytes


@dataclass(frozen=True)
class _Recent:
    frame_id: str
    bgr: bytes


class Recorder:
    def __init__(self, cfg: Config, wake_ocr: threading.Event, stop: threading.Event, log: logging.Logger):
        self.cfg = cfg
        self.wake_ocr = wake_ocr
        self.stop = stop
        self.log = log
        self.lock = threading.Lock()
        self.tick = threading.Event()  # 控制切换时立即唤醒采样
        self.epoch = 0
        self.recording = False
        self.target: _Target | None = None
        self.session_id: str | None = None
        self.gap_id: int | None = None
        self.gap_reason: str | None = None
        self.current: _Current | None = None
        self.recent: OrderedDict[tuple, _Recent] = OrderedDict()

    # ================= 控制线程调用 =================

    def apply_request(self, conn: sqlite3.Connection, ctrl: sqlite3.Row) -> None:
        seq = int(ctrl["request_seq"])
        desired = ctrl["desired_state"]
        with self.lock:
            now = utc_ms()
            end_reason: str | None = None
            start_info: winapi.WindowInfo | None = None
            if desired == "recording":
                info = self._resolve_target(ctrl)
                if info is None:
                    end_reason = "target_invalid" if self.recording else None
                    actual, reason = "paused", "目标窗口不存在或已变化，请重新选择"
                elif self.recording and self.target and self.target.hwnd == info.hwnd:
                    actual, reason = "recording", None  # 已在录制同一窗口
                else:
                    end_reason = "switch_target" if self.recording else None
                    start_info = info
                    actual, reason = "recording", None
            else:
                end_reason = "paused" if self.recording else None
                actual, reason = "paused", "已按请求暂停"

            new_session = db.new_id() if start_info else None
            with db.write_txn(conn):
                if end_reason:
                    self._sql_end_session(conn, now, end_reason)
                if start_info:
                    self._sql_start_session(conn, new_session, now, start_info)
                session_for_ack = new_session if start_info else (self.session_id if actual == "recording" else None)
                control.write_ack(conn, seq, actual, reason, session_for_ack)
            # 已提交，再更新内存
            if end_reason or start_info:
                self.epoch += 1
            if end_reason:
                self._mem_end_session()
            if start_info:
                self._mem_start_session(new_session, start_info)
        self.log.info("control seq=%d desired=%s -> %s%s", seq, desired, actual, f" ({reason})" if reason else "")
        self.tick.set()

    def shutdown(self, conn: sqlite3.Connection) -> None:
        with self.lock:
            if self.recording:
                with db.write_txn(conn):
                    self._sql_end_session(conn, utc_ms(), "daemon_stop")
                self._mem_end_session()
            self.epoch += 1
        self.tick.set()

    def _resolve_target(self, ctrl: sqlite3.Row) -> winapi.WindowInfo | None:
        info = winapi.describe_window(ctrl["target_hwnd"])
        if info is None:
            return None
        expected = ctrl["target_process"]
        if expected and info.process.lower() != expected.lower():
            return None  # 句柄已被其他程序复用
        return info

    def _sql_start_session(self, conn, session_id: str, now: int, info: winapi.WindowInfo) -> None:
        offset, name = local_tz()
        conn.execute(
            "INSERT INTO sessions (session_id, started_at_ms, target_hwnd, target_process, target_title, "
            "tz_offset_min, tz_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, now, info.hwnd, info.process, info.title, offset, name),
        )

    def _sql_end_session(self, conn, now: int, reason: str) -> None:
        if self.gap_id is not None:
            conn.execute("UPDATE gaps SET ended_at_ms = ? WHERE gap_id = ? AND ended_at_ms IS NULL", (now, self.gap_id))
        if self.session_id:
            conn.execute(
                "UPDATE sessions SET ended_at_ms = ?, end_reason = ? WHERE session_id = ? AND ended_at_ms IS NULL",
                (now, reason, self.session_id),
            )

    def _mem_start_session(self, session_id: str, info: winapi.WindowInfo) -> None:
        self.recording = True
        self.session_id = session_id
        self.target = _Target(info.hwnd, info.process, info.title)
        self.current = None
        self.gap_id = None
        self.gap_reason = None

    def _mem_end_session(self) -> None:
        # 近期缓存保留：恢复录制后若画面像素完全一致，仍可复用已保存内容
        self.recording = False
        self.session_id = None
        self.target = None
        self.current = None
        self.gap_id = None
        self.gap_reason = None

    # ================= 采集线程 =================

    def run(self) -> None:
        conn = db.connect(self.cfg.db_path)
        interval = self.cfg.capture.interval_seconds
        next_due = time.monotonic()
        try:
            while not self.stop.is_set():
                woke = self.tick.wait(max(0.0, next_due - time.monotonic()))
                if self.stop.is_set():
                    break
                now = time.monotonic()
                if woke:
                    self.tick.clear()
                    next_due = now + interval  # 开启、恢复或切换目标时直接捕获
                elif now < next_due:
                    continue
                else:
                    next_due += interval
                    if next_due <= now:
                        next_due = now + interval
                try:
                    self._sample(conn)
                except Exception:
                    self.log.exception("采样出错")
        finally:
            conn.close()

    def _sample(self, conn: sqlite3.Connection) -> None:
        with self.lock:
            if not self.recording or self.target is None:
                return
            epoch, target = self.epoch, self.target
        observed_ms = utc_ms()

        if winapi.is_input_desktop_locked():
            self._note_gap(conn, epoch, observed_ms, "locked")
            return
        info = winapi.describe_window(target.hwnd)
        if info is None or info.process.lower() != target.process.lower():
            self._auto_pause(conn, epoch, "目标窗口已关闭，录制已自动暂停")
            return
        try:
            cap = winapi.capture_window(target.hwnd)
        except winapi.CaptureError as exc:
            if exc.reason == "window_gone":
                self._auto_pause(conn, epoch, "目标窗口已关闭，录制已自动暂停")
            else:
                self._note_gap(conn, epoch, observed_ms, exc.reason, exc.detail)
            return
        except OSError as exc:
            self._note_gap(conn, epoch, observed_ms, "capture_error", str(exc))
            return

        digest = hashlib.blake2b(cap.bgr, digest_size=16).hexdigest()
        reuse_ctx = (info.hwnd, info.process.lower(), cap.width, cap.height)
        occ_ctx = reuse_ctx + (info.title,)

        cur = self.current
        if cur and cur.occ_ctx == occ_ctx and cur.digest == digest and cur.bgr == cap.bgr:
            self._commit_extend(conn, epoch, cur, observed_ms)
            return
        key = (reuse_ctx, digest)
        hit = self.recent.get(key)
        if hit and hit.bgr == cap.bgr:
            self._commit_reuse(conn, epoch, key, hit, info, occ_ctx, digest, observed_ms)
            return

        t0 = time.perf_counter()
        capcfg = self.cfg.capture
        payload = storage.encode_webp(
            cap.bgr, cap.width, cap.height,
            quality=capcfg.webp_quality, lossless=capcfg.webp_lossless, method=capcfg.webp_method,
        )
        encode_ms = int((time.perf_counter() - t0) * 1000)
        self._commit_new(conn, epoch, key, info, occ_ctx, digest, cap, payload, observed_ms, encode_ms)

    def _insert_occurrence(self, conn, occ_id: str, frame_id: str, info: winapi.WindowInfo, observed_ms: int) -> None:
        conn.execute(
            "INSERT INTO occurrences (occurrence_id, frame_id, session_id, first_seen_ms, last_seen_ms, "
            "app, window_title, hwnd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (occ_id, frame_id, self.session_id, observed_ms, observed_ms, info.process, info.title, info.hwnd),
        )

    def _sql_close_gap(self, conn, ms: int) -> None:
        if self.gap_id is not None:
            conn.execute("UPDATE gaps SET ended_at_ms = ? WHERE gap_id = ? AND ended_at_ms IS NULL", (ms, self.gap_id))

    def _mem_gap_closed(self) -> None:
        if self.gap_id is not None:
            self.log.info("gap end reason=%s", self.gap_reason)
        self.gap_id = None
        self.gap_reason = None

    def _commit_extend(self, conn, epoch: int, cur: _Current, observed_ms: int) -> None:
        with self.lock:
            if epoch != self.epoch or self.current is not cur:
                return
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE occurrences SET last_seen_ms = MAX(last_seen_ms, ?), observations = observations + 1 "
                    "WHERE occurrence_id = ?",
                    (observed_ms, cur.occurrence_id),
                )
                self._sql_close_gap(conn, observed_ms)
                control.set_capture_note(conn, None, observed_ms)
            self._mem_gap_closed()

    def _commit_reuse(self, conn, epoch, key, hit: _Recent, info, occ_ctx, digest, observed_ms) -> None:
        occ_id = db.new_id()
        with self.lock:
            if epoch != self.epoch:
                return
            with db.write_txn(conn):
                still_ok = conn.execute(
                    "SELECT 1 FROM frames WHERE frame_id = ? AND image_status = 'ok'", (hit.frame_id,)
                ).fetchone()
                if still_ok:
                    self._insert_occurrence(conn, occ_id, hit.frame_id, info, observed_ms)
                    self._sql_close_gap(conn, observed_ms)
                    control.set_capture_note(conn, None, observed_ms)
            if not still_ok:
                self.recent.pop(key, None)  # 内容已失效，下一次采样按新内容保存
                return
            self._mem_gap_closed()
            self.current = _Current(occ_id, hit.frame_id, occ_ctx, digest, hit.bgr)
            self.recent.move_to_end(key)
        self.log.info("reuse frame=%s new occurrence=%s", hit.frame_id, occ_id)

    def _commit_new(self, conn, epoch, key, info, occ_ctx, digest, cap: winapi.Capture, payload, observed_ms, encode_ms) -> None:
        frame_id, occ_id = db.new_id(), db.new_id()
        rel = storage.frame_rel_path(frame_id, observed_ms)
        meta = json.dumps({"rect": cap.rect, "dpi": cap.dpi, "method": cap.method}, ensure_ascii=False)
        capcfg = self.cfg.capture
        fmt = "webp-lossless" if capcfg.webp_lossless else f"webp-q{capcfg.webp_quality}"
        with self.lock:
            if epoch != self.epoch:
                self.log.info("控制已切换，丢弃在途画面")
                return
            nbytes = storage.write_atomic(self.cfg.data_dir, rel, payload, (cap.width, cap.height))
            try:
                with db.write_txn(conn):
                    now = utc_ms()
                    conn.execute(
                        "INSERT INTO frames (frame_id, image_path, width, height, captured_at_ms, accepted_at_ms, "
                        "pixel_hash, image_format, image_bytes, capture_meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (frame_id, rel, cap.width, cap.height, observed_ms, now, digest, fmt, nbytes, meta),
                    )
                    self._insert_occurrence(conn, occ_id, frame_id, info, observed_ms)
                    conn.execute(
                        "INSERT INTO index_jobs (frame_id, stage, status, target_version, created_at_ms, updated_at_ms) "
                        "VALUES (?, 'ocr', 'pending', 1, ?, ?)",
                        (frame_id, now, now),
                    )
                    self._sql_close_gap(conn, observed_ms)
                    control.set_capture_note(conn, None, observed_ms)
            except BaseException:
                storage.discard(self.cfg.data_dir, rel)  # 未登记的文件不算接受
                raise
            self._mem_gap_closed()
            self.current = _Current(occ_id, frame_id, occ_ctx, digest, cap.bgr)
            self.recent[key] = _Recent(frame_id, cap.bgr)
            self.recent.move_to_end(key)
            while len(self.recent) > capcfg.recent_cache_size:
                self.recent.popitem(last=False)
        self.wake_ocr.set()
        self.log.info(
            "accepted frame=%s %dx%d bytes=%d encode_ms=%d method=%s dpi=%s",
            frame_id, cap.width, cap.height, nbytes, encode_ms, cap.method, cap.dpi,
        )

    def _note_gap(self, conn, epoch: int, observed_ms: int, reason: str, detail: str = "") -> None:
        with self.lock:
            if epoch != self.epoch:
                return
            self.current = None  # 缺口中断出现连续性
            if self.gap_reason == reason:
                return
            with db.write_txn(conn):
                self._sql_close_gap(conn, observed_ms)
                cur = conn.execute(
                    "INSERT INTO gaps (session_id, started_at_ms, reason, detail) VALUES (?, ?, ?, ?)",
                    (self.session_id, observed_ms, reason, detail or None),
                )
                control.set_capture_note(conn, GAP_TEXT.get(reason, reason), None)
            self._mem_gap_closed()
            self.gap_id, self.gap_reason = cur.lastrowid, reason
        self.log.info("gap start reason=%s %s", reason, detail)

    def _auto_pause(self, conn, epoch: int, reason: str) -> None:
        with self.lock:
            if epoch != self.epoch:
                return
            with db.write_txn(conn):
                self._sql_end_session(conn, utc_ms(), "window_gone")
                control.set_state(conn, "paused", reason)
            self.epoch += 1
            self._mem_end_session()
        self.log.warning(reason)
