"""SQLite 连接、表结构与事务工具。

约定（方案 2.5、3.3）：
- 每个执行单元（线程/进程）使用自己的连接，autocommit 模式，写操作用 BEGIN IMMEDIATE 短事务；
- WAL 模式：读不阻塞写，但同一时间只有一个写入者；推理与图片编码都放在事务外；
- 所有关联用不可变 ID；时间统一为 UTC 毫秒。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 连续录制段：开启录制时创建，暂停 / 切换目标 / 目标失效 / 后台退出时结束
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    started_at_ms   INTEGER NOT NULL,
    ended_at_ms     INTEGER,
    end_reason      TEXT,
    source_type     TEXT NOT NULL DEFAULT 'live',
    target_hwnd     INTEGER,
    target_process  TEXT,
    target_title    TEXT,
    tz_offset_min   INTEGER NOT NULL,
    tz_name         TEXT
);

-- 保存的画面内容：一份图片及其索引（"保存了什么"）
CREATE TABLE IF NOT EXISTS frames (
    frame_id        TEXT PRIMARY KEY,
    image_path      TEXT NOT NULL UNIQUE,   -- 相对 data_dir，正斜杠
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    captured_at_ms  INTEGER NOT NULL,       -- 决定保存的那次采样的抓拍时间
    accepted_at_ms  INTEGER NOT NULL,       -- 图片落盘且登记提交的时间
    pixel_hash      TEXT NOT NULL,          -- 原始 BGR 像素 blake2b-128，只用于定位比较候选
    image_format    TEXT NOT NULL,
    image_bytes     INTEGER NOT NULL,
    image_status    TEXT NOT NULL DEFAULT 'ok'
                    CHECK (image_status IN ('ok', 'missing', 'expired', 'deleted')),
    ocr_status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (ocr_status IN ('pending', 'done', 'failed')),
    record_version  INTEGER NOT NULL DEFAULT 1,
    capture_meta    TEXT NOT NULL           -- JSON：窗口矩形、DPI、捕获方式
);
CREATE INDEX IF NOT EXISTS idx_frames_captured ON frames (captured_at_ms);

-- 每次观察到某内容的时间与来源（"何时、从哪里看到"）
CREATE TABLE IF NOT EXISTS occurrences (
    occurrence_id   TEXT PRIMARY KEY,
    frame_id        TEXT NOT NULL REFERENCES frames (frame_id),
    session_id      TEXT NOT NULL REFERENCES sessions (session_id),
    first_seen_ms   INTEGER NOT NULL,
    last_seen_ms    INTEGER NOT NULL,
    observations    INTEGER NOT NULL DEFAULT 1,
    app             TEXT,
    window_title    TEXT,
    hwnd            INTEGER,
    source_type     TEXT NOT NULL DEFAULT 'live',
    CHECK (last_seen_ms >= first_seen_ms)
);
CREATE INDEX IF NOT EXISTS idx_occ_frame ON occurrences (frame_id);
CREATE INDEX IF NOT EXISTS idx_occ_time ON occurrences (first_seen_ms, last_seen_ms);
CREATE INDEX IF NOT EXISTS idx_occ_app ON occurrences (app);

-- 录制段内的已知缺口：锁屏、最小化、捕获失败等
CREATE TABLE IF NOT EXISTS gaps (
    gap_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions (session_id),
    started_at_ms   INTEGER NOT NULL,
    ended_at_ms     INTEGER,
    reason          TEXT NOT NULL,
    detail          TEXT
);

-- 完整 OCR 原文，每帧一行。text_id 是显式整数主键，作为 FTS rowid，VACUUM 后不变
CREATE TABLE IF NOT EXISTS frame_text (
    text_id         INTEGER PRIMARY KEY,
    frame_id        TEXT NOT NULL UNIQUE REFERENCES frames (frame_id),
    text            TEXT NOT NULL,          -- 各 OCR 行按行序以 \\n 连接
    text_fold       TEXT NOT NULL,          -- 等长小写检索副本，与 text 字符位置一一对应
    ocr_version     TEXT NOT NULL,
    line_count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ocr_lines (
    frame_id        TEXT NOT NULL REFERENCES frames (frame_id),
    line_no         INTEGER NOT NULL,
    text            TEXT NOT NULL,
    confidence      REAL NOT NULL,
    quad            TEXT NOT NULL,          -- JSON [[x, y] x 4]，原图物理像素
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    char_start      INTEGER NOT NULL,       -- 在 frame_text.text 中的 [start, end)
    char_end        INTEGER NOT NULL,
    PRIMARY KEY (frame_id, line_no)
);

-- 三元组全文索引：索引等长小写副本，查询前对检索词做同样的折叠
CREATE VIRTUAL TABLE IF NOT EXISTS ocr_fts USING fts5 (
    text_fold,
    tokenize = 'trigram case_sensitive 1'
);

-- 持久化索引待办：按 (frame_id, stage) 幂等；领取与写回都校验执行标识和目标版本
CREATE TABLE IF NOT EXISTS index_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_id        TEXT NOT NULL REFERENCES frames (frame_id),
    stage           TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    target_version  INTEGER NOT NULL,
    worker_token    TEXT,
    not_before_ms   INTEGER NOT NULL DEFAULT 0,
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL,
    last_error      TEXT,
    UNIQUE (frame_id, stage)
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON index_jobs (stage, status, job_id);

-- 页面与后台之间的录制控制（单行）。页面写期望状态与递增序号，后台写回 ACK 与心跳
CREATE TABLE IF NOT EXISTS control (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    request_seq        INTEGER NOT NULL DEFAULT 0,
    desired_state      TEXT NOT NULL DEFAULT 'paused'
                       CHECK (desired_state IN ('recording', 'paused')),
    target_hwnd        INTEGER,
    target_process     TEXT,
    target_title       TEXT,
    requested_at_ms    INTEGER,
    applied_seq        INTEGER NOT NULL DEFAULT 0,
    actual_state       TEXT NOT NULL DEFAULT 'stopped'
                       CHECK (actual_state IN ('recording', 'paused', 'stopped')),
    state_reason       TEXT,
    capture_note       TEXT,
    active_session_id  TEXT,
    last_capture_ms    INTEGER,
    heartbeat_ms       INTEGER NOT NULL DEFAULT 0,
    daemon_pid         INTEGER,
    daemon_started_ms  INTEGER
);

INSERT OR IGNORE INTO control (id) VALUES (1);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta (key, value) VALUES ('index_revision', '0');
"""


# 逐版本增量迁移：键为目标版本，值为在同一写事务内执行的 SQL。
# 新增结构时追加下一个版本号（例如向量表用 3），不要改动已发布的版本。
_MIGRATIONS: dict[int, str] = {
    # v2 采集节奏（方案 4.1）：记录模式、排错倒计时、补拍请求与快捷键状态
    2: """
ALTER TABLE control ADD COLUMN desired_mode TEXT NOT NULL DEFAULT 'normal'
    CHECK (desired_mode IN ('normal', 'debug'));
ALTER TABLE control ADD COLUMN actual_mode TEXT NOT NULL DEFAULT 'normal'
    CHECK (actual_mode IN ('normal', 'debug'));
ALTER TABLE control ADD COLUMN mode_until_ms INTEGER;
ALTER TABLE control ADD COLUMN mode_reason TEXT;
ALTER TABLE control ADD COLUMN snapshot_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE control ADD COLUMN snapshot_applied_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE control ADD COLUMN snapshot_result TEXT;
ALTER TABLE control ADD COLUMN snapshot_at_ms INTEGER;
ALTER TABLE control ADD COLUMN hotkey_status TEXT;
CREATE INDEX IF NOT EXISTS idx_gaps_session ON gaps (session_id, started_at_ms);
""",
}


class SchemaError(RuntimeError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise SchemaError(f"无法启用 WAL（当前为 {mode}），数据库需放在本机磁盘上")
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise SchemaError(f"数据库结构版本 {current} 比代码支持的 {SCHEMA_VERSION} 新，请更新代码")
        if current == SCHEMA_VERSION:
            return
    else:
        current = 0
    if current < 1:
        _run_script(conn, _SCHEMA)
        current = 1
    for version in range(current + 1, SCHEMA_VERSION + 1):
        _run_script(
            conn,
            _MIGRATIONS[version]
            + f"\nUPDATE meta SET value = '{version}' WHERE key = 'schema_version';",
        )


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """短写事务：立即取得写锁，异常时回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def bump_revision(conn: sqlite3.Connection) -> None:
    """索引可见内容变化时在同一事务里递增，供页面缓存判断是否需要刷新（方案 3.6）。"""
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'index_revision'"
    )


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'index_revision'").fetchone()
    return int(row[0]) if row else 0


def index_stats(conn: sqlite3.Connection) -> dict:
    jobs = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at_ms) AS oldest FROM index_jobs "
        "WHERE status IN ('pending', 'running')"
    ).fetchone()
    failed = conn.execute("SELECT COUNT(*) FROM index_jobs WHERE status = 'failed'").fetchone()[0]
    frames = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(image_bytes), 0) AS bytes FROM frames "
        "WHERE image_status = 'ok'"
    ).fetchone()
    occurrences = conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
    return {
        "backlog": int(jobs["n"]),
        "backlog_oldest_ms": jobs["oldest"],
        "failed": int(failed),
        "frames": int(frames["n"]),
        "image_bytes": int(frames["bytes"]),
        "occurrences": int(occurrences),
        "revision": get_revision(conn),
    }
