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

SCHEMA_VERSION = 6

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
    # v3 向量基础设施（方案 3.1、3.3、3.7、5.1）：共享文本块、帧-块关联、带模型身份的向量、分阶段就绪状态
    3: """
CREATE TABLE IF NOT EXISTS text_blocks (
    block_id        INTEGER PRIMARY KEY,
    block_key       TEXT NOT NULL UNIQUE,   -- blake2b(规范化版本, 分块版本, 规范化文本)
    text            TEXT NOT NULL,          -- 规范化后的块文本，用于编码；显示原文取 frame_text
    norm_version    TEXT NOT NULL,
    chunker_version TEXT NOT NULL,
    token_count     INTEGER NOT NULL,
    created_at_ms   INTEGER NOT NULL
);
-- 每帧各自的块顺序与位置：相同文本块被多帧复用时，坐标仍分别保存
CREATE TABLE IF NOT EXISTS frame_blocks (
    frame_id        TEXT NOT NULL REFERENCES frames (frame_id),
    seq             INTEGER NOT NULL,
    block_id        INTEGER NOT NULL REFERENCES text_blocks (block_id),
    chunker_version TEXT NOT NULL,
    line_start      INTEGER NOT NULL,       -- ocr_lines.line_no，含两端
    line_end        INTEGER NOT NULL,
    char_start      INTEGER NOT NULL,       -- frame_text.text 中的 [start, end)
    char_end        INTEGER NOT NULL,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    PRIMARY KEY (frame_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_frame_blocks_block ON frame_blocks (block_id);
-- 向量：文本块（branch=text）或整帧（branch=image）；同一检索矩阵只装同一模型与预处理版本
CREATE TABLE IF NOT EXISTS embeddings (
    emb_id             INTEGER PRIMARY KEY,
    target_kind        TEXT NOT NULL CHECK (target_kind IN ('block', 'frame')),
    target_id          TEXT NOT NULL,
    branch             TEXT NOT NULL CHECK (branch IN ('text', 'image')),
    model_id           TEXT NOT NULL,       -- repo@revision
    preprocess_version TEXT NOT NULL,
    dim                INTEGER NOT NULL,
    dtype              TEXT NOT NULL,
    vector             BLOB NOT NULL,
    created_at_ms      INTEGER NOT NULL,
    UNIQUE (target_kind, target_id, branch, model_id, preprocess_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_matrix ON embeddings (branch, model_id, preprocess_version);
ALTER TABLE frames ADD COLUMN text_embed_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (text_embed_status IN ('pending', 'done', 'failed', 'skipped'));
ALTER TABLE frames ADD COLUMN image_embed_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (image_embed_status IN ('pending', 'done', 'failed', 'skipped'));
-- 已有数据补登记：OCR 已完成且有文字的帧补文本待办；OCR 已结束、原图可用的帧补全图待办
UPDATE frames SET text_embed_status = 'skipped'
    WHERE ocr_status = 'failed'
       OR (ocr_status = 'done' AND NOT EXISTS
           (SELECT 1 FROM frame_text t WHERE t.frame_id = frames.frame_id AND t.text != ''));
INSERT OR IGNORE INTO index_jobs (frame_id, stage, status, target_version, created_at_ms, updated_at_ms)
    SELECT f.frame_id, 'text_embed', 'pending', f.record_version, f.captured_at_ms, f.captured_at_ms
    FROM frames f WHERE f.ocr_status = 'done' AND f.image_status != 'deleted' AND f.text_embed_status = 'pending';
INSERT OR IGNORE INTO index_jobs (frame_id, stage, status, target_version, created_at_ms, updated_at_ms)
    SELECT f.frame_id, 'image_embed', 'pending', f.record_version, f.captured_at_ms, f.captured_at_ms
    FROM frames f WHERE f.ocr_status IN ('done', 'failed') AND f.image_status = 'ok';
UPDATE frames SET image_embed_status = 'skipped' WHERE image_status != 'ok';
""",
    # v4 批量导入（方案 6.8）：批次清单与逐文件结果。批次以文件指纹清单去重，重交同一批次只处理未成功的文件
    4: """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id        TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL UNIQUE,   -- blake2b(按顺序的 文件名 + 内容指纹)
    label           TEXT,
    session_id      TEXT NOT NULL REFERENCES sessions (session_id),
    time_known      INTEGER NOT NULL,       -- 1 = 用户给了可信捕获时间；0 = 记导入时间并标"时间未知"
    created_at_ms   INTEGER NOT NULL,
    updated_at_ms   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS import_items (
    batch_id        TEXT NOT NULL REFERENCES import_batches (batch_id),
    seq             INTEGER NOT NULL,       -- 批次内固定顺序
    file_name       TEXT NOT NULL,
    content_hash    TEXT NOT NULL,          -- 文件字节 blake2b-128，只用于导入幂等
    status          TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'skipped')),
    reason          TEXT,
    frame_id        TEXT REFERENCES frames (frame_id),
    updated_at_ms   INTEGER NOT NULL,
    PRIMARY KEY (batch_id, seq)
);
""",
    # v5 收藏（方案 5.2、5.4）：收藏挂在某一次出现上，frame_id 冗余存放便于按画面判断是否受保护。
    # 外键使"有收藏的出现记录"不能被直接删除，删除流程必须先显式处理收藏。
    5: """
CREATE TABLE IF NOT EXISTS favorites (
    occurrence_id   TEXT PRIMARY KEY REFERENCES occurrences (occurrence_id),
    frame_id        TEXT NOT NULL REFERENCES frames (frame_id),
    created_at_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fav_frame ON favorites (frame_id);
CREATE INDEX IF NOT EXISTS idx_occ_last_seen ON occurrences (last_seen_ms);
""",
    # v6 内容分类（方案 6.4）：模型预测的类别，只用于显示和用户主动筛选；人工标注在数据集 CSV 里，不进库
    6: """
CREATE TABLE IF NOT EXISTS frame_labels (
    frame_id        TEXT PRIMARY KEY REFERENCES frames (frame_id),
    label           TEXT NOT NULL,
    score           REAL NOT NULL,
    model_version   TEXT NOT NULL,
    created_at_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frame_labels_label ON frame_labels (label);
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
    """分阶段统计：backlog / backlog_oldest_ms / failed 只算 OCR（文字可搜的前提），向量另列。"""
    stages = {
        r["stage"]: r
        for r in conn.execute(
            "SELECT stage, SUM(status IN ('pending', 'running')) AS n, "
            "MIN(CASE WHEN status IN ('pending', 'running') THEN created_at_ms END) AS oldest, "
            "SUM(status = 'failed') AS failed FROM index_jobs GROUP BY stage"
        )
    }

    def stat(stage: str, key: str) -> int:
        row = stages.get(stage)
        return int(row[key] or 0) if row is not None else 0

    def oldest(stage: str):
        row = stages.get(stage)
        return row["oldest"] if row is not None else None

    frames = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(image_bytes), 0) AS bytes FROM frames "
        "WHERE image_status = 'ok'"
    ).fetchone()
    occurrences = conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
    return {
        "backlog": stat("ocr", "n"),
        "backlog_oldest_ms": oldest("ocr"),
        "failed": stat("ocr", "failed"),
        "text_backlog": stat("text_embed", "n"),
        "text_oldest_ms": oldest("text_embed"),
        "image_backlog": stat("image_embed", "n"),
        "image_oldest_ms": oldest("image_embed"),
        "embed_failed": stat("text_embed", "failed") + stat("image_embed", "failed"),
        "frames": int(frames["n"]),
        "image_bytes": int(frames["bytes"]),
        "occurrences": int(occurrences),
        "revision": get_revision(conn),
    }


def main(argv: list[str] | None = None) -> int:
    """python -m screenrecall.db [--data-dir PATH]：只读打印结构版本与基本计数（升级后核对用）。"""
    import argparse

    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m screenrecall.db")
    ap.add_argument("--data-dir")
    cfg = load_config(ap.parse_args(argv).data_dir)
    conn = sqlite3.connect(f"file:{cfg.db_path.as_posix()}?mode=ro", uri=True)
    try:
        ver = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        frames = conn.execute("SELECT COUNT(*) FROM frames WHERE image_status != 'deleted'").fetchone()[0]
        occ = conn.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
    finally:
        conn.close()
    print(f"数据库 {cfg.db_path}：结构版本 v{ver}（代码支持 v{SCHEMA_VERSION}），画面 {frames}，出现记录 {occ}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
