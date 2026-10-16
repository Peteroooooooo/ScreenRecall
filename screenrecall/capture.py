"""采集：按记录节奏采样指定窗口，精确重复跳过或复用，接受新画面并登记 OCR 待办。

范围（方案 1.1、4.1、4.2）：
- 普通模式每 interval 秒检查一次；变化后等稳（相邻观察变化比例 < 阈值）再保存，
  持续变化从等待起点累计 stable_timeout 秒后在首个到期采样点保存（节奏规则见 cadence.py）；
- 排错模式每 1 秒检查，不等稳，自动保存至少间隔 2 秒，限频期间只保留最新候选；
  满 30 分钟或 OCR 待办过高时自动退回普通模式；
- 快捷键/页面补拍：立即捕获并新增一次出现（source_type='manual'），不受自动间隔限制，
  仍受暂停、锁屏和排除规则限制；
- 只有原始像素完全一致、且捕获上下文（窗口、进程、尺寸）一致才复用已保存内容；
- 连续观察到同一内容只延长当前出现记录；A→B→A 复用 frame_A 并新增一次出现；
- 观察到变化但未保存（等稳或限频）时记一条 unsaved_change 中断标记，中断当前出现的连续性，
  不保存未接受候选的图像；
- 锁屏、最小化、捕获失败、命中排除规则记为缺口并中断出现连续性；目标窗口关闭则自动暂停。

接受点（方案 4.2）：图片可靠落盘，且内容、出现与 OCR 待办在同一 SQLite 事务提交。
控制切换与接受提交用同一把锁串行化：暂停 ACK 只在在途提交完成后写出，
在途采样在提交前核对 epoch，控制已切换则丢弃（方案 4.1）。
所有内存状态只在事务提交成功后更新，避免与数据库不一致。
cadence 与 held 只由采集线程读写；控制线程通过 _cadence_reset 请求重置。
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
from typing import Callable

from . import cadence as cad
from . import control, db, rules, storage, winapi
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
    "excluded": "窗口命中排除规则，暂不采集",
}
UNSAVED_NOTE = {
    cad.NORMAL: "画面正在变化，等稳定后保存",
    cad.DEBUG: "排错限频中，稍后保存最新画面",
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


@dataclass(frozen=True)
class _Obs:
    """一次成功的观察（尚未接受）。"""

    info: winapi.WindowInfo
    cap: winapi.Capture
    digest: str
    reuse_ctx: tuple  # (hwnd, 进程, 宽, 高)：精确复用与变化比例都只在同一上下文内比较
    occ_ctx: tuple  # reuse_ctx + 标题：出现记录的连续性
    observed_ms: int

    @property
    def key(self) -> tuple:
        return (self.reuse_ctx, self.digest)


class Recorder:
    def __init__(
        self,
        cfg: Config,
        wake_ocr: threading.Event,
        stop: threading.Event,
        log: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.wake_ocr = wake_ocr
        self.stop = stop
        self.log = log
        self.clock = clock  # 单调时钟，可注入替身做验收（方案 4.1）
        self.lock = threading.Lock()
        self.tick = threading.Event()  # 控制切换、补拍请求时唤醒采集线程
        self.epoch = 0
        self.recording = False
        self.target: _Target | None = None
        self.session_id: str | None = None
        self.gap_id: int | None = None
        self.gap_reason: str | None = None
        self.current: _Current | None = None
        self.recent: OrderedDict[tuple, _Recent] = OrderedDict()
        # 记录节奏
        self.cadence = cad.Cadence(cad.CadenceParams.from_capture_config(cfg.capture))
        self.held: _Obs | None = None  # 排错模式限频期间的最新候选
        self.mode = cad.NORMAL
        self.debug_deadline: float | None = None  # 单调时钟
        self.debug_until_ms: int | None = None  # UTC，仅供页面显示倒计时
        self._cadence_reset = False
        self._sample_now = False
        self._snapshots: list[int | None] = []  # 待处理补拍：页面请求序号，或 None（快捷键）
        self._last_snapshot_seq = 0
        self._next_due = 0.0
        self.suppressed: tuple | None = None  # (occ_ctx, digest)：用户刚删除的当前画面，画面变化前不再记录
        self.storage_block: str | None = None  # 空间不足时由维护线程设置；期间不开始录制、不写新图片

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
                denied = rules.check(info.process, info.title, self.cfg.rules) if info else None
                if self.storage_block:
                    end_reason = "storage_full" if self.recording else None
                    actual, reason = "paused", self.storage_block
                elif info is None:
                    end_reason = "target_invalid" if self.recording else None
                    actual, reason = "paused", "目标窗口不存在或已变化，请重新选择"
                elif denied:
                    end_reason = "target_excluded" if self.recording else None
                    actual, reason = "paused", f"{denied}，未开始录制"
                elif self.recording and self.target and self.target.hwnd == info.hwnd:
                    actual, reason = "recording", None  # 已在录制同一窗口
                else:
                    end_reason = "switch_target" if self.recording else None
                    start_info = info
                    actual, reason = "recording", None
            else:
                end_reason = "paused" if self.recording else None
                actual, reason = "paused", "已按请求暂停"

            # 记录模式只在录制中生效，暂停后回到普通；排错已在进行时保持原到期时间（不能无限续期）
            wanted = ctrl["desired_mode"] if actual == "recording" else cad.NORMAL
            if wanted not in cad.MODES:
                wanted = cad.NORMAL
            mode_reason = None
            if wanted == cad.DEBUG and self.mode == cad.DEBUG and self.debug_deadline is not None:
                deadline, until_ms = self.debug_deadline, self.debug_until_ms
            elif wanted == cad.DEBUG:
                span_s = self.cfg.capture.debug_max_minutes * 60
                deadline, until_ms = self.clock() + span_s, now + int(span_s * 1000)
            else:
                deadline = until_ms = None
                if self.mode == cad.DEBUG and actual == "recording":
                    mode_reason = "已按请求回到普通模式"

            new_session = db.new_id() if start_info else None
            with db.write_txn(conn):
                if end_reason:
                    self._sql_end_session(conn, now, end_reason)
                if start_info:
                    self._sql_start_session(conn, new_session, now, start_info)
                session_for_ack = new_session if start_info else (self.session_id if actual == "recording" else None)
                control.write_ack(
                    conn, seq, actual, reason, session_for_ack,
                    mode=wanted, mode_until_ms=until_ms, mode_reason=mode_reason,
                )
            # 已提交，再更新内存
            mode_changed = wanted != self.mode
            if end_reason or start_info:
                self.epoch += 1
            if end_reason:
                self._mem_end_session()
            if start_info:
                self._mem_start_session(new_session, start_info)
            self.mode, self.debug_deadline, self.debug_until_ms = wanted, deadline, until_ms
            if end_reason or start_info or mode_changed:
                self._cadence_reset = True  # 作废未接受候选，重建比较基线
                self._sample_now = True  # 开启、恢复、切换目标或模式时直接采样
        self.log.info(
            "control seq=%d desired=%s/%s -> %s/%s%s",
            seq, desired, ctrl["desired_mode"], actual, wanted, f" ({reason})" if reason else "",
        )
        self.tick.set()

    def request_snapshot(self, seq: int | None = None) -> None:
        """补拍请求（页面按钮带序号；快捷键为 None），由采集线程尽快执行。"""
        with self.lock:
            if seq is not None:
                if seq <= self._last_snapshot_seq:
                    return  # 控制轮询在结果写回前会重复看到同一请求
                self._last_snapshot_seq = seq
            self._snapshots.append(seq)
        self.tick.set()

    def storage_pause(self, conn: sqlite3.Connection, reason: str) -> None:
        """维护线程发现空间不足：结束录制段并暂停，之后的开启请求也被拒绝，直到 storage_clear。"""
        with self.lock:
            first = self.storage_block is None
            self.storage_block = reason
            if self.recording:
                with db.write_txn(conn):
                    self._sql_end_session(conn, utc_ms(), "storage_full")
                    control.set_state(conn, "paused", reason)
                self.epoch += 1
                self._mem_end_session()
                self.held = None
        if first:
            self.log.warning(reason)
        self.tick.set()

    def storage_clear(self) -> None:
        with self.lock:
            was, self.storage_block = self.storage_block, None
        if was:
            self.log.info("存储空间已恢复，可以重新开启录制")

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
        self.mode, self.debug_deadline, self.debug_until_ms = cad.NORMAL, None, None
        self._cadence_reset = True  # 暂停时作废未接受候选（方案 4.1）

    # ================= 采集线程 =================

    def _interval(self) -> float:
        cap = self.cfg.capture
        return cap.debug_interval_seconds if self.mode == cad.DEBUG else cap.interval_seconds

    def run(self) -> None:
        conn = db.connect(self.cfg.db_path)
        self._next_due = self.clock()
        try:
            while not self.stop.is_set():
                self.tick.wait(max(0.0, self._next_due - self.clock()))
                self.tick.clear()  # 标志位在 set() 之前写好，clear 后再读，不会丢请求
                if self.stop.is_set():
                    break
                try:
                    self.run_once(conn)
                except Exception:
                    self.log.exception("采样出错")
        finally:
            conn.close()

    def run_once(self, conn: sqlite3.Connection) -> None:
        """处理一次唤醒：先执行补拍，再检查模式到期，最后按节奏决定是否自动采样。"""
        with self.lock:
            snaps, self._snapshots = self._snapshots, []
            immediate, self._sample_now = self._sample_now, False
        for seq in snaps:
            self.snapshot(conn, seq)
        self.check_mode(conn)
        now = self.clock()
        interval = self._interval()
        if immediate:
            self._next_due = now + interval
        elif now < self._next_due:
            self._next_due = min(self._next_due, now + interval)  # 切到更短间隔时不必等旧间隔
            return
        else:
            self._next_due += interval
            if self._next_due <= now:
                self._next_due = now + interval
        self.sample(conn)

    # ---------- 模式 ----------

    def check_mode(self, conn: sqlite3.Connection) -> None:
        """排错模式到时或 OCR 待办过高时退回普通模式（方案 4.1、4.3）。"""
        with self.lock:
            if self.mode != cad.DEBUG or not self.recording:
                return
            deadline = self.debug_deadline
        reason = None
        if deadline is not None and self.clock() >= deadline:
            reason = f"排错模式已满 {self.cfg.capture.debug_max_minutes:g} 分钟，已自动回到普通模式"
        else:
            backlog = conn.execute(
                "SELECT COUNT(*) FROM index_jobs WHERE stage = 'ocr' AND status IN ('pending', 'running')"
            ).fetchone()[0]
            if backlog >= self.cfg.capture.debug_exit_backlog:
                reason = f"OCR 待办达到 {backlog}，排错模式已提前回到普通模式"
        if reason is None:
            return
        with self.lock:
            if self.mode != cad.DEBUG:
                return
            with db.write_txn(conn):
                control.set_mode(conn, cad.NORMAL, None, reason)
            self.mode, self.debug_deadline, self.debug_until_ms = cad.NORMAL, None, None
            self._cadence_reset = True
        self.log.info(reason)

    def _sync_cadence(self) -> None:
        """每次采样前应用控制线程请求的重置与模式。"""
        with self.lock:
            reset, self._cadence_reset = self._cadence_reset, False
            mode = self.mode
        if reset:
            self.held = None
            self.cadence.reset(direct=True)
        self.cadence.set_mode(mode)

    # ---------- 观察 ----------

    def _observe(self, conn: sqlite3.Connection, epoch: int, target: _Target) -> tuple[_Obs | None, str | None]:
        """取一次画面。取不到时登记缺口或自动暂停，返回 (None, 原因)。"""
        observed_ms = utc_ms()
        if winapi.is_input_desktop_locked():
            self._note_gap(conn, epoch, observed_ms, "locked")
            return None, GAP_TEXT["locked"]
        info = winapi.describe_window(target.hwnd)
        if info is None or info.process.lower() != target.process.lower():
            self._auto_pause(conn, epoch, "目标窗口已关闭，录制已自动暂停")
            return None, "目标窗口已关闭"
        denied = rules.check(info.process, info.title, self.cfg.rules)
        if denied:
            self._note_gap(conn, epoch, observed_ms, "excluded", denied)
            return None, denied
        try:
            cap = winapi.capture_window(target.hwnd)
        except winapi.CaptureError as exc:
            if exc.reason == "window_gone":
                self._auto_pause(conn, epoch, "目标窗口已关闭，录制已自动暂停")
            else:
                self._note_gap(conn, epoch, observed_ms, exc.reason, exc.detail)
            return None, GAP_TEXT.get(exc.reason, exc.reason)
        except OSError as exc:
            self._note_gap(conn, epoch, observed_ms, "capture_error", str(exc))
            return None, GAP_TEXT["capture_error"]
        digest = hashlib.blake2b(cap.bgr, digest_size=16).hexdigest()
        reuse_ctx = (info.hwnd, info.process.lower(), cap.width, cap.height)
        return _Obs(info, cap, digest, reuse_ctx, reuse_ctx + (info.title,), observed_ms), None

    def _classify(self, obs: _Obs):
        """返回 'same'（与当前出现的内容一致）、'suppressed'（刚被用户删除、画面未变）、
        _Recent（可精确复用的缓存内容）或 None（新内容）。"""
        if self.suppressed is not None:
            if self.suppressed == (obs.occ_ctx, obs.digest):
                return "suppressed"
            self.suppressed = None
        cur = self.current
        if cur and cur.occ_ctx == obs.occ_ctx and cur.digest == obs.digest and cur.bgr == obs.cap.bgr:
            return "same"
        hit = self.recent.get(obs.key)
        if hit and hit.bgr == obs.cap.bgr:
            return hit
        return None

    def sample(self, conn: sqlite3.Connection) -> None:
        """一次自动采样。"""
        self._sync_cadence()
        with self.lock:
            if not self.recording or self.target is None:
                return
            epoch, target = self.epoch, self.target
        obs, _ = self._observe(conn, epoch, target)
        if obs is None:
            return
        now = self.clock()
        kind = self._classify(obs)

        # 排错模式：这次观察不需要新存，但限频期间留有更早的候选，允许时先保存它
        if kind is not None and self.held is not None and self.cadence.debug_allowed(now):
            held, self.held = self.held, None
            if self._save_new(conn, epoch, held, trigger="held"):
                self.cadence.on_saved(now)
            kind = self._classify(obs)

        w, h = obs.cap.width, obs.cap.height
        if kind == "suppressed":
            self.cadence.observe(now, obs.reuse_ctx, obs.cap.bgr, w, h, changed=False)
            return
        if kind == "same":
            self.cadence.observe(now, obs.reuse_ctx, obs.cap.bgr, w, h, changed=False)
            self._commit_extend(conn, epoch, self.current, obs.observed_ms)
            return
        if kind is not None:
            self.cadence.observe(now, obs.reuse_ctx, obs.cap.bgr, w, h, changed=False)
            if self._commit_reuse(conn, epoch, obs, kind):
                self.cadence.on_settled()
            return

        decision = self.cadence.observe(now, obs.reuse_ctx, obs.cap.bgr, w, h, changed=True)
        if decision == cad.SAVE:
            self.held = None
            if self._save_new(conn, epoch, obs, trigger="auto"):
                self.cadence.on_saved(now)
            return
        if decision == cad.HOLD:
            self.held = obs  # 只保留最新候选
        self._note_interruption(conn, epoch, obs.observed_ms)

    def snapshot(self, conn: sqlite3.Connection, seq: int | None) -> str:
        """补拍：立即捕获并新增一次出现；重复内容复用已保存图片。返回结果说明。"""
        self._sync_cadence()
        with self.lock:
            recording, epoch, target = self.recording, self.epoch, self.target
        if not recording or target is None:
            result = "未在录制，补拍没有执行"
        else:
            obs, why = self._observe(conn, epoch, target)
            if obs is None:
                result = f"补拍没有执行：{why}"
            else:
                kind = self._classify(obs)
                if kind == "suppressed":
                    kind, self.suppressed = None, None  # 手动补拍是用户明确要求，按新内容保存
                if kind == "same":
                    kind = _Recent(self.current.frame_id, self.current.bgr)
                if kind is not None:
                    ok = self._commit_reuse(conn, epoch, obs, kind, source="manual")
                    result = "已补拍：与已保存画面相同，记为一次新的出现" if ok else "补拍没有执行：内容已失效，请重试"
                else:
                    ok = self._save_new(conn, epoch, obs, trigger="manual", source="manual")
                    result = "已补拍：新画面已保存，等待 OCR" if ok else "补拍没有执行：录制状态已变化"
                if ok:
                    self.held = None  # 补拍取得了更新的画面
                    self.cadence.on_settled()
                    self.cadence.last_obs = (obs.reuse_ctx, obs.cap.bgr)
        with db.write_txn(conn):
            control.write_snapshot_result(conn, seq, result, utc_ms())
        self.log.info("snapshot seq=%s: %s", seq, result)
        return result

    # ---------- 提交 ----------

    def _insert_occurrence(
        self, conn, occ_id: str, frame_id: str, info: winapi.WindowInfo, observed_ms: int, source: str = "live"
    ) -> None:
        conn.execute(
            "INSERT INTO occurrences (occurrence_id, frame_id, session_id, first_seen_ms, last_seen_ms, "
            "app, window_title, hwnd, source_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (occ_id, frame_id, self.session_id, observed_ms, observed_ms, info.process, info.title, info.hwnd, source),
        )

    def _sql_close_gap(self, conn, ms: int) -> None:
        if self.gap_id is not None:
            conn.execute("UPDATE gaps SET ended_at_ms = ? WHERE gap_id = ? AND ended_at_ms IS NULL", (ms, self.gap_id))

    def _mem_gap_closed(self) -> None:
        if self.gap_id is not None:
            self.log.info("gap end reason=%s", self.gap_reason)
        self.gap_id = None
        self.gap_reason = None

    def _commit_extend(self, conn, epoch: int, cur: _Current | None, observed_ms: int) -> None:
        with self.lock:
            if cur is None or epoch != self.epoch or self.current is not cur:
                return
            with db.write_txn(conn):
                extended = conn.execute(
                    "UPDATE occurrences SET last_seen_ms = MAX(last_seen_ms, ?), observations = observations + 1 "
                    "WHERE occurrence_id = ?",
                    (observed_ms, cur.occurrence_id),
                ).rowcount
                if extended:
                    self._sql_close_gap(conn, observed_ms)
                    control.set_capture_note(conn, None, observed_ms)
            if not extended:
                # 用户刚删除了这次记录：不再延长，也不在画面不变时立刻重新存入（画面变化或手动补拍后恢复）
                self.current = None
                for k in [k for k, v in self.recent.items() if v.frame_id == cur.frame_id]:
                    self.recent.pop(k, None)
                self.suppressed = (cur.occ_ctx, cur.digest)
                self.log.info("occurrence=%s 已被删除，相同画面不再记录，直到画面变化", cur.occurrence_id)
                return
            self._mem_gap_closed()

    def _commit_reuse(self, conn, epoch: int, obs: _Obs, hit: _Recent, source: str = "live") -> bool:
        occ_id = db.new_id()
        with self.lock:
            if epoch != self.epoch:
                return False
            with db.write_txn(conn):
                still_ok = conn.execute(
                    "SELECT 1 FROM frames WHERE frame_id = ? AND image_status = 'ok'", (hit.frame_id,)
                ).fetchone()
                if still_ok:
                    self._insert_occurrence(conn, occ_id, hit.frame_id, obs.info, obs.observed_ms, source)
                    self._sql_close_gap(conn, obs.observed_ms)
                    control.set_capture_note(conn, None, obs.observed_ms)
            if not still_ok:
                self.recent.pop(obs.key, None)  # 内容已失效，下一次采样按新内容保存
                if self.current and self.current.frame_id == hit.frame_id:
                    self.current = None
                return False
            self._mem_gap_closed()
            self.current = _Current(occ_id, hit.frame_id, obs.occ_ctx, obs.digest, hit.bgr)
            if obs.key in self.recent:
                self.recent.move_to_end(obs.key)
        self.log.info("reuse frame=%s new occurrence=%s source=%s", hit.frame_id, occ_id, source)
        return True

    def _save_new(self, conn, epoch: int, obs: _Obs, *, trigger: str, source: str = "live") -> bool:
        """在事务外编码，再接受新内容。trigger：auto / held / manual，写入 capture_meta 供评测统计。"""
        t0 = time.perf_counter()
        capcfg = self.cfg.capture
        cap = obs.cap
        payload = storage.encode_webp(
            cap.bgr, cap.width, cap.height,
            quality=capcfg.webp_quality, lossless=capcfg.webp_lossless, method=capcfg.webp_method,
        )
        encode_ms = int((time.perf_counter() - t0) * 1000)
        return self._commit_new(conn, epoch, obs, payload, encode_ms, trigger=trigger, source=source)

    def _commit_new(
        self, conn, epoch: int, obs: _Obs, payload: bytes, encode_ms: int, *, trigger: str, source: str
    ) -> bool:
        info, cap, key, observed_ms = obs.info, obs.cap, obs.key, obs.observed_ms
        frame_id, occ_id = db.new_id(), db.new_id()
        rel = storage.frame_rel_path(frame_id, observed_ms)
        meta = json.dumps(
            {"rect": cap.rect, "dpi": cap.dpi, "method": cap.method, "mode": self.cadence.mode, "trigger": trigger},
            ensure_ascii=False,
        )
        capcfg = self.cfg.capture
        fmt = "webp-lossless" if capcfg.webp_lossless else f"webp-q{capcfg.webp_quality}"
        with self.lock:
            if epoch != self.epoch:
                self.log.info("控制已切换，丢弃在途画面")
                return False
            nbytes = storage.write_atomic(self.cfg.data_dir, rel, payload, (cap.width, cap.height))
            try:
                with db.write_txn(conn):
                    now = utc_ms()
                    conn.execute(
                        "INSERT INTO frames (frame_id, image_path, width, height, captured_at_ms, accepted_at_ms, "
                        "pixel_hash, image_format, image_bytes, capture_meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (frame_id, rel, cap.width, cap.height, observed_ms, now, obs.digest, fmt, nbytes, meta),
                    )
                    self._insert_occurrence(conn, occ_id, frame_id, info, observed_ms, source)
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
            self.current = _Current(occ_id, frame_id, obs.occ_ctx, obs.digest, cap.bgr)
            self.recent[key] = _Recent(frame_id, cap.bgr)
            self.recent.move_to_end(key)
            while len(self.recent) > capcfg.recent_cache_size:
                self.recent.popitem(last=False)
        self.wake_ocr.set()
        self.log.info(
            "accepted frame=%s %dx%d bytes=%d encode_ms=%d method=%s dpi=%s mode=%s trigger=%s",
            frame_id, cap.width, cap.height, nbytes, encode_ms, cap.method, cap.dpi, self.cadence.mode, trigger,
        )
        return True

    # ---------- 缺口与中断 ----------

    def _open_gap(self, conn, epoch: int, observed_ms: int, reason: str, detail: str | None, note: str) -> None:
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
                control.set_capture_note(conn, note, None)
            self._mem_gap_closed()
            self.gap_id, self.gap_reason = cur.lastrowid, reason
        self.log.info("gap start reason=%s", reason)

    def _note_gap(self, conn, epoch: int, observed_ms: int, reason: str, detail: str = "") -> None:
        """取不到画面的缺口：旧观察不再可比，未接受候选作废。"""
        self.held = None
        self.cadence.rebaseline()
        self._open_gap(conn, epoch, observed_ms, reason, detail, GAP_TEXT.get(reason, reason))

    def _note_interruption(self, conn, epoch: int, observed_ms: int) -> None:
        """观察到变化但未保存（方案 4.2 表中 t1 的 B）：记轻量中断标记，不保存候选图像。

        detail 记录被中断的出现；下一次接受或复用时结束该标记，前后关系由时间与 session 还原。
        """
        prev = self.current
        detail = json.dumps(
            {"prev_occurrence_id": prev.occurrence_id if prev else None, "mode": self.cadence.mode},
            ensure_ascii=False,
        )
        self._open_gap(conn, epoch, observed_ms, "unsaved_change", detail, UNSAVED_NOTE[self.cadence.mode])

    def _auto_pause(self, conn, epoch: int, reason: str) -> None:
        with self.lock:
            if epoch != self.epoch:
                return
            with db.write_txn(conn):
                self._sql_end_session(conn, utc_ms(), "window_gone")
                control.set_state(conn, "paused", reason)
            self.epoch += 1
            self._mem_end_session()
        self.held = None
        self.log.warning(reason)
