"""v2 数据库：文件名、表结构、迁移与连接。

约定：
- 每个数据目录一个新库 recall.sqlite3；旧库 screenrecall.sqlite3 留给冻结的评测，不读不写；
- 时间统一为 UTC 毫秒；块内坐标以块第一帧顶端为 0，向下为正（物理像素）；
- 每个线程/进程用自己的连接，autocommit 模式，写操作用 BEGIN IMMEDIATE 短事务；
- 全文索引（lines_tri / lines_bi）在主库文件里，由写入方显式维护（不用触发器，见 fts.docs）；
  向量在单独的文件里（store.vectors），不进 SQLite。

与任务卡表名的差别：
- 多了 piece_segments（片段与内容源的多对多），代替 pieces 里的 JSON 数组，便于按任务筛选和删除；
- lines 多了 segment_id（冗余，按内容源分组与删除不用再连 blocks）、tail / tail_line_id（跨行检索用，
  见 fts.docs）；
- 两个全文表都用普通 contentless（content=''），删除时提供原值：实测 contentless_delete=1 删除后
  原文字节仍留在库文件里，不满足隐私删除；
- 多了 purges（隐私删除登记），别的写入方晚到的、时间落在已删范围里的写入据此丢弃（见 writer）。

全文索引文档由 Python 的 Unicode 规则（NFKC、\\w）算出，删除时要算出一模一样的文档。
规则版本（FTS_VERSION）或 Python 的 Unicode 版本变了，读写连接打开时自动重建两张全文表。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

from .fts import docs

DB_NAME = "recall.sqlite3"
SCHEMA_VERSION = 2

# 全文索引文档的生成规则版本（fts.docs）。删除要提供与写入时完全相同的文档，规则一改就得重建索引
FTS_VERSION = "f1"
UNICODE_VERSION = unicodedata.unidata_version

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 前台窗口焦点记录：每次切换一行；excluded = 命中排除规则（只记时间，不录内容）
CREATE TABLE IF NOT EXISTS activity (
    activity_id  INTEGER PRIMARY KEY,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER,
    hwnd         INTEGER,
    pid          INTEGER,
    process      TEXT,
    title        TEXT,
    excluded     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_activity_start ON activity (start_ms);
CREATE INDEX IF NOT EXISTS idx_activity_process ON activity (process, start_ms);

-- 已知缺口：来自窗口/会话状态（锁屏、最小化、排除、暂停、关闭、出错），不从"没收到帧"推断
CREATE TABLE IF NOT EXISTS gaps (
    gap_id       INTEGER PRIMARY KEY,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER,
    reason       TEXT NOT NULL
                 CHECK (reason IN ('locked', 'minimised', 'excluded', 'paused', 'closed', 'error')),
    detail       TEXT,
    hwnd         INTEGER,
    process      TEXT
);
CREATE INDEX IF NOT EXISTS idx_gaps_start ON gaps (start_ms);

-- 内容源：同一应用 + 标题的一段内容（30 分钟内再出现复用同一个）
CREATE TABLE IF NOT EXISTS segments (
    segment_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    app            TEXT NOT NULL,
    title          TEXT NOT NULL,
    first_seen_ms  INTEGER NOT NULL,
    last_seen_ms   INTEGER NOT NULL,
    volatile_mask  TEXT,                    -- JSON：易变 32px 方块（视频、时钟）
    -- sr_fold(app) / sr_fold(title)：隐藏名单按它们比较。预先算好，检索时不必逐行回调 Python
    -- （5 万个内容源、两个关键词时逐行折叠实测 89 ms）
    app_fold       TEXT NOT NULL DEFAULT '',
    title_fold     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_segments_app_title ON segments (app, title, last_seen_ms);
CREATE INDEX IF NOT EXISTS idx_segments_last ON segments (last_seen_ms);

-- 块：一把独立的纵向标尺。merged_into + offset_px：本块坐标 + offset_px = 目标块坐标
CREATE TABLE IF NOT EXISTS blocks (
    block_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    segment_id    INTEGER NOT NULL REFERENCES segments (segment_id),
    created_ms    INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    anchor        TEXT NOT NULL DEFAULT 'none' CHECK (anchor IN ('top', 'bottom', 'none')),
    merged_into   INTEGER REFERENCES blocks (block_id),
    offset_px     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_blocks_segment ON blocks (segment_id);
CREATE INDEX IF NOT EXISTS idx_blocks_merged ON blocks (merged_into) WHERE merged_into IS NOT NULL;

-- 文字行：同一行只存一次，带存在区间（first_seen → last_seen，消失时记 disappeared_ms）。
-- norm = NFKC + casefold + 去掉全部空白，用于匹配与检索；tail = 下一行 norm 的开头（跨行检索）
CREATE TABLE IF NOT EXISTS lines (
    line_id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- 不复用：全文 rowid、向量 id、tail_line_id 都靠它
    block_id          INTEGER NOT NULL REFERENCES blocks (block_id),
    segment_id        INTEGER NOT NULL REFERENCES segments (segment_id),
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    order_key         REAL NOT NULL,        -- 块内阅读顺序（先 y0 后 x0）
    text              TEXT NOT NULL,
    norm              TEXT NOT NULL,
    tail              TEXT NOT NULL DEFAULT '',
    tail_line_id      INTEGER,              -- tail 取自哪一行；那一行被删时清空 tail（隐私）
    source            TEXT NOT NULL CHECK (source IN ('uia', 'ocr')),
    confidence        REAL NOT NULL DEFAULT 1.0,
    first_seen_ms     INTEGER NOT NULL,
    last_seen_ms      INTEGER NOT NULL,
    disappeared_ms    INTEGER,
    replaces_line_id  INTEGER REFERENCES lines (line_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_lines_live ON lines (block_id, order_key) WHERE disappeared_ms IS NULL;
CREATE INDEX IF NOT EXISTS idx_lines_block_order ON lines (block_id, order_key);
CREATE INDEX IF NOT EXISTS idx_lines_last_seen ON lines (last_seen_ms);
CREATE INDEX IF NOT EXISTS idx_lines_segment ON lines (segment_id, last_seen_ms);
CREATE INDEX IF NOT EXISTS idx_lines_tail ON lines (tail_line_id) WHERE tail_line_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_lines_replaces ON lines (replaces_line_id) WHERE replaces_line_id IS NOT NULL;

-- 三元组全文索引：文档 = norm || tail，detail=none（只给候选，最终用 instr 核验）
CREATE VIRTUAL TABLE IF NOT EXISTS lines_tri USING fts5 (
    k, content = '', detail = 'none', tokenize = 'trigram case_sensitive 1'
);
-- 中日韩二元组 + 词索引：文档由程序生成（相邻两字、行尾单字、字母数字词），用于 1-2 字查询
CREATE VIRTUAL TABLE IF NOT EXISTS lines_bi USING fts5 (
    k, content = '', detail = 'none', tokenize = 'ascii'
);
INSERT INTO lines_tri (lines_tri, rank) VALUES ('secure-delete', 1);
INSERT INTO lines_bi (lines_bi, rank) VALUES ('secure-delete', 1);

-- 证据帧：无损 WebP，路径相对数据目录 frames/YYYY/MM/DD/<id>.webp
CREATE TABLE IF NOT EXISTS frames (
    frame_id      TEXT PRIMARY KEY,
    frame_no      INTEGER NOT NULL UNIQUE,  -- 不复用的整数编号（meta.frame_seq），图片向量用它作 id
    image_path    TEXT NOT NULL UNIQUE,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    bytes         INTEGER,
    captured_ms   INTEGER NOT NULL,
    saved_ms      INTEGER,
    last_seen_ms  INTEGER NOT NULL,          -- 之后又看到像素完全相同的画面时更新（frame_seen）
    segment_id    INTEGER REFERENCES segments (segment_id),
    block_id      INTEGER REFERENCES blocks (block_id),
    pos_y         INTEGER,                  -- 帧顶端在块标尺上的位置
    hwnd          INTEGER,
    app           TEXT,
    title         TEXT,
    pixel_hash    TEXT,
    keep          INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'encoding'
                  CHECK (status IN ('encoding', 'ok', 'failed', 'deleted'))
);
CREATE INDEX IF NOT EXISTS idx_frames_captured ON frames (captured_ms);
CREATE INDEX IF NOT EXISTS idx_frames_block ON frames (block_id, pos_y);
CREATE INDEX IF NOT EXISTS idx_frames_segment ON frames (segment_id);

-- 行出现在哪些证据帧里（帧像素坐标），用于高亮
CREATE TABLE IF NOT EXISTS line_in_frame (
    frame_id  TEXT NOT NULL REFERENCES frames (frame_id) ON DELETE CASCADE,
    line_id   INTEGER NOT NULL REFERENCES lines (line_id) ON DELETE CASCADE,
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    PRIMARY KEY (frame_id, line_id)
);
CREATE INDEX IF NOT EXISTS idx_lif_line ON line_in_frame (line_id);

-- 待 OCR 区域：UIA 读不到字的帧（或其中一块），空闲时处理
CREATE TABLE IF NOT EXISTS ocr_regions (
    region_id   INTEGER PRIMARY KEY,
    frame_id    TEXT NOT NULL REFERENCES frames (frame_id) ON DELETE CASCADE,
    x0 INTEGER NOT NULL, y0 INTEGER NOT NULL, x1 INTEGER NOT NULL, y1 INTEGER NOT NULL,
    block_id    INTEGER REFERENCES blocks (block_id),
    pos_y       INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_ms  INTEGER NOT NULL,
    claimed_ms  INTEGER,
    updated_ms  INTEGER NOT NULL,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ocr_regions_status ON ocr_regions (status, region_id);
CREATE INDEX IF NOT EXISTS idx_ocr_regions_frame ON ocr_regions (frame_id);

-- 快速划过（没停下、没存证据）的范围：块标尺上的 [y_from, y_to)
CREATE TABLE IF NOT EXISTS skims (
    skim_id     INTEGER PRIMARY KEY,
    segment_id  INTEGER NOT NULL REFERENCES segments (segment_id),
    block_id    INTEGER REFERENCES blocks (block_id),
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    y_from      INTEGER,
    y_to        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_skims_segment ON skims (segment_id, start_ms);
CREATE INDEX IF NOT EXISTS idx_skims_start ON skims (start_ms);

-- 片段（会话）：同一应用 + 标题、间隔不超过 2 分钟的连续使用
CREATE TABLE IF NOT EXISTS pieces (
    piece_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    app          TEXT,
    title        TEXT,
    task_label   TEXT,
    created_ms   INTEGER NOT NULL,
    updated_ms   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pieces_start ON pieces (start_ms);
CREATE INDEX IF NOT EXISTS idx_pieces_task ON pieces (task_label) WHERE task_label IS NOT NULL;
CREATE TABLE IF NOT EXISTS piece_segments (
    piece_id    INTEGER NOT NULL REFERENCES pieces (piece_id) ON DELETE CASCADE,
    segment_id  INTEGER NOT NULL REFERENCES segments (segment_id) ON DELETE CASCADE,
    PRIMARY KEY (piece_id, segment_id)
);
CREATE INDEX IF NOT EXISTS idx_piece_segments_segment ON piece_segments (segment_id);

-- 概括缓存：level = piece / hour / day；status = stale 表示来源变了（或被删），需要重写
CREATE TABLE IF NOT EXISTS summaries (
    summary_id     INTEGER PRIMARY KEY,
    level          TEXT NOT NULL CHECK (level IN ('piece', 'hour', 'day')),
    key            TEXT NOT NULL,           -- piece_id / 'YYYY-MM-DDTHH' / 'YYYY-MM-DD'（本地时间）
    start_ms       INTEGER,
    end_ms         INTEGER,
    text           TEXT NOT NULL,
    refs           TEXT NOT NULL DEFAULT '[]',  -- JSON：引用的 piece / line / frame
    status         TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'stale')),
    model          TEXT,
    endpoint_host  TEXT,
    created_ms     INTEGER NOT NULL,
    UNIQUE (level, key)
);
CREATE INDEX IF NOT EXISTS idx_summaries_time ON summaries (start_ms);

-- 空闲整理进程的待办：(kind, target) 幂等
CREATE TABLE IF NOT EXISTS jobs (
    job_id         INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,
    target         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'done', 'failed')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    not_before_ms  INTEGER NOT NULL DEFAULT 0,
    created_ms     INTEGER NOT NULL,
    updated_ms     INTEGER NOT NULL,
    last_error     TEXT,
    UNIQUE (kind, target)
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (status, kind, not_before_ms);

-- 控制（单行）：界面写期望状态与规则，录制进程写回心跳与当前窗口
CREATE TABLE IF NOT EXISTS control (
    id                       INTEGER PRIMARY KEY CHECK (id = 1),
    request_seq              INTEGER NOT NULL DEFAULT 0,
    desired_state            TEXT NOT NULL DEFAULT 'paused'
                             CHECK (desired_state IN ('recording', 'paused')),
    pause_until_ms           INTEGER,
    pinned_windows           TEXT NOT NULL DEFAULT '[]',
    excluded_processes       TEXT NOT NULL DEFAULT '[]',
    excluded_title_keywords  TEXT NOT NULL DEFAULT '[]',
    hidden_from_agents       TEXT NOT NULL DEFAULT '{"apps": [], "title_keywords": []}',
    agent_enabled            INTEGER NOT NULL DEFAULT 0,
    recorder_heartbeat_ms    INTEGER NOT NULL DEFAULT 0,
    recorder_pid             INTEGER,
    recorder_state           TEXT NOT NULL DEFAULT 'stopped'
                             CHECK (recorder_state IN ('stopped', 'starting', 'recording', 'paused', 'error')),
    recorder_note            TEXT,
    current_hwnd             INTEGER,
    current_process          TEXT,
    current_title            TEXT,
    current_since_ms         INTEGER,
    updated_ms               INTEGER NOT NULL DEFAULT 0
);

-- 隐私删除登记：[time_from, time_to) + 应用（空 = 所有应用）。删除开始前先登记，
-- 别的进程晚到的写入（录制进程手里的候选帧、空闲 OCR 结果）时间落在范围里就丢弃。保留 1 天
CREATE TABLE IF NOT EXISTS purges (
    purge_id    INTEGER PRIMARY KEY,
    time_from   INTEGER NOT NULL,
    time_to     INTEGER NOT NULL,
    app         TEXT,
    created_ms  INTEGER NOT NULL
);

INSERT OR IGNORE INTO control (id) VALUES (1);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '1');
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
INSERT OR IGNORE INTO meta (key, value) VALUES ('frame_seq', '0');
"""

# 逐版本增量迁移：键为目标版本，值为在同一写事务内执行的 SQL。只追加，不改已发布的版本
_MIGRATIONS: dict[int, str] = {
    # v2（空闲整理进程）：
    # - text_chunks：文字向量按"短段落"算（同一块里阅读顺序相邻的几行），一个向量对应多行。
    #   chunk_id = 向量 id（通常是段落第一行的 line_id；被部分删除后重组的段落用负数编号，见 v2.work_text）。
    #   行删除时成员关系级联删除；隐私删除据此把含有被删行的整段向量一起删掉（store.delete）；
    # - frames.label / label_score / label_model：内容分类器（CLIP 图片向量 + 线性分类器）给证据帧的类别。
    2: """
CREATE TABLE IF NOT EXISTS text_chunks (
    line_id   INTEGER PRIMARY KEY REFERENCES lines (line_id) ON DELETE CASCADE,
    chunk_id  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_text_chunks_chunk ON text_chunks (chunk_id);
ALTER TABLE frames ADD COLUMN label TEXT;
ALTER TABLE frames ADD COLUMN label_score REAL;
ALTER TABLE frames ADD COLUMN label_model TEXT;
""",
}


class SchemaError(RuntimeError):
    pass


def db_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / DB_NAME


def _sql_fold(s):
    return None if s is None else docs.fold(str(s))


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # sr_fold(x) = NFKC + casefold：隐藏名单、隐私删除登记按应用/标题比较时用（SQLite 的 lower/NOCASE 只认 ASCII）
    conn.create_function("sr_fold", 1, _sql_fold, deterministic=True)
    return conn


def connect(path: Path | str, *, migrate: bool = True) -> sqlite3.Connection:
    """读写连接（录制进程、空闲整理进程、主进程的写线程各用一个）。

    - WAL：读不阻塞写，写入者一次一个，靠 busy_timeout 排队；
    - synchronous = NORMAL：WAL 下每次提交不刷盘，只在检查点刷。代价是断电/系统崩溃时
      可能丢最后几次提交（库本身不会损坏）；程序崩溃不丢。录制数据每秒都在补，换来写入快约一个数量级；
    - secure_delete = ON：删除的内容在页面里清零，隐私删除才是真删（每个连接都要设）。
    """
    path = Path(path)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    _configure(conn)
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise SchemaError(f"无法启用 WAL（当前为 {mode}），数据库需放在本机磁盘上")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA secure_delete = ON")
    if migrate:
        try:
            init_schema(conn)
        except BaseException:
            conn.close()
            raise
    return conn


def connect_ro(path: Path | str) -> sqlite3.Connection:
    """只读连接（网页、MCP）。

    录制进程没在运行时库文件可能还不存在，或结构版本比代码旧：先用一次读写连接建库/迁移再关掉，
    然后以 mode=ro 打开（WAL 的 -shm/-wal 不存在时 SQLite 会在可写目录里自己建）。
    另设 query_only 防止误写。
    """
    path = Path(path)
    m = _read_meta(path)
    if (int(m.get("schema_version", 0)) != SCHEMA_VERSION or m.get("fts_version") != FTS_VERSION
            or m.get("unicode_version") != UNICODE_VERSION):
        connect(path).close()
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0,
                           isolation_level=None, check_same_thread=False)
    _configure(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _read_meta(path: Path) -> dict[str, str]:
    """不建库、不加锁地读 meta；文件不存在或还没有 meta 表时返回空 dict。"""
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    try:
        return {str(k): str(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def schema_version(path: Path | str) -> int:
    """不建库、不加锁地读结构版本；文件不存在或还没有 meta 表时返回 0。"""
    return int(_read_meta(Path(path)).get("schema_version", 0))


def init_schema(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise SchemaError(f"数据库结构版本 {current} 比代码支持的 {SCHEMA_VERSION} 新，请更新代码")
        if current == SCHEMA_VERSION:
            _ensure_fts(conn)
            return
    else:
        current = 0
    if current < 1:
        _run_script(conn, _SCHEMA)
        current = 1
    for version in range(current + 1, SCHEMA_VERSION + 1):
        _run_script(
            conn,
            _MIGRATIONS[version] + f"\nUPDATE meta SET value = '{version}' WHERE key = 'schema_version';",
        )
    _ensure_fts(conn)


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """全文索引规则或 Unicode 版本与库里登记的不同 → 重建两张全文表（新库只是登记版本）。

    不重建的话，之后按新规则算出的删除文档和库里的对不上，隐私删除会删不干净。
    """
    m = {r[0]: r[1] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('fts_version', 'unicode_version')")}
    if m.get("fts_version") == FTS_VERSION and m.get("unicode_version") == UNICODE_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "fts_version" in m or conn.execute("SELECT 1 FROM lines LIMIT 1").fetchone():
            docs.rebuild_index(conn)
            conn.execute("UPDATE segments SET app_fold = sr_fold(app), title_fold = sr_fold(title)")
        conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         [("fts_version", FTS_VERSION), ("unicode_version", UNICODE_VERSION)])
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def bump_revision(conn: sqlite3.Connection) -> None:
    """可见内容变化时在同一事务里递增，页面据此判断是否刷新。"""
    conn.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'revision'")


def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row[0]) if row else 0
