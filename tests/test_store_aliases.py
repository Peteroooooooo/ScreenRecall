"""合并过的块里重复的行（store.aliases）：并入的那一份记成别名，检索和阅读视图只显示一份；
历史不删；隐私删除两份各自判断；迁移给已有的合并树补上。全部是合成文字。"""

from __future__ import annotations

import sqlite3

from screenrecall.store import delete, schema
from screenrecall.store.queries import Queries
from screenrecall.store.writer import LineIn, Writer
from screenrecall.v2 import work_text
from screenrecall.v2.work_common import Ctx, StopFlag
from tests._v2_worker_fakes import make_cfg

TEXTS = [f"第{i}段：一篇可以滚动的合成长文档 synthetic paragraph {i}" for i in range(10)]


def _put(w: Writer, block, t, texts, y0, step=30):
    return w.record_lines(block, t, (y0 - 5, y0 + step * len(texts) + 5),
                          [LineIn(x, 10, y0 + i * step, 410, y0 + i * step + 20) for i, x in enumerate(texts)]).result(5)


def _two_blocks(w: Writer, t=1_000_000):
    """b1 看到第 0–5 段，b2（后来另起的一把标尺）看到第 3–9 段：第 3–5 段两边都有。b2 坐标 + 90 = b1 坐标。"""
    sid = w.segment_for("a.exe", "长文档", t).result(5)
    b1 = w.block_new(sid, t).result(5)
    ids1 = _put(w, b1, t, TEXTS[:6], 0)
    b2 = w.block_new(sid, t + 60_000).result(5)
    ids2 = _put(w, b2, t + 60_000, TEXTS[3:10], 0)
    return sid, b1, b2, ids1, ids2


def _db(path) -> sqlite3.Connection:
    c = sqlite3.connect(str(schema.db_path(path)))
    c.row_factory = sqlite3.Row
    return c


def test_merge_marks_the_duplicate_lines_of_the_source_block(tmp_path):
    t = 1_000_000
    with Writer(tmp_path) as w:
        sid, b1, b2, ids1, ids2 = _two_blocks(w, t)
        assert w.block_merge(b2, b1, 90).result(5) == 3
        pid = w.piece_put(t - 1000, t + 120_000, app="a.exe", title="长文档", segment_ids=[sid]).result(5)
    c = _db(tmp_path)
    alias = {r["line_id"]: r["alias_of"] for r in c.execute("SELECT line_id, alias_of FROM lines")}
    assert [alias[i] for i in ids2[:3]] == ids1[3:6]  # 第 3–5 段：b2 那份指向 b1 那份
    assert all(alias[i] is None for i in ids1 + ids2[3:])
    # 历史还在：两份都在库里；留下的那份代表两次看到
    assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 13
    r = c.execute("SELECT first_seen_ms, last_seen_ms FROM lines WHERE line_id = ?", (ids1[3],)).fetchone()
    assert (r[0], r[1]) == (t, t + 60_000)
    q = Queries(tmp_path)
    try:
        res = q.search("synthetic paragraph 4")
        assert [ln["line_id"] for g in res["groups"] for ln in g["lines"]] == [ids1[4]]
        res = q.search("synthetic paragraph", per_group=20)
        assert res["total"] == 10  # 每段一份
        lines = [ln["text"] for b in q.piece(pid)["segments"][0]["blocks"] for ln in b["lines"]]
        assert lines == TEXTS
    finally:
        q.close()


def test_privacy_delete_still_removes_both_copies_and_unhides_a_copy_seen_outside(tmp_path):
    t = 1_000_000
    with Writer(tmp_path) as w:
        _, b1, b2, ids1, ids2 = _two_blocks(w, t)
        w.block_merge(b2, b1, 90).result(5)
    conn = schema.connect(schema.db_path(tmp_path))
    try:
        # 删掉 b2 被看到的那一刻：b2 的行（含别名那一份）和留下的那份（存在区间伸到了这一刻）都删
        delete.delete_data(conn, tmp_path, time_from=t + 59_000, time_to=t + 61_000, now_ms=t + 10**7)
        left = {r[0] for r in conn.execute("SELECT text FROM lines")}
        assert left == set(TEXTS[:3])
        for x in TEXTS[3:6]:
            n = conn.execute("SELECT COUNT(*) FROM lines WHERE text = ?", (x,)).fetchone()[0]
            assert n == 0
    finally:
        conn.close()


def test_deleting_the_kept_copy_unhides_the_alias(tmp_path):
    t = 1_000_000
    with Writer(tmp_path) as w:
        _, b1, b2, ids1, ids2 = _two_blocks(w, t)
        w.block_merge(b2, b1, 90).result(5)
        conn = schema.connect(schema.db_path(tmp_path))
        try:
            # 只删留下的那一行（模拟它单独落在删除范围里）：别名那份重新显示
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE lines SET first_seen_ms = 10, last_seen_ms = 20 WHERE line_id = ?", (ids1[4],))
            conn.execute("COMMIT")
            delete.delete_data(conn, tmp_path, time_from=0, time_to=100, now_ms=t + 10**7)
            assert conn.execute("SELECT alias_of FROM lines WHERE line_id = ?", (ids2[1],)).fetchone()[0] is None
            assert conn.execute("SELECT alias_of FROM lines WHERE line_id = ?", (ids2[0],)).fetchone()[0] == ids1[3]
        finally:
            conn.close()
    q = Queries(tmp_path)
    try:
        res = q.search("synthetic paragraph 4")
        assert [ln["line_id"] for g in res["groups"] for ln in g["lines"]] == [ids2[1]]
    finally:
        q.close()


def test_lines_written_into_a_merged_block_later_are_aliased_by_the_merge_step(tmp_path):
    cfg = make_cfg(tmp_path, idle={"clip": False})
    t = 1_000_000
    with Writer(cfg.data_dir) as w:
        sid, b1, b2, ids1, ids2 = _two_blocks(w, t)
        w.block_merge(b2, b1, 90).result(5)
        # 合并后录制进程还在 b2 上：往回滚，又看到了第 1、2 段（b1 里早就有）
        late = _put(w, b2, t + 90_000, TEXTS[1:3], -60)
        ctx = Ctx(cfg, w, StopFlag(), clock=lambda: t + 100_000)
        try:
            ctx.meta_put(work_text.MERGE_CURSOR, max(ids2))
            work_text.MergeStep(ctx).run()
        finally:
            ctx.close()
    c = _db(cfg.data_dir)
    got = [c.execute("SELECT alias_of FROM lines WHERE line_id = ?", (i,)).fetchone()[0] for i in late]
    assert got == ids1[1:3]


def test_v2_database_with_merged_blocks_is_backfilled_on_migration(tmp_path):
    p = schema.db_path(tmp_path)
    c = sqlite3.connect(str(p), isolation_level=None)
    c.executescript("BEGIN;" + schema._SCHEMA + schema._MIGRATIONS[2]
                    + "UPDATE meta SET value = '2' WHERE key = 'schema_version'; COMMIT;")
    c.execute("INSERT INTO segments (segment_id, app, title, first_seen_ms, last_seen_ms) VALUES (1, 'a', 't', 1, 9)")
    c.execute("INSERT INTO blocks (block_id, segment_id, created_ms, last_seen_ms) VALUES (1, 1, 1, 9)")
    c.execute("INSERT INTO blocks (block_id, segment_id, created_ms, last_seen_ms, merged_into, offset_px) "
              "VALUES (2, 1, 5, 9, 1, 90)")
    rows = [(1, 1, "第三段合成", 90), (2, 1, "第四段合成", 120), (3, 2, "第三段合成", 0), (4, 2, "第四段合成", 30),
            (5, 2, "第五段合成", 60)]
    for lid, b, text, y in rows:
        c.execute("INSERT INTO lines (line_id, block_id, segment_id, x0, y0, x1, y1, order_key, text, norm, source, "
                  "first_seen_ms, last_seen_ms) VALUES (?, ?, 1, 10, ?, 400, ?, ?, ?, ?, 'uia', ?, ?)",
                  (lid, b, y, y + 20, y, text, text, b, b + 1))
    c.close()
    conn = schema.connect(p)
    try:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "3"
        got = {r[0]: r[1] for r in conn.execute("SELECT line_id, alias_of FROM lines")}
        assert got == {1: None, 2: None, 3: 1, 4: 2, 5: None}
    finally:
        conn.close()
    schema.connect(p).close()  # 再开不重复迁移
