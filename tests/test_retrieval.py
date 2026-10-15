"""向量 worker 与两种搜索模式：替身编码器验证规则，真实模型（若本机已下载）做冒烟检查。"""

import logging
import threading
import zlib

import numpy as np
import pytest

from screenrecall import db, models, retrieval, search
from screenrecall.embed import EmbedWorker
from screenrecall.ocr import OcrBox, OcrWorker

from .conftest import make_image

DIM = 64


def _bow(text: str) -> np.ndarray:
    """确定性的词袋向量：按空白切词并哈希到 DIM 维，L2 归一化。"""
    v = np.zeros(DIM, dtype=np.float32)
    for w in text.lower().split():
        v[zlib.crc32(w.encode()) % DIM] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v


class FakeText:
    model_id = "fake-text@1"
    max_tokens = 128

    def __init__(self):
        self.encoded: list[str] = []

    def count_tokens(self, text):
        return len(text.split()) + 2

    def encode(self, texts):
        self.encoded.extend(texts)
        return np.vstack([_bow(t) for t in texts])


class FakeImage:
    model_id = "fake-image@1"

    def encode_images(self, images):
        out = []
        for im in images:
            mean = np.asarray(im.convert("RGB"), dtype=np.float32).mean(axis=(0, 1)) / 255.0
            v = np.concatenate([mean, 1 - mean]).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.vstack(out)


def fake_clip_text(texts):
    """'dark' 指向暗图，'bright' 指向亮图，与 FakeImage 的空间对齐。"""
    out = []
    for t in texts:
        mean = np.zeros(3) if "dark" in t else np.ones(3)
        v = np.concatenate([mean, 1 - mean]).astype(np.float32)
        out.append(v / np.linalg.norm(v))
    return np.vstack(out)


def box(text, y=0):
    return OcrBox(text, 0.9, [[0, y], [100, y], [100, y + 10], [0, y + 10]])


class Pipeline:
    def __init__(self, h):
        self.h = h
        self.text = FakeText()
        log = logging.getLogger("t")
        self.embed = EmbedWorker(h.cfg, threading.Event(), threading.Event(), log,
                                 text_encoder_factory=lambda: self.text, image_encoder_factory=FakeImage)
        self.started = False

    def add(self, img, lines):
        """采集一张新画面并跑完 OCR（替身引擎）与全部向量待办。返回 frame_id。"""
        h = self.h
        h.show(img)
        if not self.started:
            h.start()
            h.rec.sample(h.conn)
            self.started = True
        else:
            h.rec.snapshot(h.conn, None)
        fid = h.conn.execute("SELECT frame_id FROM frames ORDER BY rowid DESC LIMIT 1").fetchone()[0]

        class E:
            version = "fake"

            def recognize(self, image):
                return [box(t, 12 * i) for i, t in enumerate(lines)]

        w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
        job = w._claim(h.conn)
        w._process(h.conn, job)
        while self.embed.run_once(h.conn):
            pass
        return fid

    def engines(self):
        return retrieval.Engines(
            text_encoder=self.text.encode,
            clip_text_encoder=fake_clip_text,
            text_index=retrieval.TextVectorIndex(self.text.model_id),
            image_index=retrieval.ImageVectorIndex(FakeImage.model_id),
        )


@pytest.fixture
def p(h):
    return Pipeline(h)


def q(raw):
    return search.parse_query(raw)


# ---------- 向量 worker ----------

def test_embed_worker_writes_blocks_and_vectors(p):
    fid = p.add(make_image(1), ["error code 0x80070005", "access denied"])
    c = p.h.conn
    row = c.execute("SELECT text_embed_status, image_embed_status FROM frames WHERE frame_id = ?", (fid,)).fetchone()
    assert tuple(row) == ("done", "done")
    assert c.execute("SELECT COUNT(*) FROM frame_blocks WHERE frame_id = ?", (fid,)).fetchone()[0] == 1
    kinds = {tuple(r) for r in c.execute("SELECT target_kind, branch FROM embeddings")}
    assert kinds == {("block", "text"), ("frame", "image")}
    assert db.index_stats(c)["text_backlog"] == 0


def test_shared_text_encoded_once_for_two_frames(p):
    p.add(make_image(1), ["同一段 说明 文字"])
    p.add(make_image(2), ["同一段 说明 文字"])  # 画面不同、文字相同
    c = p.h.conn
    assert p.text.encoded == ["同一段 说明 文字"]
    assert c.execute("SELECT COUNT(*) FROM text_blocks").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM frame_blocks").fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM embeddings WHERE branch = 'image'").fetchone()[0] == 2  # 新图照算全图


def test_embed_yields_to_ocr_backlog(harness_factory):
    h = harness_factory()
    h.cfg = h.cfg.__class__(**{**h.cfg.__dict__, "index": h.cfg.index.__class__(ocr_high_water=1)})
    p = Pipeline(h)
    p.add(make_image(1), ["a b c"])
    h.show(make_image(9))
    h.rec.snapshot(h.conn, None)  # 一个 OCR 待办
    with db.write_txn(h.conn):
        h.conn.execute("UPDATE index_jobs SET status = 'pending' WHERE stage = 'image_embed'")
    assert p.embed.run_once(h.conn) is False


def test_deleted_during_inference_is_not_written_back(p):
    h = p.h
    h.show(make_image(3))
    h.start()
    h.rec.sample(h.conn)

    class E:
        version = "fake"

        def recognize(self, image):
            return [box("secret text")]

    w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    w._process(h.conn, w._claim(h.conn))
    job = p.embed._claim(h.conn)
    with db.write_txn(h.conn):  # 推理期间被删除（逻辑失效 + 版本提升）
        h.conn.execute("UPDATE frames SET image_status = 'deleted', record_version = record_version + 1")
    p.embed._process_text(h.conn, job)
    assert h.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0
    assert h.conn.execute("SELECT status FROM index_jobs WHERE job_id = ?", (job.job_id,)).fetchone()[0] == "cancelled"


def test_embed_failure_retries_then_fails(p):
    class Boom(FakeText):
        def encode(self, texts):
            raise RuntimeError("oom")

    p.text = Boom()
    p.embed._text = p.text
    h = p.h
    h.cfg = h.cfg.__class__(**{**h.cfg.__dict__, "index": h.cfg.index.__class__(embed_max_attempts=1)})
    p.embed.cfg = h.cfg
    fid = p.add(make_image(4), ["x y"])
    row = h.conn.execute("SELECT text_embed_status, image_embed_status FROM frames WHERE frame_id = ?", (fid,)).fetchone()
    assert tuple(row) == ("failed", "done")  # 一路失败不影响另一路


# ---------- 文字模式 ----------

def test_rrf_formula():
    s = retrieval.rrf([["a", "b"], ["b", "c"]], k=60)
    assert s["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert s["a"] == pytest.approx(1 / 61) and s["c"] == pytest.approx(1 / 62)


def test_text_mode_fuses_lexical_and_semantic(p):
    f_lex = p.add(make_image(1), ["restaurant", "menu today"])
    f_sem = p.add(make_image(2), ["colleague recommended a place", "restaurant nearby"])
    p.add(make_image(3), ["completely unrelated build log"])
    resp = retrieval.run(p.h.conn, q("colleague restaurant"), search.Filters(), mode="text", engines=p.engines())
    ids = [r.frame_id for r in resp.results]
    assert set(ids[:2]) == {f_lex, f_sem}
    assert resp.rank_basis == "rrf"
    top = resp.results[0]
    assert "词法" in top.sources or "语义" in top.sources
    assert all(r.score is not None for r in resp.results)


def test_semantic_only_hit_has_block_lines_for_highlight(p):
    fid = p.add(make_image(5), ["header", "", "delivery order number 16"])
    resp = retrieval.run(p.h.conn, q("delivery order"), search.Filters(), mode="text", engines=p.engines())
    r = next(r for r in resp.results if r.frame_id == fid)
    assert r.semantic_lines and r.snippet


def test_quoted_filter_applies_to_both_branches(p):
    f1 = p.add(make_image(1), ["Model-X battery capacity"])
    p.add(make_image(2), ["Model-Y battery capacity"])
    resp = retrieval.run(p.h.conn, q('"Model-X" battery'), search.Filters(), mode="text", engines=p.engines())
    assert [r.frame_id for r in resp.results] == [f1]


def test_quote_only_query_uses_time_order_without_models(p):
    p.add(make_image(1), ["0x80070005 failed"])
    eng = retrieval.Engines()  # 没有任何模型
    resp = retrieval.run(p.h.conn, q('"0x80070005"'), search.Filters(), mode="text", engines=eng)
    assert resp.rank_basis == "time" and len(resp.results) == 1


def test_text_mode_without_model_falls_back_to_lexical_with_note(p):
    p.add(make_image(1), ["restaurant menu"])
    eng = retrieval.Engines(unavailable_reason={"text": "模型未下载"})
    resp = retrieval.run(p.h.conn, q("restaurant"), search.Filters(), mode="text", engines=eng)
    assert len(resp.results) == 1 and any("模型未下载" in n for n in resp.notes)


def test_pending_vectors_still_participate_lexically(p):
    fid = p.add(make_image(1), ["restaurant menu"])
    with db.write_txn(p.h.conn):
        p.h.conn.execute("DELETE FROM embeddings")
        p.h.conn.execute("UPDATE frames SET text_embed_status = 'pending'")
        db.bump_revision(p.h.conn)
    resp = retrieval.run(p.h.conn, q("restaurant"), search.Filters(), mode="text", engines=p.engines())
    assert [r.frame_id for r in resp.results] == [fid]
    assert any("尚未完成" in n for n in resp.notes)


def test_date_filter_restricts_semantic_branch(p):
    p.add(make_image(1), ["restaurant menu"])
    resp = retrieval.run(p.h.conn, q("restaurant"), search.Filters(start_ms=10**15), mode="text", engines=p.engines())
    assert not resp.results


# ---------- 画面模式 ----------

def test_image_mode_ranks_by_whole_frame(p):
    dark = p.add(np.zeros((30, 40, 3), np.uint8) + 10, ["a"])
    bright = p.add(np.zeros((30, 40, 3), np.uint8) + 245, ["b"])
    eng = p.engines()
    r1 = retrieval.run(p.h.conn, q("dark background"), search.Filters(), mode="image", engines=eng)
    r2 = retrieval.run(p.h.conn, q("bright page"), search.Filters(), mode="image", engines=eng)
    assert r1.results[0].frame_id == dark and r2.results[0].frame_id == bright
    assert r1.rank_basis == "image" and r1.results[0].sources == ("画面",)


def test_image_mode_excludes_expired_images_and_does_not_switch_mode(p):
    fid = p.add(make_image(1), ["a"])
    with db.write_txn(p.h.conn):
        p.h.conn.execute("UPDATE frames SET image_status = 'expired' WHERE frame_id = ?", (fid,))
        db.bump_revision(p.h.conn)
    resp = retrieval.run(p.h.conn, q("dark"), search.Filters(), mode="image", engines=p.engines())
    assert not resp.results
    resp = retrieval.run(p.h.conn, q("dark"), search.Filters(), mode="image", engines=retrieval.Engines())
    assert "不会自动切换" in resp.message


def test_index_refresh_after_revision_change(p):
    p.add(make_image(1), ["alpha beta"])
    eng = p.engines()
    retrieval.run(p.h.conn, q("alpha"), search.Filters(), mode="text", engines=eng)
    rev = eng.text_index.revision
    p.add(make_image(2), ["gamma delta"])
    resp = retrieval.run(p.h.conn, q("gamma"), search.Filters(), mode="text", engines=eng)
    assert eng.text_index.revision > rev and resp.results


# ---------- 真实模型冒烟（需本机已下载，约 1–2 GB 内存） ----------

_real = pytest.mark.skipif(bool(models.available(models.DEFAULT_MODELS)), reason="本机未下载锁定版本的模型")


@_real
def test_real_minilm_cross_language_semantic(p):
    cfg = models.DEFAULT_MODELS
    real = models.StText(cfg.text, cfg)
    p.text = real
    p.embed._text = real
    target = p.add(make_image(1), ["Lunch: my colleague recommended a Sichuan restaurant near the office"])
    p.add(make_image(2), ["Traceback (most recent call last): KeyError: 'user_id'"])
    p.add(make_image(3), ["Quarterly report spreadsheet total 16,240"])
    eng = retrieval.Engines(text_encoder=real.encode, text_index=retrieval.TextVectorIndex(real.model_id))
    resp = retrieval.run(p.h.conn, q("同事推荐的餐厅"), search.Filters(), mode="text", engines=eng)
    assert resp.results[0].frame_id == target
    assert resp.results[0].sources == ("语义",)
    # 真实 tokenizer 计入特殊 token
    assert real.count_tokens("hello") >= 3


@_real
def test_real_clip_dark_vs_white(p):
    cfg = models.DEFAULT_MODELS
    img_enc = models.StImage(cfg.image, cfg)
    clip_txt = models.StText(cfg.clip_text, cfg)
    p.embed._image = img_enc
    dark = np.zeros((300, 400, 3), np.uint8)
    dark[150:, :, :] = 40  # 深色背景
    white = np.full((300, 400, 3), 250, np.uint8)
    f_dark = p.add(dark, ["x"])
    f_white = p.add(white, ["y"])
    eng = retrieval.Engines(clip_text_encoder=clip_txt.encode, image_index=retrieval.ImageVectorIndex(img_enc.model_id))
    r = retrieval.run(p.h.conn, q("一张全黑的深色图片"), search.Filters(), mode="image", engines=eng)
    assert r.results[0].frame_id == f_dark
    r = retrieval.run(p.h.conn, q("a plain white blank page"), search.Filters(), mode="image", engines=eng)
    assert r.results[0].frame_id == f_white
