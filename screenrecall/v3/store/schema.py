"""v3 库的表结构与连接（V3_CONTRACT.md §2）。

- 读写连接（connect）：WAL、synchronous=NORMAL、busy_timeout=5000、foreign_keys=ON、secure_delete=ON；
  自动提交模式，写操作由调用方用 BEGIN IMMEDIATE 包成短事务。第一次打开时建表，并写 control 行（id = 1）。
- 只读连接（connect_ro）：query_only，不建库、不迁移（网页、MCP、空闲整理的查询用）。
- 编号列都是 AUTOINCREMENT：隐私删除后编号不会被新行重用（录制进程、缩略图缓存、向量里留着的旧编号
  不会指到别的东西上）。
- 全文索引文档由 Python 的 Unicode 规则（NFKC、\\w）算出，删除时要算出一模一样的文档：
  规则版本（FTS_VERSION）或 Python 的 Unicode 版本变了，读写连接打开时自动重建两张全文表
  （同时按新规则重算 sources.title_norm、clips.norm）。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path
from urllib.request import pathname2url

from ...store.fts import docs

SCHEMA_VERSION = 1
# 全文索引文档的生成规则版本（store.fts.docs，v3 的 tail 一律为空）。规则一改就得重建索引
FTS_VERSION = "v3-f1"
UNICODE_VERSION = unicodedata.unidata_version

_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- 名单：不在表里的进程 = never（不录）
CREATE TABLE apps (
    process  TEXT PRIMARY KEY COLLATE NOCASE,
    tier     TEXT NOT NULL CHECK (tier IN ('record', 'always', 'never')),
    added_ms INTEGER NOT NULL
);

-- 你设的不录窗口和网站
CREATE TABLE excludes (
    exclude_id INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('host', 'title')),
    value      TEXT NOT NULL,
    added_ms   INTEGER NOT NULL,
    UNIQUE (kind, value)
);

CREATE TABLE control (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    paused_until_ms INTEGER,
    rev             INTEGER NOT NULL DEFAULT 0,
    agent_enabled   INTEGER NOT NULL DEFAULT 1,
    recorder_pid    INTEGER,
    recorder_state  TEXT,
    heartbeat_ms    INTEGER,
    last_snap_ms    INTEGER,
    current_app     TEXT,
    current_title   TEXT,
    updated_ms      INTEGER
);

-- ③ 出处：一个网址 / 一个文件 / 一个"软件 + 标题"
CREATE TABLE sources (
    source_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL CHECK (kind IN ('url', 'file', 'app_title')),
    key           TEXT NOT NULL UNIQUE,
    app           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    title_norm    TEXT NOT NULL DEFAULT '',
    url           TEXT,
    path          TEXT,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    shot_id       INTEGER REFERENCES shots (shot_id) ON DELETE SET NULL,
    name          TEXT,
    name_ms       INTEGER
);
CREATE INDEX idx_sources_seen ON sources (last_seen_ms);

-- ④ 字：整个库里每行只存一份（按 norm 去重）
CREATE TABLE lines (
    line_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    text          TEXT NOT NULL,
    norm          TEXT NOT NULL UNIQUE,
    via           TEXT NOT NULL,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL
);
CREATE INDEX idx_lines_seen ON lines (last_seen_ms);

-- 行和出处：在哪看到、第一次和最近一次
CREATE TABLE source_lines (
    source_id     INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    line_id       INTEGER NOT NULL REFERENCES lines (line_id) ON DELETE CASCADE,
    first_seen_ms INTEGER NOT NULL,
    last_seen_ms  INTEGER NOT NULL,
    PRIMARY KEY (source_id, line_id)
) WITHOUT ROWID;
CREATE INDEX idx_source_lines_line ON source_lines (line_id, last_seen_ms);
CREATE INDEX idx_source_lines_seen ON source_lines (source_id, last_seen_ms);

-- 全文索引：两张 contentless 表，rowid = line_id
CREATE VIRTUAL TABLE lines_tri USING fts5 (k, content = '', detail = 'none', tokenize = 'trigram case_sensitive 1');
CREATE VIRTUAL TABLE lines_bi  USING fts5 (k, content = '', detail = 'none', tokenize = 'ascii');
INSERT INTO lines_tri (lines_tri, rank) VALUES ('secure-delete', 1);
INSERT INTO lines_bi (lines_bi, rank) VALUES ('secure-delete', 1);

-- ⑥ 时间线的一条（一次"看"，5 分钟内回到同一个出处合成一条）
CREATE TABLE visits (
    visit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    start_ms      INTEGER NOT NULL,
    end_ms        INTEGER NOT NULL,
    open_since_ms INTEGER,
    dwell_ms      INTEGER NOT NULL DEFAULT 0,
    background    INTEGER NOT NULL DEFAULT 0,
    merged        INTEGER NOT NULL DEFAULT 0,
    new_lines     INTEGER NOT NULL DEFAULT 0,
    copied        INTEGER NOT NULL DEFAULT 0,
    shot_id       INTEGER REFERENCES shots (shot_id) ON DELETE SET NULL
);
CREATE INDEX idx_visits_start ON visits (start_ms);
CREATE INDEX idx_visits_source ON visits (source_id, end_ms);

-- ① 每拍一次（过了跳过检查的）一行
CREATE TABLE snaps (
    snap_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    t_ms       INTEGER NOT NULL,
    source_id  INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    visit_id   INTEGER REFERENCES visits (visit_id) ON DELETE CASCADE,
    reason     TEXT NOT NULL,
    via        TEXT NOT NULL,
    new_lines  INTEGER NOT NULL,
    seen_lines INTEGER NOT NULL,
    shot_id    INTEGER REFERENCES shots (shot_id) ON DELETE SET NULL
);
CREATE INDEX idx_snaps_visit ON snaps (visit_id);
CREATE INDEX idx_snaps_t ON snaps (t_ms);

-- ⑤ 留下的截图（JPEG q80，宽最多 1920），path 相对数据目录
CREATE TABLE shots (
    shot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    t_ms      INTEGER NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources (source_id) ON DELETE CASCADE,
    visit_id  INTEGER REFERENCES visits (visit_id) ON DELETE SET NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('first', 'visit', 'fallback')),
    path      TEXT NOT NULL,
    width     INTEGER NOT NULL,
    height    INTEGER NOT NULL,
    bytes     INTEGER NOT NULL
);
CREATE INDEX idx_shots_t ON shots (t_ms);
CREATE INDEX idx_shots_source ON shots (source_id, t_ms);

-- 截图上每行字的框（JPEG 坐标），搜索结果画黄框用
CREATE TABLE shot_lines (
    shot_id INTEGER NOT NULL REFERENCES shots (shot_id) ON DELETE CASCADE,
    line_id INTEGER NOT NULL REFERENCES lines (line_id) ON DELETE CASCADE,
    x INTEGER NOT NULL, y INTEGER NOT NULL, w INTEGER NOT NULL, h INTEGER NOT NULL,
    PRIMARY KEY (shot_id, line_id)
) WITHOUT ROWID;
CREATE INDEX idx_shot_lines_line ON shot_lines (line_id);

-- 复制过的：复制了什么、在哪复制的、几点
CREATE TABLE clips (
    clip_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    t_ms      INTEGER NOT NULL,
    text      TEXT NOT NULL,
    norm      TEXT NOT NULL,
    source_id INTEGER REFERENCES sources (source_id) ON DELETE SET NULL,
    visit_id  INTEGER REFERENCES visits (visit_id) ON DELETE SET NULL,
    app       TEXT NOT NULL,
    title     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_clips_t ON clips (t_ms);

-- 时间线上的灰字：不在名单的软件、暂停、锁屏
CREATE TABLE grey_spans (
    span_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    reason   TEXT NOT NULL CHECK (reason IN ('unlisted', 'paused', 'locked')),
    app      TEXT,
    start_ms INTEGER NOT NULL,
    end_ms   INTEGER
);
CREATE INDEX idx_grey_start ON grey_spans (start_ms);

-- 可选：每小时一句话（hour_ms = 本机整点的 UTC 毫秒）
CREATE TABLE hour_lines (
    hour_ms INTEGER PRIMARY KEY,
    text    TEXT NOT NULL,
    model   TEXT,
    made_ms INTEGER NOT NULL
);
"""


class SchemaError(RuntimeError):
    """库文件的结构版本和代码不一致，或无法启用 WAL。"""


def _statements(script: str) -> list[str]:
    """把建表脚本切成一条条语句（sqlite3.complete_statement 判断语句结束，注释里的分号不误切）。"""
    out: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    return out  # 末尾剩下的只有注释


def _sql_fold(x):
    return None if x is None else docs.fold(str(x))


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    # sr_fold(x) = NFKC + casefold（SQLite 的 lower / NOCASE 只认 ASCII）
    conn.create_function("sr_fold", 1, _sql_fold, deterministic=True)
    return conn


def connect(path: Path | str, *, now_ms: int = 0) -> sqlite3.Connection:
    """读写连接（录制进程、主进程、空闲整理进程各用一个）。不存在时建库；now_ms 写进 control.updated_ms。"""
    path = Path(path)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    try:
        _configure(conn)
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise SchemaError(f"无法启用 WAL（当前为 {mode}），数据库需放在本机磁盘上")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA secure_delete = ON")
        init_schema(conn, now_ms)
        ensure_fts(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def connect_ro(path: Path | str) -> sqlite3.Connection:
    """只读连接：mode=ro + query_only。库文件不存在时 sqlite 报错（调用方先判断存在）。"""
    path = Path(path).resolve()
    uri = "file:" + pathname2url(str(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None, check_same_thread=False)
    _configure(conn)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def init_schema(conn: sqlite3.Connection, now_ms: int = 0) -> None:
    """第一次建表（一个写事务，两个进程同时打开也只建一次）；版本不对报错。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = None
        if _table_exists(conn, "meta"):
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            version = int(row[0]) if row else None
        if version is None:
            for stmt in _statements(_DDL):
                conn.execute(stmt)
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                             [("schema_version", str(SCHEMA_VERSION)), ("fts_version", FTS_VERSION),
                              ("unicode_version", UNICODE_VERSION)])
            conn.execute("INSERT INTO control (id, updated_ms) VALUES (1, ?)", (int(now_ms),))
        elif version != SCHEMA_VERSION:
            raise SchemaError(f"库文件的结构版本是 {version}，代码是 {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def rebuild_fts(conn: sqlite3.Connection, batch: int = 5000) -> int:
    """清空两张全文表，按当前规则从 lines 重新生成全部文档（须在调用方写事务内）。返回行数。"""
    conn.execute("INSERT INTO lines_tri (lines_tri) VALUES ('delete-all')")
    conn.execute("INSERT INTO lines_bi (lines_bi) VALUES ('delete-all')")
    last, n = 0, 0
    while True:
        rows = conn.execute("SELECT line_id, text FROM lines WHERE line_id > ? ORDER BY line_id LIMIT ?",
                            (last, batch)).fetchall()
        if not rows:
            return n
        fresh = [(int(r[0]), r[1], docs.norm(r[1]), "") for r in rows]
        # norm 规则也可能变了：按新规则重算（新 norm 和别的行撞了就保留旧值，不合并已有的行）
        for lid, _, nm, _ in fresh:
            conn.execute("UPDATE OR IGNORE lines SET norm = ? WHERE line_id = ?", (nm, lid))
        stored = {int(r[0]): r[1] for r in conn.execute(
            "SELECT line_id, norm FROM lines WHERE line_id IN (SELECT value FROM json_each(?))",
            (f"[{','.join(str(x[0]) for x in fresh)}]",))}
        docs.index_rows(conn, [(lid, text, stored[lid], "") for lid, text, _, _ in fresh])
        last = fresh[-1][0]
        n += len(fresh)


def ensure_fts(conn: sqlite3.Connection) -> None:
    """全文索引规则或 Unicode 版本变了 → 重建两张全文表，并按新规则重算标题和复制内容的 norm。"""
    m = {r[0]: r[1] for r in conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('fts_version', 'unicode_version')")}
    if m.get("fts_version") == FTS_VERSION and m.get("unicode_version") == UNICODE_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        rebuild_fts(conn)
        for sid, title in conn.execute("SELECT source_id, title FROM sources").fetchall():
            conn.execute("UPDATE sources SET title_norm = ? WHERE source_id = ?", (docs.norm(title), sid))
        for cid, text in conn.execute("SELECT clip_id, text FROM clips").fetchall():
            conn.execute("UPDATE clips SET norm = ? WHERE clip_id = ?", (docs.norm(text), cid))
        conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                         [("fts_version", FTS_VERSION), ("unicode_version", UNICODE_VERSION)])
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
