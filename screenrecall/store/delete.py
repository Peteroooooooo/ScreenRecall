"""隐私删除：按时间范围和/或应用，把文字、索引、证据图、向量都真正删掉。

范围语义（宁可多删）：
- 文字行：存在区间 [first_seen, last_seen] 与 [time_from, time_to) 有重叠即删；
- 证据帧：captured_ms 落在范围内；文件先用零覆盖再删除；
- 窗口焦点 / 缺口：落在范围内的删掉，跨边界的裁掉范围内那部分（必要时拆成两段）；
- 划过记录：与范围重叠即删；待 OCR 区域随帧删除；
- 片段：完全落在范围内的删掉（连同它的概括）；部分重叠的保留，它的概括清空并标为 stale；
- 小时 / 天概括：与被删内容的时间跨度重叠的清空并标为 stale（可能引用了被删内容，之后重写）；
- 内容源：不再有任何行、帧、划过记录时连同它的块一起删掉；
- 向量：被删行（文本向量，id = line_id）和被删帧（图片向量，id = frame_no）写墓碑并就地清零。
只给 app 不给时间 = 删除该应用的全部记录。

保证真删的设置：每个连接 PRAGMA secure_delete = ON（删除的页面内容清零）；
两张全文表开了 FTS5 secure-delete，并用 'delete' 命令按原文档删除（contentless_delete 实测会留下原文）；
最后 wal_checkpoint(TRUNCATE) 把 WAL 里的旧页面清掉。有别的连接正在读时检查点可能做不完，
报告里 checkpoint_busy = True，调用方可稍后再调 checkpoint()。
SSD 的磨损均衡、文件系统快照不在程序控制范围内。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from .fts import docs

log = logging.getLogger(__name__)

NEG_INF = -(2 ** 62)
POS_INF = 2 ** 62


@dataclass
class DeleteReport:
    lines: int = 0
    frames: int = 0
    files: int = 0
    files_failed: int = 0
    activity: int = 0
    gaps: int = 0
    skims: int = 0
    segments: int = 0
    pieces: int = 0
    summaries_cleared: int = 0
    text_vectors: int = 0
    image_vectors: int = 0
    checkpoint_busy: bool = False
    line_ids: list[int] = field(default_factory=list, repr=False)
    frame_nos: list[int] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("line_ids")
        d.pop("frame_nos")
        return d


def wipe_file(path: Path) -> bool:
    """先用零覆盖再删除；文件不存在返回 False。"""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return False
    with open(path, "r+b") as fh:
        chunk = bytes(min(size, 1 << 20))
        left = size
        while left > 0:
            n = min(left, len(chunk))
            fh.write(chunk[:n])
            left -= n
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()
    return True


def _in(ids: Sequence) -> tuple[str, list]:
    return ", ".join("?" * len(ids)), list(ids)


def checkpoint(conn: sqlite3.Connection, tries: int = 5) -> bool:
    """wal_checkpoint(TRUNCATE)；返回 True 表示有读者占着、没做完。"""
    # TRUNCATE 会用 busy 处理器等读者结束；临时缩短等待，避免每次尝试都卡满 5 秒
    old = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.execute("PRAGMA busy_timeout = 200")
    try:
        for i in range(tries):
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            if not busy:
                return False
            time.sleep(0.1 * (i + 1))
        return True
    finally:
        conn.execute(f"PRAGMA busy_timeout = {int(old)}")


def delete_data(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    *,
    time_from: int | None = None,
    time_to: int | None = None,
    app: str | None = None,
    text_vectors: Sequence = (),
    image_vectors: Sequence = (),
    batch: int = 2000,
) -> DeleteReport:
    """conn 必须是 schema.connect() 的读写连接且不在事务中（会分批开短事务）。

    text_vectors / image_vectors：VectorStore 列表，分别以 line_id / frame_no 为 id。
    """
    if time_from is None and time_to is None and not app:
        raise ValueError("请至少给出时间范围或应用，避免误删全部记录")
    if conn.in_transaction:
        raise RuntimeError("delete_data 需要在事务之外调用")
    conn.execute("PRAGMA secure_delete = ON")
    lo = NEG_INF if time_from is None else int(time_from)
    hi = POS_INF if time_to is None else int(time_to)
    data_dir = Path(data_dir)
    rep = DeleteReport()
    span = [POS_INF, NEG_INF]  # 被删内容的实际时间跨度，用于让概括失效

    def widen(a, b):
        if a is not None:
            span[0] = min(span[0], int(a))
        if b is not None:
            span[1] = max(span[1], int(b))

    seg_filter = "1"
    seg_params: list = []
    if app:
        seg_filter = "segment_id IN (SELECT segment_id FROM segments WHERE app = ? COLLATE NOCASE)"
        seg_params = [app]
    touched_segments: set[int] = set()

    # 1. 文字行（含全文索引、引用它的 tail、帧内位置）
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT line_id, text, norm, tail, segment_id, first_seen_ms, last_seen_ms FROM lines "
                f"WHERE last_seen_ms >= ? AND first_seen_ms < ? AND {seg_filter} LIMIT ?",
                [lo, hi, *seg_params, batch],
            ).fetchall()
            if not rows:
                conn.execute("COMMIT")
                break
            ids = [int(r[0]) for r in rows]
            ph, args = _in(ids)
            docs.unindex_rows(conn, [(r[0], r[1], r[2], r[3]) for r in rows])
            # 别的行的 tail 里抄着被删行的开头：清空并重建它们的索引
            refs = conn.execute(
                f"SELECT line_id FROM lines WHERE tail_line_id IN ({ph}) AND line_id NOT IN ({ph})", args + args
            ).fetchall()
            docs.set_tails(conn, [(int(r[0]), "", None) for r in refs])
            conn.execute(f"DELETE FROM line_in_frame WHERE line_id IN ({ph})", args)
            conn.execute(f"DELETE FROM lines WHERE line_id IN ({ph})", args)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        rep.lines += len(ids)
        rep.line_ids.extend(ids)
        for r in rows:
            touched_segments.add(int(r[4]))
            widen(r[5], r[6])

    # 2. 证据帧（数据库先删，提交后覆盖并删除文件；编码中的帧由写线程发现行已不在后自行删文件）
    frame_where = "captured_ms >= ? AND captured_ms < ?"
    frame_params: list = [lo, hi]
    if app:
        frame_where += (" AND (app = ? COLLATE NOCASE OR segment_id IN "
                        "(SELECT segment_id FROM segments WHERE app = ? COLLATE NOCASE))")
        frame_params += [app, app]
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"SELECT frame_id, frame_no, image_path, segment_id, captured_ms FROM frames WHERE {frame_where} LIMIT ?",
                frame_params + [batch],
            ).fetchall()
            if not rows:
                conn.execute("COMMIT")
                break
            ph, args = _in([r[0] for r in rows])
            conn.execute(f"DELETE FROM line_in_frame WHERE frame_id IN ({ph})", args)
            conn.execute(f"DELETE FROM ocr_regions WHERE frame_id IN ({ph})", args)
            conn.execute(f"DELETE FROM frames WHERE frame_id IN ({ph})", args)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        rep.frames += len(rows)
        for r in rows:
            rep.frame_nos.append(int(r[1]))
            if r[3] is not None:
                touched_segments.add(int(r[3]))
            widen(r[4], r[4])
            try:
                if wipe_file(data_dir / r[2]):
                    rep.files += 1
            except OSError as e:
                rep.files_failed += 1
                log.warning("证据图删除失败 %s：%s", r[2], e)

    with _txn(conn):
        # 3. 划过记录
        sk = conn.execute(
            f"SELECT skim_id, segment_id, start_ms, end_ms FROM skims WHERE end_ms >= ? AND start_ms < ? AND {seg_filter}",
            [lo, hi, *seg_params],
        ).fetchall()
        if sk:
            ph, args = _in([r[0] for r in sk])
            conn.execute(f"DELETE FROM skims WHERE skim_id IN ({ph})", args)
            for r in sk:
                touched_segments.add(int(r[1]))
                widen(r[2], r[3])
        rep.skims = len(sk)

        # 4. 窗口焦点与缺口：裁剪 / 拆分
        rep.activity = _clip_intervals(conn, "activity", "activity_id", "process", lo, hi, app, widen)
        rep.gaps = _clip_intervals(conn, "gaps", "gap_id", "process", lo, hi, app, widen)

        # 5. 片段与概括
        rep.pieces, cleared = _pieces(conn, lo, hi, app, widen)
        rep.summaries_cleared = cleared

    with _txn(conn):
        if span[0] <= span[1]:
            rep.summaries_cleared += _stale_period_summaries(conn, span[0], span[1] + 1)
        # 6. 空了的内容源
        if app:
            touched_segments.update(int(r[0]) for r in conn.execute(
                "SELECT segment_id FROM segments WHERE app = ? COLLATE NOCASE", (app,)))
        rep.segments = _drop_empty_segments(conn, touched_segments)

    # 7. 向量
    for vs in text_vectors:
        rep.text_vectors += vs.delete(rep.line_ids)
    for vs in image_vectors:
        rep.image_vectors += vs.delete(rep.frame_nos)

    rep.checkpoint_busy = checkpoint(conn)
    return rep


class _txn:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, et, ev, tb):
        if et is None:
            self.conn.execute("COMMIT")
        elif self.conn.in_transaction:
            self.conn.execute("ROLLBACK")
        return False


def _clip_intervals(conn, table: str, pk: str, app_col: str, lo: int, hi: int, app: str | None, widen) -> int:
    """区间表（start_ms / end_ms，end 为空 = 仍在进行）：范围内的删掉，跨边界的裁剪，覆盖整个范围的拆成两段。"""
    params: list = [lo, hi]
    app_sql = ""
    if app:
        app_sql = f" AND {app_col} = ? COLLATE NOCASE"
        params.append(app)
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE COALESCE(end_ms, {POS_INF}) > ? AND start_ms < ?{app_sql}", params
    ).fetchall()
    n = 0
    for r in rows:
        start, end = int(r["start_ms"]), r["end_ms"]
        end_v = POS_INF if end is None else int(end)
        widen(max(start, lo), min(end_v, hi) if end_v < POS_INF else start)
        keep_left = start < lo
        keep_right = end_v > hi
        if not keep_left and not keep_right:
            conn.execute(f"DELETE FROM {table} WHERE {pk} = ?", (r[pk],))
        elif keep_left and keep_right:
            conn.execute(f"UPDATE {table} SET end_ms = ? WHERE {pk} = ?", (lo, r[pk]))
            cols = [c for c in r.keys() if c != pk]
            vals = [hi if c == "start_ms" else r[c] for c in cols]
            conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        elif keep_left:
            conn.execute(f"UPDATE {table} SET end_ms = ? WHERE {pk} = ?", (lo, r[pk]))
        else:
            conn.execute(f"UPDATE {table} SET start_ms = ? WHERE {pk} = ?", (hi, r[pk]))
        n += 1
    return n


def _pieces(conn, lo: int, hi: int, app: str | None, widen) -> tuple[int, int]:
    params: list = [lo, hi]
    app_sql = ""
    if app:
        app_sql = (" AND (app = ? COLLATE NOCASE OR piece_id IN (SELECT ps.piece_id FROM piece_segments ps "
                   "JOIN segments s ON s.segment_id = ps.segment_id WHERE s.app = ? COLLATE NOCASE))")
        params += [app, app]
    rows = conn.execute(f"SELECT piece_id, start_ms, end_ms FROM pieces WHERE end_ms >= ? AND start_ms < ?{app_sql}",
                        params).fetchall()
    deleted = cleared = 0
    for pid, start, end in rows:
        widen(start, end)
        if start >= lo and end < hi:
            conn.execute("DELETE FROM summaries WHERE level = 'piece' AND key = ?", (str(pid),))
            conn.execute("DELETE FROM piece_segments WHERE piece_id = ?", (pid,))
            conn.execute("DELETE FROM pieces WHERE piece_id = ?", (pid,))
            deleted += 1
        else:
            cleared += conn.execute(
                "UPDATE summaries SET text = '', refs = '[]', status = 'stale' WHERE level = 'piece' AND key = ?",
                (str(pid),)).rowcount
    return deleted, cleared


def period_span(level: str, key: str) -> tuple[int, int] | None:
    """hour 'YYYY-MM-DDTHH' / day 'YYYY-MM-DD'（本地时间）→ UTC 毫秒 [a, b)。"""
    try:
        if level == "day":
            a = datetime.strptime(key, "%Y-%m-%d")
            b = a + timedelta(days=1)
        elif level == "hour":
            a = datetime.strptime(key, "%Y-%m-%dT%H")
            b = a + timedelta(hours=1)
        else:
            return None
    except ValueError:
        return None
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def _stale_period_summaries(conn, lo: int, hi: int) -> int:
    n = 0
    for sid, level, key, start, end in conn.execute(
        "SELECT summary_id, level, key, start_ms, end_ms FROM summaries WHERE level IN ('hour', 'day') "
        "AND (text != '' OR status = 'ok')"
    ).fetchall():
        if start is None or end is None:
            sp = period_span(level, key)
            if sp is None:
                continue
            start, end = sp
        if start < hi and end > lo:
            conn.execute("UPDATE summaries SET text = '', refs = '[]', status = 'stale' WHERE summary_id = ?", (sid,))
            n += 1
    return n


def _drop_empty_segments(conn, segment_ids: set[int]) -> int:
    n = 0
    for sid in sorted(segment_ids):
        busy = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM lines WHERE segment_id = ?) OR EXISTS (SELECT 1 FROM frames WHERE segment_id = ?) "
            "OR EXISTS (SELECT 1 FROM skims WHERE segment_id = ?)", (sid, sid, sid)
        ).fetchone()[0]
        if busy:
            continue
        blocks = [int(r[0]) for r in conn.execute("SELECT block_id FROM blocks WHERE segment_id = ?", (sid,))]
        if blocks:
            ph, args = _in(blocks)
            conn.execute(f"UPDATE blocks SET merged_into = NULL, offset_px = 0 WHERE merged_into IN ({ph})", args)
            conn.execute(f"UPDATE ocr_regions SET block_id = NULL WHERE block_id IN ({ph})", args)
            conn.execute(f"DELETE FROM blocks WHERE block_id IN ({ph})", args)
        conn.execute("DELETE FROM piece_segments WHERE segment_id = ?", (sid,))
        conn.execute("DELETE FROM segments WHERE segment_id = ?", (sid,))
        n += 1
    return n
