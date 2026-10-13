"""向量基础设施：规范化、分块、文本块复用、迁移补登记与 OCR 后登记向量待办。"""

import json
import logging
import threading

import numpy as np
import pytest

from screenrecall import db, vectors
from screenrecall.ocr import OcrBox, OcrWorker

from .conftest import make_image


def words(text: str) -> int:
    """测试用 tokenizer：按空白切词，再计入 2 个特殊 token。"""
    return len(text.split()) + 2


def line(no, text, y, *, x0=0.0, x1=100.0, h=10.0, start=None):
    start = no * 100 if start is None else start
    return vectors.OcrLine(no, text, start, start + len(text), x0, y, x1, y + h)


# ---------- 纯函数 ----------

def test_normalize_keeps_inner_spaces_and_case():
    assert vectors.normalize("Ab  c   \nx\t \n") == "Ab  c\nx\n"
    assert vectors.normalize("e\u0301") == "\u00e9"  # NFC


def test_chunk_respects_token_limit_with_special_tokens_and_overlap():
    lines = [line(i, f"w{i} a b", y=i * 12) for i in range(6)]  # 每行 3 词
    chunks = vectors.chunk_lines(lines, words, max_tokens=8)  # 2 行 = 6 词 + 2 = 8
    assert all(c.token_count <= 8 for c in chunks)
    assert [(c.line_start, c.line_end) for c in chunks] == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]
    assert chunks[0].bbox == (0.0, 0.0, 100.0, 22.0)
    assert chunks[0].char_start == 0 and chunks[0].char_end == 100 + len("w1 a b")


def test_chunk_breaks_on_large_vertical_gap_without_overlap():
    lines = [line(0, "a", 0), line(1, "b", 12), line(2, "c", 100), line(3, "d", 112)]
    chunks = vectors.chunk_lines(lines, words, max_tokens=50)
    assert [(c.line_start, c.line_end) for c in chunks] == [(0, 1), (2, 3)]


def test_chunk_splits_overlong_single_line():
    text = " ".join(f"t{i}" for i in range(20))
    chunks = vectors.chunk_lines([line(0, text, 0, start=0)], words, max_tokens=6)
    assert all(c.token_count <= 6 for c in chunks)
    assert "".join(text[c.char_start:c.char_end] for c in chunks) == text
    assert all(c.line_start == c.line_end == 0 for c in chunks)


def test_chunk_skips_blank_lines_and_empty_input():
    assert vectors.chunk_lines([], words) == []
    assert vectors.chunk_lines([line(0, "   ", 0)], words) == []


def test_blob_roundtrip_and_dim_check():
    v = np.arange(4, dtype=np.float32)
    assert np.array_equal(vectors.from_blob(vectors.to_blob(v), 4), v)
    with pytest.raises(ValueError):
        vectors.from_blob(vectors.to_blob(v), 5)


# ---------- 数据库 ----------

def _frame(conn, fid, captured=1000, text="hello", status="done"):
    conn.execute(
        "INSERT INTO frames (frame_id, image_path, width, height, captured_at_ms, accepted_at_ms, pixel_hash, "
        "image_format, image_bytes, capture_meta, ocr_status) VALUES (?, ?, 1, 1, ?, ?, 'h', 'webp', 1, '{}', ?)",
        (fid, f"frames/{fid}.webp", captured, captured, status),
    )
    if text is not None:
        conn.execute(
            "INSERT INTO frame_text (frame_id, text, text_fold, ocr_version, line_count) VALUES (?, ?, ?, 'v', 1)",
            (fid, text, text.lower()),
        )


def test_shared_block_reused_across_frames_with_own_coordinates(h):
    conn = h.conn
    with db.write_txn(conn):
        _frame(conn, "f1")
        _frame(conn, "f2")
        c1 = vectors.Chunk("同一段说明文字", 0, 0, 0, 7, (0, 0, 50, 10), 9)
        c2 = vectors.Chunk("同一段说明文字", 3, 3, 40, 47, (200, 300, 250, 310), 9)
        b1 = vectors.replace_frame_blocks(conn, "f1", [c1], 1)
        b2 = vectors.replace_frame_blocks(conn, "f2", [c2], 1)
        b2_again = vectors.replace_frame_blocks(conn, "f2", [c2], 2)  # 幂等
    assert b1 == b2 == b2_again
    assert conn.execute("SELECT COUNT(*) FROM text_blocks").fetchone()[0] == 1
    rows = conn.execute("SELECT frame_id, x0, line_start FROM frame_blocks ORDER BY frame_id").fetchall()
    assert [tuple(r) for r in rows] == [("f1", 0, 0), ("f2", 200, 3)]


def test_missing_vectors_and_gc_orphans(h):
    conn = h.conn
    with db.write_txn(conn):
        _frame(conn, "f1")
        [bid] = vectors.replace_frame_blocks(conn, "f1", [vectors.Chunk("x", 0, 0, 0, 1, (0, 0, 1, 1), 3)], 1)
        assert vectors.missing_block_vectors(conn, [bid], "m@1") == [bid]
        vectors.put_embedding(conn, target_kind="block", target_id=str(bid), branch="text", model_id="m@1",
                              preprocess=vectors.TEXT_PREPROCESS, vec=np.ones(3, np.float32), now_ms=1)
        assert vectors.missing_block_vectors(conn, [bid], "m@1") == []
        assert vectors.missing_block_vectors(conn, [bid], "m@2") == [bid]  # 换模型不能混用
        conn.execute("DELETE FROM frame_blocks")
        assert vectors.gc_orphan_blocks(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


def test_migration_v2_to_v3_backfills_jobs(tmp_path):
    path = tmp_path / "v2.sqlite3"
    conn = db.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("BEGIN;" + db._SCHEMA + db._MIGRATIONS[2] + "UPDATE meta SET value='2' WHERE key='schema_version'; COMMIT;")
    with db.write_txn(conn):
        conn.execute("INSERT INTO sessions (session_id, started_at_ms, tz_offset_min) VALUES ('s', 0, 0)")
        # 列名在 v2 中还没有向量状态，用原始 INSERT
        for fid, status, text in [("a", "done", "有文字"), ("b", "done", ""), ("c", "failed", None), ("d", "pending", None)]:
            conn.execute(
                "INSERT INTO frames (frame_id, image_path, width, height, captured_at_ms, accepted_at_ms, pixel_hash, "
                "image_format, image_bytes, capture_meta, ocr_status) VALUES (?, ?, 1, 1, 5, 5, 'h', 'w', 1, '{}', ?)",
                (fid, fid, status),
            )
            if text is not None:
                conn.execute(
                    "INSERT INTO frame_text (frame_id, text, text_fold, ocr_version, line_count) VALUES (?, ?, ?, 'v', 1)",
                    (fid, text, text),
                )
    db.init_schema(conn)
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    jobs = {(r[0], r[1]) for r in conn.execute("SELECT frame_id, stage FROM index_jobs")}
    assert jobs == {("a", "text_embed"), ("a", "image_embed"), ("b", "image_embed"), ("c", "image_embed")}
    status = dict(conn.execute("SELECT frame_id, text_embed_status FROM frames").fetchall())
    assert status == {"a": "pending", "b": "skipped", "c": "skipped", "d": "pending"}
    conn.close()


# ---------- OCR 后登记 ----------

class FakeEngine:
    version = "fake-ocr 1"

    def __init__(self, boxes=None, error=None):
        self.boxes = boxes or []
        self.error = error

    def recognize(self, image):
        if self.error:
            raise self.error
        return self.boxes


def _ocr_once(h, engine):
    worker = OcrWorker(h.cfg, engine, threading.Event(), threading.Event(), logging.getLogger("t"))
    job = worker._claim(h.conn)
    assert job is not None
    worker._process(h.conn, job)
    return worker


def _accept_one_frame(h):
    h.show(make_image(7))
    h.start()
    h.rec.sample(h.conn)
    return h.frames()[0]["frame_id"]


def test_ocr_success_registers_text_and_image_jobs(h):
    fid = _accept_one_frame(h)
    _ocr_once(h, FakeEngine([OcrBox("错误码 0x80070005", 0.9, [[0, 0], [10, 0], [10, 5], [0, 5]])]))
    jobs = dict(h.conn.execute("SELECT stage, status FROM index_jobs WHERE frame_id = ?", (fid,)).fetchall())
    assert jobs == {"ocr": "done", "text_embed": "pending", "image_embed": "pending"}
    row = h.conn.execute("SELECT captured_at_ms FROM frames WHERE frame_id = ?", (fid,)).fetchone()
    created = h.conn.execute(
        "SELECT created_at_ms FROM index_jobs WHERE frame_id = ? AND stage = 'text_embed'", (fid,)
    ).fetchone()[0]
    assert created == row[0]  # 待办年龄从抓拍算起
    stats = db.index_stats(h.conn)
    assert stats["backlog"] == 0 and stats["text_backlog"] == 1 and stats["image_backlog"] == 1


def test_ocr_empty_text_registers_image_only(h):
    fid = _accept_one_frame(h)
    _ocr_once(h, FakeEngine([]))
    stages = {r[0] for r in h.conn.execute("SELECT stage FROM index_jobs WHERE frame_id = ?", (fid,))}
    assert stages == {"ocr", "image_embed"}
    assert h.conn.execute("SELECT text_embed_status FROM frames").fetchone()[0] == "skipped"


def test_ocr_final_failure_still_registers_image_job(harness_factory):
    h = harness_factory()
    h.cfg = h.cfg.__class__(**{**h.cfg.__dict__, "ocr": h.cfg.ocr.__class__(**{**h.cfg.ocr.__dict__, "max_attempts": 1})})
    fid = _accept_one_frame(h)
    _ocr_once(h, FakeEngine(error=RuntimeError("boom")))
    jobs = dict(h.conn.execute("SELECT stage, status FROM index_jobs WHERE frame_id = ?", (fid,)).fetchall())
    assert jobs == {"ocr": "failed", "image_embed": "pending"}
    detail = json.loads(h.frames()[0]["capture_meta"])
    assert detail["trigger"] == "auto"
