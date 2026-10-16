"""收藏、删除、到期与配额（方案 5.2、5.4；A11、A12 的可在 Mac 上验证部分）。

数据由真实 Recorder（假窗口）、替身 OCR 与替身编码器生成，真实 SQLite / WebP 文件。
这些是逻辑验证，不等于 Windows 实机验收；配额的磁盘占用用注入的测量函数模拟。
"""

import logging
import threading

import pytest

from screenrecall import db, lifecycle, retrieval, search
from screenrecall.embed import EmbedWorker
from screenrecall.lifecycle import DAY_MS, RetentionConfig
from screenrecall.ocr import OcrWorker

from .conftest import make_image
from .test_retrieval import FakeImage, FakeText, Pipeline, box, q


def _ids(resp):
    return [r.frame_id for r in resp.results]


def _occ(h, fid):
    return [r["occurrence_id"] for r in h.conn.execute(
        "SELECT occurrence_id FROM occurrences WHERE frame_id = ? ORDER BY first_seen_ms, rowid", (fid,))]


def _frame(h, fid):
    return h.conn.execute("SELECT * FROM frames WHERE frame_id = ?", (fid,)).fetchone()


def _file(h, fid):
    return h.cfg.data_dir / _frame(h, fid)["image_path"]


def _count(h, sql, *args):
    return h.conn.execute(sql, args).fetchone()[0]


def _last_seen(h, fid):
    return _count(h, "SELECT MAX(last_seen_ms) FROM occurrences WHERE frame_id = ?", fid)


def _set_age(h, occurrence_id, last_seen_ms):
    h.conn.execute("UPDATE occurrences SET first_seen_ms = ?, last_seen_ms = ? WHERE occurrence_id = ?",
                   (last_seen_ms, last_seen_ms, occurrence_id))


def _ocr_only(h, img, lines):
    """采集一张新画面并只跑 OCR（向量待办保留）。"""
    h.show(img)
    if h.rec.recording:
        h.rec.snapshot(h.conn, None)
    else:
        h.start()
        h.rec.sample(h.conn)
    fid = h.conn.execute("SELECT frame_id FROM frames ORDER BY rowid DESC LIMIT 1").fetchone()[0]

    class E:
        version = "fake"

        def recognize(self, image):
            return [box(t, 12 * i) for i, t in enumerate(lines)]

    w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    w._process(h.conn, w._claim(h.conn))
    return fid


RT = RetentionConfig()


# ---------- 删除本次记录 / 共享画面 ----------

def test_delete_one_occurrence_keeps_shared_frame_until_last_reference(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["shared invoice text"])
    h.rec.snapshot(h.conn, None)  # 同一画面再记一次出现（复用图片）
    o1, o2 = _occ(h, fid)
    assert _count(h, "SELECT COUNT(*) FROM frames") == 1

    res = lifecycle.delete_occurrence(h.conn, h.cfg.data_dir, o1)
    assert (res.occurrences_removed, res.frames_purged) == (1, 0)
    assert _frame(h, fid)["image_status"] == "ok" and _file(h, fid).exists()
    hits = search.search(h.conn, q('"invoice"'))
    assert _ids(hits) == [fid] and hits.results[0].latest.occurrence_id == o2  # 另一次出现不受影响

    path = _file(h, fid)
    res = lifecycle.delete_occurrence(h.conn, h.cfg.data_dir, o2)
    assert (res.frames_purged, res.files_removed) == (1, 1)
    assert not path.exists()
    f = _frame(h, fid)
    assert f["image_status"] == "deleted" and f["record_version"] == 2
    for table in ("frame_text", "ocr_lines", "frame_blocks"):
        assert _count(h, f"SELECT COUNT(*) FROM {table} WHERE frame_id = ?", fid) == 0
    assert _count(h, "SELECT COUNT(*) FROM ocr_fts") == 0
    assert _count(h, "SELECT COUNT(*) FROM embeddings") == 0  # 整帧向量与不再被引用的块向量
    assert _count(h, "SELECT COUNT(*) FROM text_blocks") == 0
    assert _ids(search.search(h.conn, q('"invoice"'))) == []


def test_shared_text_block_survives_until_last_frame_is_deleted(h):
    p = Pipeline(h)
    f1 = p.add(make_image(1), ["quarterly budget review"])
    f2 = p.add(make_image(2), ["quarterly budget review"])  # 不同画面、相同文字 → 共享文本块
    assert _count(h, "SELECT COUNT(*) FROM text_blocks") == 1
    eng = p.engines()
    before = _ids(retrieval.run(h.conn, q("budget review"), search.Filters(), mode=retrieval.TEXT, engines=eng))
    assert set(before) == {f1, f2}

    plan = lifecycle.plan_frame_delete(h.conn, f1)
    lifecycle.delete_frame(h.conn, h.cfg.data_dir, f1, confirmed_favorites=plan.favorites)
    assert _count(h, "SELECT COUNT(*) FROM text_blocks") == 1
    assert _count(h, "SELECT COUNT(*) FROM embeddings WHERE target_kind = 'block'") == 1
    after = retrieval.run(h.conn, q("budget review"), search.Filters(), mode=retrieval.TEXT, engines=eng)
    assert _ids(after) == [f2] and "语义" in after.results[0].sources  # 共享块向量仍为 f2 服务

    lifecycle.delete_frame(h.conn, h.cfg.data_dir, f2, confirmed_favorites=frozenset())
    assert _count(h, "SELECT COUNT(*) FROM text_blocks") == 0
    assert _count(h, "SELECT COUNT(*) FROM embeddings") == 0


# ---------- 删除此画面的全部记录：先说明、确认后执行 ----------

def test_delete_frame_plan_lists_favorites_and_refuses_unshown_favorite(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["contract draft"])
    h.rec.snapshot(h.conn, None)
    o1, o2 = _occ(h, fid)
    assert lifecycle.add_favorite(h.conn, o1)

    plan = lifecycle.plan_frame_delete(h.conn, fid)
    assert [o["occurrence_id"] for o in plan.occurrences] == [o1, o2]
    assert plan.favorites == {o1}

    with pytest.raises(lifecycle.PlanChanged):  # 页面没展示这个收藏就不能删
        lifecycle.delete_frame(h.conn, h.cfg.data_dir, fid, confirmed_favorites=frozenset())
    assert _frame(h, fid)["image_status"] == "ok" and len(_occ(h, fid)) == 2

    res = lifecycle.delete_frame(h.conn, h.cfg.data_dir, fid, confirmed_favorites=plan.favorites)
    assert (res.occurrences_removed, res.favorites_removed, res.frames_purged) == (2, 1, 1)
    assert _count(h, "SELECT COUNT(*) FROM favorites") == 0
    assert lifecycle.plan_frame_delete(h.conn, fid) is None


def test_delete_favorited_occurrence_removes_only_its_own_favorite(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["receipt 42"])
    h.rec.snapshot(h.conn, None)
    o1, o2 = _occ(h, fid)
    lifecycle.add_favorite(h.conn, o1)
    lifecycle.add_favorite(h.conn, o2)
    res = lifecycle.delete_occurrence(h.conn, h.cfg.data_dir, o1)
    assert (res.favorites_removed, res.frames_purged) == (1, 0)
    assert lifecycle.favorite_ids(h.conn, [o1, o2]) == {o2}


def test_favorite_foreign_key_blocks_raw_occurrence_delete(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["x"])
    lifecycle.add_favorite(h.conn, _occ(h, fid)[0])
    with pytest.raises(Exception):
        h.conn.execute("DELETE FROM occurrences WHERE frame_id = ?", (fid,))


# ---------- A11 到期：30 天原图 / 90 天记录，收藏保护 ----------

def test_a11_expiry_image_30_days_text_90_days_favorite_protected(h):
    p = Pipeline(h)
    fav = p.add(make_image(1), ["favorite error 0x80070005"])
    plain = p.add(make_image(2), ["plain error 0x80070005"])
    lifecycle.add_favorite(h.conn, _occ(h, fav)[0])
    t = max(_last_seen(h, fav), _last_seen(h, plain))
    eng = p.engines()

    out = lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + 29 * DAY_MS)
    assert out.images_expired == 0

    plain_file = _file(h, plain)
    out = lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + 31 * DAY_MS)
    assert out.images_expired == 1 and not plain_file.exists()
    assert _frame(h, plain)["image_status"] == "expired" and _frame(h, fav)["image_status"] == "ok"
    hits = search.search(h.conn, q('"0x80070005"'))
    assert set(_ids(hits)) == {fav, plain}  # 原图过期后仍可查文字
    assert {r.frame_id: r.image_status for r in hits.results}[plain] == "expired"
    img = retrieval.run(h.conn, q("bright"), search.Filters(), mode=retrieval.IMAGE, engines=eng)
    assert plain not in _ids(img)  # 画面模式只搜原图仍可用的记录

    out = lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + 91 * DAY_MS)
    assert (out.occurrences_removed, out.frames_purged) == (1, 1)
    assert _ids(search.search(h.conn, q('"0x80070005"'))) == [fav]
    assert _frame(h, fav)["image_status"] == "ok" and _file(h, fav).exists()

    lifecycle.remove_favorite(h.conn, _occ(h, fav)[0])  # 取消收藏后按普通规则处理
    lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + 91 * DAY_MS)
    assert _frame(h, fav)["image_status"] == "deleted"


def test_a11_image_kept_while_any_occurrence_is_recent(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["shared page"])
    h.rec.snapshot(h.conn, None)
    o_old, o_new = _occ(h, fid)
    t = _last_seen(h, fid)
    _set_age(h, o_old, t - 60 * DAY_MS)
    out = lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + DAY_MS)
    assert out.images_expired == 0 and out.occurrences_removed == 0
    _set_age(h, o_old, t - 100 * DAY_MS)  # 旧记录超过 90 天：删记录，画面仍被新记录引用
    out = lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + DAY_MS)
    assert (out.occurrences_removed, out.frames_purged) == (1, 0)
    assert _occ(h, fid) == [o_new] and _frame(h, fid)["image_status"] == "ok"


def test_unprocessed_frames_are_not_expired_or_evicted(h):
    h.show(make_image(1))
    h.start()
    h.rec.sample(h.conn)  # OCR 待办未处理
    fid = h.frames()[0]["frame_id"]
    t = _last_seen(h, fid)
    lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + 200 * DAY_MS)
    assert _frame(h, fid)["image_status"] == "ok" and len(_occ(h, fid)) == 1
    tiny = RetentionConfig(quota_bytes=1, min_free_bytes=0, reserve_bytes=0)
    res = lifecycle.enforce_quota(h.conn, h.cfg.data_dir, tiny, t, free_fn=lambda d: 10**12, usage_fn=lambda d: 10)
    assert res.paused and _frame(h, fid)["image_status"] == "ok" and len(_occ(h, fid)) == 1


# ---------- A12 配额 ----------

def _frames_usage(h):
    return lambda d: sum(f.stat().st_size for f in (d / "frames").rglob("*.webp"))


def test_a12_quota_evicts_oldest_non_favorite_images_first(h):
    p = Pipeline(h)
    f_old = p.add(make_image(1), ["oldest"])
    f_mid = p.add(make_image(2), ["middle"])
    f_new = p.add(make_image(3), ["newest"])
    lifecycle.add_favorite(h.conn, _occ(h, f_old)[0])  # 最旧的是收藏，必须跳过
    usage = _frames_usage(h)
    total = usage(h.cfg.data_dir)
    cfg = RetentionConfig(quota_bytes=total - 1, min_free_bytes=0, reserve_bytes=0)
    res = lifecycle.enforce_quota(h.conn, h.cfg.data_dir, cfg, _last_seen(h, f_new),
                                  free_fn=lambda d: 10**12, usage_fn=usage)
    assert not res.paused and res.needed_bytes == 0
    assert res.steps[:3] == ["清理到期数据", "清理可重建缓存", "提前清理最旧的非收藏原图"]
    assert res.images_expired_early == 1
    status = {f: _frame(h, f)["image_status"] for f in (f_old, f_mid, f_new)}
    assert status == {f_old: "ok", f_mid: "expired", f_new: "ok"}
    assert _ids(search.search(h.conn, q('"middle"'))) == [f_mid]  # 文字仍在


def test_a12_quota_pauses_when_remaining_data_is_protected(h):
    p = Pipeline(h)
    f1 = p.add(make_image(1), ["one"])
    f2 = p.add(make_image(2), ["two"])
    f3 = p.add(make_image(3), ["three"])
    lifecycle.add_favorite(h.conn, _occ(h, f3)[0])
    (h.cfg.data_dir / "cache" / "thumbs").mkdir(parents=True)
    (h.cfg.data_dir / "cache" / "thumbs" / "x.bin").write_bytes(b"0" * 10)
    (h.cfg.data_dir / "cache" / "orphans").mkdir(parents=True)
    (h.cfg.data_dir / "cache" / "orphans" / "keep.webp").write_bytes(b"0")
    cfg = RetentionConfig(quota_bytes=1, min_free_bytes=0, reserve_bytes=0, batch=1)
    res = lifecycle.enforce_quota(h.conn, h.cfg.data_dir, cfg, _last_seen(h, f3),
                                  free_fn=lambda d: 10**12, usage_fn=lambda d: 1000)  # 删除记录不缩小文件
    assert res.paused and "收藏" in res.reason and "已暂停采集" in res.reason
    assert not (h.cfg.data_dir / "cache" / "thumbs").exists()
    assert (h.cfg.data_dir / "cache" / "orphans" / "keep.webp").exists()  # 未登记图片不是可重建缓存
    assert _frame(h, f3)["image_status"] == "ok"
    # 原图先于记录清理；记录清理没有减少实际占用：删了一批（最旧的 f1）就停，不把历史删光
    assert res.images_expired_early == 2
    assert (_frame(h, f1)["image_status"], _frame(h, f2)["image_status"]) == ("deleted", "expired")
    assert res.occurrences_removed_early == 1
    assert _count(h, "SELECT COUNT(*) FROM occurrences") == 2


def test_a12_low_free_disk_also_triggers_cleanup(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["disk"])
    cfg = RetentionConfig(quota_bytes=10**12, min_free_bytes=100, reserve_bytes=0)
    res = lifecycle.enforce_quota(h.conn, h.cfg.data_dir, cfg, _last_seen(h, fid),
                                  free_fn=lambda d: 50, usage_fn=lambda d: 0)
    assert res.paused and res.images_expired_early == 1


def test_maintenance_report_is_stored_for_the_page(h):
    Pipeline(h).add(make_image(1), ["a"])
    rep = lifecycle.run_maintenance(h.conn, h.cfg.data_dir, RT, free_fn=lambda d: 10**12)
    assert lifecycle.last_maintenance(h.conn) == rep and rep["paused"] is False


def test_recorder_storage_pause_refuses_start_until_cleared(h):
    h.show(make_image(1))
    h.start()
    h.rec.storage_pause(h.conn, "存储空间不足，已暂停采集")
    ctrl = h.ctrl()
    assert ctrl["actual_state"] == "paused" and "存储空间不足" in ctrl["state_reason"]
    assert h.conn.execute("SELECT end_reason FROM sessions").fetchone()[0] == "storage_full"
    h.start()
    assert h.ctrl()["actual_state"] == "paused"
    h.rec.storage_clear()
    h.start()
    assert h.ctrl()["actual_state"] == "recording"


# ---------- A10 补充：删除期间后台任务写回 ----------

def test_ocr_result_written_after_delete_is_discarded(h):
    h.show(make_image(1))
    h.start()
    h.rec.sample(h.conn)
    fid = h.frames()[0]["frame_id"]

    class E:
        version = "fake"

        def recognize(self, image):
            # 推理进行中，用户删除了这条记录（图片文件已删除前，worker 已读入内存）
            lifecycle.delete_occurrence(h.conn, h.cfg.data_dir, _occ(h, fid)[0])
            return [box("secret token")]

    w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    w._process(h.conn, w._claim(h.conn))
    assert _count(h, "SELECT COUNT(*) FROM frame_text") == 0
    assert _count(h, "SELECT COUNT(*) FROM ocr_fts") == 0
    assert _count(h, "SELECT COUNT(*) FROM index_jobs WHERE stage != 'ocr'") == 0  # 没有登记向量待办
    assert _count(h, "SELECT status FROM index_jobs WHERE frame_id = ?", fid) == "cancelled"
    assert _ids(search.search(h.conn, q('"secret"'))) == []


def test_embed_results_written_after_delete_are_discarded(h):
    fid = _ocr_only(h, make_image(1), ["unique sentence about tax"])

    class DeletingText(FakeText):
        def encode(self, texts):
            out = super().encode(texts)
            plan = lifecycle.plan_frame_delete(h.conn, fid)
            lifecycle.delete_frame(h.conn, h.cfg.data_dir, fid, confirmed_favorites=plan.favorites)
            return out

    w = EmbedWorker(h.cfg, threading.Event(), threading.Event(), logging.getLogger("t"),
                    text_encoder_factory=DeletingText, image_encoder_factory=FakeImage)
    while w.run_once(h.conn):
        pass
    assert _count(h, "SELECT COUNT(*) FROM frame_blocks") == 0
    assert _count(h, "SELECT COUNT(*) FROM text_blocks") == 0
    assert _count(h, "SELECT COUNT(*) FROM embeddings") == 0  # 先写回的整帧向量也随删除回收
    assert _count(h, "SELECT COUNT(*) FROM index_jobs WHERE status IN ('pending', 'running')") == 0


def test_image_embed_after_expiry_is_discarded(h):
    fid = _ocr_only(h, make_image(1), ["chart"])
    t = _last_seen(h, fid)

    class ExpiringImage(FakeImage):
        def encode_images(self, images):
            lifecycle.expire(h.conn, h.cfg.data_dir, RT, t + 31 * DAY_MS)
            return super().encode_images(images)

    w = EmbedWorker(h.cfg, threading.Event(), threading.Event(), logging.getLogger("t"),
                    text_encoder_factory=FakeText, image_encoder_factory=ExpiringImage)
    while w.run_once(h.conn):
        pass
    assert _frame(h, fid)["image_status"] == "expired"
    assert _count(h, "SELECT COUNT(*) FROM embeddings WHERE target_kind = 'frame'") == 0


# ---------- 缓存刷新 ----------

def test_vector_matrices_refresh_after_delete(h):
    p = Pipeline(h)
    keep = p.add(make_image(1), ["travel plan tokyo"])
    gone = p.add(make_image(2), ["travel plan osaka"])
    eng = p.engines()
    text = retrieval.run(h.conn, q("travel plan"), search.Filters(), mode=retrieval.TEXT, engines=eng)
    assert set(_ids(text)) == {keep, gone}
    retrieval.run(h.conn, q("bright"), search.Filters(), mode=retrieval.IMAGE, engines=eng)
    rev = eng.text_index.revision
    assert gone in eng.text_index.frames_with_vectors and gone in eng.image_index.frame_ids

    lifecycle.delete_occurrence(h.conn, h.cfg.data_dir, _occ(h, gone)[0])
    assert db.get_revision(h.conn) > rev
    text = retrieval.run(h.conn, q("travel plan"), search.Filters(), mode=retrieval.TEXT, engines=eng)
    assert _ids(text) == [keep]
    assert gone not in eng.text_index.frames_with_vectors  # 矩阵已按修订号重载
    retrieval.run(h.conn, q("bright"), search.Filters(), mode=retrieval.IMAGE, engines=eng)
    assert gone not in eng.image_index.frame_ids


# ---------- 采集线程与删除 ----------

def test_recorder_does_not_extend_or_resave_deleted_current_record(h):
    a, b = make_image(1), make_image(2)
    h.show(a)
    h.start()
    h.rec.sample(h.conn)
    fid = h.frames()[0]["frame_id"]
    lifecycle.delete_occurrence(h.conn, h.cfg.data_dir, _occ(h, fid)[0])
    for _ in range(3):
        h.tick()  # 画面没变：不延长已删记录，也不立刻重新存入
    assert _count(h, "SELECT COUNT(*) FROM occurrences") == 0
    assert _count(h, "SELECT COUNT(*) FROM frames WHERE image_status = 'ok'") == 0
    h.show(b)
    for _ in range(3):
        h.tick()
    assert _count(h, "SELECT COUNT(*) FROM frames WHERE image_status = 'ok'") == 1  # 画面变化后照常记录
    h.show(a)
    h.rec.snapshot(h.conn, None)  # 用户主动补拍：按新内容保存
    assert _count(h, "SELECT COUNT(*) FROM frames WHERE image_status = 'ok'") == 2
