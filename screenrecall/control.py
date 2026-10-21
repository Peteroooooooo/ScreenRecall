"""页面与后台之间的录制控制记录（方案 2.2、2.3、4.1）。

页面只写"期望状态 + 递增请求序号"；后台应用后写回 applied_seq 作为 ACK，并持续写心跳。
页面在 applied_seq 追上 request_seq 之前显示"处理中"，不把点击当成已生效。
控制记录只传开启、暂停、目标窗口、记录模式与补拍请求，不用于转发搜索。
"""

from __future__ import annotations

import sqlite3

from . import db
from .timeutil import utc_ms

HEARTBEAT_STALE_MS = 5000


def read(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM control WHERE id = 1").fetchone()


def daemon_alive(ctrl: sqlite3.Row, now_ms: int | None = None) -> bool:
    now_ms = utc_ms() if now_ms is None else now_ms
    return ctrl["actual_state"] != "stopped" and now_ms - int(ctrl["heartbeat_ms"] or 0) < HEARTBEAT_STALE_MS


def is_pending(ctrl: sqlite3.Row) -> bool:
    return int(ctrl["request_seq"]) > int(ctrl["applied_seq"])


# ---------- 页面侧 ----------

def send_request(
    conn: sqlite3.Connection,
    desired: str,
    *,
    hwnd: int | None = None,
    process: str | None = None,
    title: str | None = None,
    mode: str = "normal",
) -> int:
    if desired not in ("recording", "paused"):
        raise ValueError(desired)
    if mode not in ("normal", "debug"):
        raise ValueError(mode)
    with db.write_txn(conn):
        conn.execute(
            "UPDATE control SET request_seq = request_seq + 1, desired_state = ?, desired_mode = ?, "
            "target_hwnd = ?, target_process = ?, target_title = ?, requested_at_ms = ? WHERE id = 1",
            (desired, mode, hwnd, process, title, utc_ms()),
        )
        return int(conn.execute("SELECT request_seq FROM control WHERE id = 1").fetchone()[0])


def send_mode_request(conn: sqlite3.Connection, mode: str) -> int:
    """录制中切换普通/排错模式：沿用当前目标窗口，只改期望模式。"""
    ctrl = read(conn)
    return send_request(
        conn, "recording",
        hwnd=ctrl["target_hwnd"], process=ctrl["target_process"], title=ctrl["target_title"], mode=mode,
    )


def request_snapshot(conn: sqlite3.Connection) -> int:
    """页面上的"立即补拍"。与录制请求分开计数，不影响开启/暂停的 ACK。"""
    with db.write_txn(conn):
        conn.execute("UPDATE control SET snapshot_seq = snapshot_seq + 1 WHERE id = 1")
        return int(conn.execute("SELECT snapshot_seq FROM control WHERE id = 1").fetchone()[0])


def snapshot_pending(ctrl: sqlite3.Row) -> bool:
    return int(ctrl["snapshot_seq"]) > int(ctrl["snapshot_applied_seq"])


# ---------- 后台侧（除 on_daemon_start/stop/heartbeat 外都须在调用方事务内执行） ----------

def on_daemon_start(conn: sqlite3.Connection, pid: int) -> None:
    """后台启动：录制默认关闭，此前积压的请求视为过期（恢复待办不等于恢复采集）。"""
    now = utc_ms()
    with db.write_txn(conn):
        conn.execute(
            "UPDATE control SET applied_seq = request_seq, desired_state = 'paused', "
            "actual_state = 'paused', state_reason = ?, capture_note = NULL, active_session_id = NULL, "
            "desired_mode = 'normal', actual_mode = 'normal', mode_until_ms = NULL, mode_reason = NULL, "
            "snapshot_applied_seq = snapshot_seq, snapshot_result = NULL, hotkey_status = NULL, "
            "heartbeat_ms = ?, daemon_pid = ?, daemon_started_ms = ? WHERE id = 1",
            ("后台已启动，录制默认关闭", now, pid, now),
        )


def heartbeat(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE control SET heartbeat_ms = ? WHERE id = 1", (utc_ms(),))


def write_ack(
    conn: sqlite3.Connection,
    seq: int,
    actual: str,
    reason: str | None,
    session_id: str | None,
    *,
    mode: str = "normal",
    mode_until_ms: int | None = None,
    mode_reason: str | None = None,
) -> None:
    conn.execute(
        "UPDATE control SET applied_seq = ?, actual_state = ?, state_reason = ?, "
        "active_session_id = ?, capture_note = NULL, actual_mode = ?, mode_until_ms = ?, "
        "mode_reason = ? WHERE id = 1",
        (seq, actual, reason, session_id, mode, mode_until_ms, mode_reason),
    )


def set_state(conn: sqlite3.Connection, actual: str, reason: str | None) -> None:
    """后台自行改变状态（如目标窗口关闭后自动暂停），不改动请求序号。暂停时模式回到普通。"""
    conn.execute(
        "UPDATE control SET actual_state = ?, state_reason = ?, active_session_id = NULL, "
        "capture_note = NULL, actual_mode = 'normal', mode_until_ms = NULL WHERE id = 1",
        (actual, reason),
    )


def set_mode(conn: sqlite3.Connection, mode: str, until_ms: int | None, reason: str | None) -> None:
    """后台自行改变模式（排错到时或过载退回普通），不改动请求序号。"""
    conn.execute(
        "UPDATE control SET actual_mode = ?, mode_until_ms = ?, mode_reason = ? WHERE id = 1",
        (mode, until_ms, reason),
    )


def write_snapshot_result(conn: sqlite3.Connection, seq: int | None, result: str, at_ms: int) -> None:
    """补拍结果。seq 为页面请求序号；快捷键触发时为 None，只更新结果文字。"""
    if seq is None:
        conn.execute(
            "UPDATE control SET snapshot_result = ?, snapshot_at_ms = ? WHERE id = 1", (result, at_ms)
        )
    else:
        conn.execute(
            "UPDATE control SET snapshot_applied_seq = MAX(snapshot_applied_seq, ?), snapshot_result = ?, "
            "snapshot_at_ms = ? WHERE id = 1",
            (seq, result, at_ms),
        )


def set_hotkey_status(conn: sqlite3.Connection, text: str | None) -> None:
    with db.write_txn(conn):
        conn.execute("UPDATE control SET hotkey_status = ? WHERE id = 1", (text,))


def set_capture_note(conn: sqlite3.Connection, note: str | None, last_capture_ms: int | None) -> None:
    if last_capture_ms is None:
        conn.execute("UPDATE control SET capture_note = ? WHERE id = 1", (note,))
    else:
        conn.execute(
            "UPDATE control SET capture_note = ?, last_capture_ms = ? WHERE id = 1",
            (note, last_capture_ms),
        )


def on_daemon_stop(conn: sqlite3.Connection) -> None:
    with db.write_txn(conn):
        conn.execute(
            "UPDATE control SET actual_state = 'stopped', state_reason = '后台已退出', "
            "capture_note = NULL, active_session_id = NULL, actual_mode = 'normal', mode_until_ms = NULL, "
            "hotkey_status = NULL, heartbeat_ms = ? WHERE id = 1",
            (utc_ms(),),
        )
