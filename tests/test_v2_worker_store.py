"""空闲整理进程对 store 的小改动：v2 迁移（text_chunks、frames.label），隐私删除连带段落向量，删除后的重组。"""

from __future__ import annotations

import sqlite3

import numpy as np

from screenrecall.store import delete, schema
from screenrecall.store.queries import Queries
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import Writer
from screenrecall.v2 import work_text
from screenrecall.v2.searchsvc import text_vectors_dir
from screenrecall.v2.work_common import Ctx, StopFlag
from tests._v2_worker_fakes import DIM, FakeEncoder, L, db, make_cfg, put_lines, text_vec
from tests.test_v2_worker import run


def test_v1_database_is_migrated_to_v2(tmp_path):
    p = tmp_path / schema.DB_NAME
    c = sqlite3.connect(str(p), isolation_level=None)
    c.executescript("BEGIN;" + schema._SCHEMA + "COMMIT;")  # v1 的库（录制进程先建的）
    c.execute("INSERT INTO segments (app, title, first_seen_ms, last_seen_ms) VALUES ('a', 'b', 1, 2)")
    c.close()
    c = schema.connect(p)
    assert c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "2"
    cols = {r[1] for r in c.execute("PRAGMA table_info(frames)")}
    assert {"label", "label_score", "label_model"} <= cols
    assert c.execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    c.close()
    schema.connect(p).close()  # 再开不重复迁移


def test_text_chunk_membership_cascades_with_line_delete(tmp_path):
    with Writer(tmp_path) as w:
        _, _, ids = put_lines(w, "a.exe", "t", 1000, ["一", "二"])
        w.call(lambda c: c.executemany("INSERT INTO text_chunks VALUES (?, ?)", [(i, ids[0]) for i in ids])).result(5)
        w.call(lambda c: c.execute("DELETE FROM lines WHERE line_id = ?", (ids[1],))).result(5)
    c = sqlite3.connect(str(tmp_path / schema.DB_NAME))
    assert c.execute("SELECT line_id FROM text_chunks").fetchall() == [(ids[0],)]


def _disk_rows(vdir) -> np.ndarray:
    """向量库目录里盘上的全部向量（含已删除、清零的行）。"""
    parts = [np.fromfile(p, dtype="<f4").reshape(-1, DIM) for p in sorted(vdir.glob("*.vec"))]
    return np.concatenate(parts) if parts else np.zeros((0, DIM), np.float32)


def test_partial_delete_kills_line_and_paragraph_vectors_and_rechunks_survivors(tmp_path):
    cfg = make_cfg(tmp_path, idle={"clip": False})
    t0 = 1_000_000
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t0, ["保留的第一行", "要删除的秘密行", "保留的第三行"])
        # 第二行一直看到 t0 + 600 s；另外两行只在 t0 出现过
        w.record_lines(b, t0 + 600_000, (25, 55), [L("要删除的秘密行", 30)]).result(5)
    run(cfg)
    vdir = text_vectors_dir(cfg.data_dir)
    vs = VectorStore(vdir, DIM)
    whole = text_vec("保留的第一行\n要删除的秘密行\n保留的第三行")
    assert vs.search(whole, k=1)[0].id == -1 and len(vs) == 4  # 3 个单行 + 1 个段落
    # 删除 [t0 + 300 s, t0 + 900 s)：只有第二行与范围重叠
    conn = schema.connect(schema.db_path(cfg.data_dir))
    rep = delete.delete_data(conn, cfg.data_dir, time_from=t0 + 300_000, time_to=t0 + 900_000, text_vectors=[vs],
                             now_ms=t0 + 1_000_000)
    # 被删行自己的向量和整段向量都删了（都是用被删的字算出来的）；另外两行自己的向量留着
    assert rep.lines == 1 and rep.chunk_ids == [-1] and rep.text_vectors == 2
    vs.refresh()
    assert len(vs) == 2
    job = conn.execute("SELECT kind, target, status FROM jobs").fetchone()
    assert tuple(job) == ("text_rechunk", "-1", "pending")
    conn.close()
    secret = [text_vec("要删除的秘密行"), whole]
    disk = _disk_rows(vdir)
    assert not any(np.allclose(row, s) for row in disk for s in secret)  # 盘上也没有了
    # 空闲整理：剩下两行重新拼段落（新的负数编号），它们自己的单行向量不用重算
    enc = FakeEncoder()
    rep = run(cfg, enc=enc)
    assert rep["steps"]["text"]["rechunked"] == 2
    assert enc.batches == [["保留的第一行\n保留的第三行"]]
    c = db(cfg)
    members = c.execute("SELECT line_id, chunk_id FROM text_chunks ORDER BY line_id").fetchall()
    assert [tuple(m) for m in members] == [(ids[0], -2), (ids[2], -2)]
    assert c.execute("SELECT status FROM jobs").fetchone()[0] == "done"
    vs.refresh()
    assert sorted(h.id for h in vs.search(text_vec("保留的第一行\n保留的第三行"), k=5)) == [-2, ids[0], ids[2]]
    q = Queries(cfg.data_dir, text_vectors=VectorStore(vdir, DIM, readonly=True))
    res = q.search("完全无关", encoder=lambda s: text_vec("保留的第一行\n保留的第三行"))
    assert [ln["line_id"] for ln in res["groups"][0]["lines"]] == [ids[0], ids[2]]
    q.close()
    vs.close()
    disk = _disk_rows(vdir)
    assert not any(np.allclose(row, s) for row in disk for s in secret)


def test_rechunk_leaving_one_line_drops_the_paragraph(tmp_path):
    cfg = make_cfg(tmp_path, idle={"clip": False})
    t0 = 1_000_000
    with Writer(cfg.data_dir) as w:
        _, b, ids = put_lines(w, "a.exe", "doc", t0, ["保留的那一行", "要删除的秘密行"])
        w.record_lines(b, t0 + 600_000, (25, 55), [L("要删除的秘密行", 30)]).result(5)
    run(cfg)
    vs = VectorStore(text_vectors_dir(cfg.data_dir), DIM)
    conn = schema.connect(schema.db_path(cfg.data_dir))
    delete.delete_data(conn, cfg.data_dir, time_from=t0 + 300_000, time_to=t0 + 900_000, text_vectors=[vs],
                       now_ms=t0 + 1_000_000)
    conn.close()
    enc = FakeEncoder()
    run(cfg, enc=enc)
    assert enc.batches == []  # 只剩一行：不算段落，它自己的单行向量还在
    assert db(cfg).execute("SELECT COUNT(*) FROM text_chunks").fetchone()[0] == 0
    vs.refresh()
    assert [h.id for h in vs.search(text_vec("保留的那一行"), k=5)] == [ids[0]]
    vs.close()


def test_delete_between_membership_and_append_zeroes_the_vectors(tmp_path):
    """算向量期间发生隐私删除：追加后核对行和段落成员，少了就把刚追加的向量清掉（盘上也清零）。"""
    cfg = make_cfg(tmp_path, idle={"clip": False})
    with Writer(cfg.data_dir) as w:
        _, _, ids = put_lines(w, "a.exe", "doc", 5000, ["甲行的内容", "乙行的内容"])
    with Writer(cfg.data_dir) as w:
        ctx = Ctx(cfg, w, StopFlag())
        step = work_text.TextVectorStep(ctx, FakeEncoder)
        rows = ctx.conn.execute("SELECT line_id, block_id, segment_id, text, order_key, y0, y1, first_seen_ms "
                                "FROM lines").fetchall()
        lines, chunks = work_text.make_units(rows)
        assert len(lines) == 2 and len(chunks) == 1
        vecs = np.stack([text_vec(u.text) for u in lines + chunks])
        res = w.call(work_text._put_units, [u.id for u in lines], [c.line_ids for c in chunks], None, None,
                     1).result(5)
        conn = schema.connect(schema.db_path(cfg.data_dir))
        delete.delete_data(conn, cfg.data_dir, time_from=0, time_to=10_000, text_vectors=[step.vs], now_ms=20_000)
        conn.close()
        step._append(lines, chunks, vecs, res)
        assert all(step.vs.search(v, k=5) == [] for v in vecs)
        assert not _disk_rows(text_vectors_dir(cfg.data_dir)).any()  # 盘上也清零了
        step.close()
        ctx.close()


def test_writer_ops_skip_lines_deleted_before_membership(tmp_path):
    cfg = make_cfg(tmp_path, idle={"clip": False})
    with Writer(cfg.data_dir) as w:
        _, _, ids = put_lines(w, "a.exe", "doc", 5000, ["甲", "乙", "丙"])
        w.call(lambda c: c.execute("DELETE FROM lines WHERE line_id = ?", (ids[1],))).result(5)
        res = w.call(work_text._put_units, ids, [ids], ("worker.text_line_id", ids[-1]), None, 1).result(5)
    assert res == {"chunk_ids": [-1], "chunks_ok": [], "lines_ok": [0, 2]}  # 段落和被删的行不追加向量
    c = db(cfg)
    assert [r[0] for r in c.execute("SELECT line_id FROM text_chunks ORDER BY line_id")] == [ids[0], ids[2]]
    assert tuple(c.execute("SELECT kind, target FROM jobs").fetchone()) == ("text_rechunk", "-1")
