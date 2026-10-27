"""清理截图、保留期、空间上限（screenrecall.v2.work_frames）：真实 Writer 建的临时库 + 合成截图。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import pytest

from screenrecall.store import delete as store_delete
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import Writer
from screenrecall.v2 import config as v2config
from screenrecall.v2 import work_frames
from screenrecall.v2.work_common import Ctx, FileLock, StopFlag
from screenrecall.v2.work_frames import FRAMES_LOCK, PENDING_FILE, FrameCleanup, local_midnight_ms
from tests._v2_worker_fakes import DIM, L, db, make_cfg, now_ms, put_lines, text_vec

HOUR = 3_600_000
DAY = 86_400_000
OFF = {"text_days": 0, "image_days": 0, "dedupe_frames": False}


def img(seed: int = 0) -> np.ndarray:
    a = np.full((40, 60, 3), 250, np.uint8)
    a[5 + seed % 20:10 + seed % 20, 5:50] = 0
    return a


def frame(w: Writer, sid, b, t, line_ids, *, keep=False, ocr=False, seed=0):
    fid = w.save_frame(img(seed), captured_ms=t, segment_id=sid, block_id=b, pos_y=0, app="a.exe", title="doc",
                       keep=keep, line_ids=line_ids, line_rects=[(0, i * 10, 50, i * 10 + 8) for i in range(len(line_ids))],
                       ocr_rects=[(0, 0, 10, 10)] if ocr else None)
    w.flush()
    return fid


def cleanup(cfg, **retention):
    """跑一次清理（用 cfg 自己的写线程）；返回 (删了几项, 统计)。"""
    with Writer(cfg.data_dir) as w:
        ctx = Ctx(cfg, w, StopFlag())
        try:
            step = FrameCleanup(ctx, v2config.RetentionSettings(**{**OFF, **retention}))
            n = step.run()
            return n, ctx.stat("frames").extra
        finally:
            ctx.close()


def alive(cfg) -> set[str]:
    return {r[0] for r in db(cfg).execute("SELECT frame_id FROM frames")}


def path_of(cfg, fid) -> object:
    return cfg.data_dir / db(cfg).execute("SELECT image_path FROM frames WHERE frame_id = ?", (fid,)).fetchone()[0]


def image_vectors(cfg, frame_nos):
    vs = VectorStore(cfg.data_dir / "vectors" / "image", 512)
    try:
        vs.append(frame_nos, np.eye(512, dtype=np.float32)[:len(frame_nos)], [1] * len(frame_nos), [0] * len(frame_nos))
    finally:
        vs.close()


def image_ids_left(cfg, frame_nos) -> set[int]:
    vs = VectorStore(cfg.data_dir / "vectors" / "image", 512)
    try:
        return {h.id for h in vs.search(np.ones(512, np.float32), k=len(frame_nos), ids=frame_nos) if h.score > 0}
    finally:
        vs.close()


def frame_no(cfg, fid) -> int:
    return int(db(cfg).execute("SELECT frame_no FROM frames WHERE frame_id = ?", (fid,)).fetchone()[0])


# ======================= 多余的旧截图 =======================

def test_old_frame_whose_lines_are_all_in_a_later_frame_is_deleted(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["第一行", "第二行", "第三行"])
        f1 = frame(w, sid, b, t, ids[:2], seed=1)
        f2 = frame(w, sid, b, t + 60_000, ids, seed=2)
    p1 = path_of(cfg, f1)
    image_vectors(cfg, [frame_no(cfg, f1), frame_no(cfg, f2)])
    n1 = frame_no(cfg, f1)
    rev0 = int(db(cfg).execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])
    n, st = cleanup(cfg, dedupe_frames=True)
    assert n == 1 and st["redundant"] == 1 and st["bytes_freed"] > 0
    assert alive(cfg) == {f2} and not p1.exists()
    c = db(cfg)
    assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 3  # 文字不动
    assert {r[0] for r in c.execute("SELECT DISTINCT frame_id FROM line_in_frame")} == {f2}
    assert int(c.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0]) > rev0
    assert image_ids_left(cfg, [n1, frame_no(cfg, f2)]) == {frame_no(cfg, f2)}  # 图片向量一起删
    assert cleanup(cfg, dedupe_frames=True)[0] == 0  # 再跑没有可删的


def test_frames_that_are_still_needed_are_kept(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["甲", "乙", "丙"])
        only_here = frame(w, sid, b, t, [ids[0], ids[1]], seed=1)        # 乙后面没有
        pending = frame(w, sid, b, t + 1000, [ids[0]], ocr=True, seed=2)   # 还在等 OCR
        kept = frame(w, sid, b, t + 2000, [ids[0]], keep=True, seed=3)     # 标了保留
        no_lines = frame(w, sid, b, t + 3000, [], seed=4)                  # 只有图
        young = frame(w, sid, b, now_ms() - 60_000, [ids[0]], seed=5)       # 刚存
        last = frame(w, sid, b, now_ms() - 30_000, [ids[0], ids[2]], seed=6)
    assert cleanup(cfg, dedupe_frames=True)[0] == 0
    assert alive(cfg) == {only_here, pending, kept, no_lines, young, last}
    # 年龄门槛放宽之后，刚存的那张也算多余（甲在更晚的 last 里）
    assert cleanup(cfg, dedupe_frames=True, dedupe_min_age_s=1)[0] == 1
    assert young not in alive(cfg)


def test_every_line_keeps_at_least_one_frame_across_a_chain(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 5 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["A", "B", "C"])
        a, bb, c = ids
        f1 = frame(w, sid, b, t, [a], seed=1)
        f2 = frame(w, sid, b, t + 1000, [a, bb], seed=2)
        f3 = frame(w, sid, b, t + 2000, [bb, c], seed=3)
        f4 = frame(w, sid, b, t + 3000, [c], seed=4)
        # 同一时刻的两张：编号大的算"更晚"
        f5 = frame(w, sid, b, t + 3000, [c], seed=5)
    cleanup(cfg, dedupe_frames=True)
    left = alive(cfg)
    assert left == {f2, f3, f5}
    covered = {r[0] for r in db(cfg).execute("SELECT DISTINCT line_id FROM line_in_frame")}
    assert covered == set(ids)
    assert f1 not in left and f4 not in left


def test_a_crash_after_marking_is_finished_next_time(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["x"])
        f1 = frame(w, sid, b, t, ids, seed=1)
        f2 = frame(w, sid, b, t + 1000, ids, seed=2)
        w.run_raw(work_frames._mark_deleted, [f1]).result(5)  # 标记完就"被杀"了
    p1 = path_of(cfg, f1)
    assert p1.exists()
    n, _ = cleanup(cfg)  # 什么规则都关着，也先把上次没做完的做完
    assert n == 1 and not p1.exists() and alive(cfg) == {f2}


def test_dedupe_runs_inside_the_idle_worker(tmp_path):
    from tests.test_v2_worker import run

    cfg = make_cfg(tmp_path, idle={"clip": False, "summaries": False})
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["一", "二"])
        f1 = frame(w, sid, b, t, ids, seed=1)
        f2 = frame(w, sid, b, t + 1000, ids, seed=2)
    rep = run(cfg)
    assert alive(cfg) == {f2} and rep["steps"]["frames"]["redundant"] == 1
    assert f1 not in alive(cfg)


# ======================= 保留期 =======================

def test_expired_screenshots_go_and_the_text_stays(tmp_path):
    cfg = make_cfg(tmp_path)
    now = now_ms()
    old = local_midnight_ms(now, 30) - HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", old, ["旧的一行"])
        f_old = frame(w, sid, b, old, ids, seed=1)
        f_keep = frame(w, sid, b, old + 1, ids, keep=True, seed=2)
        f_new = frame(w, sid, b, now - HOUR, [], seed=3)
    n, st = cleanup(cfg, image_days=30)
    assert n == 1 and st["expired"] == 1
    assert alive(cfg) == {f_keep, f_new}
    assert db(cfg).execute("SELECT text FROM lines").fetchone()[0] == "旧的一行"


def test_expired_text_is_deleted_like_a_privacy_delete(tmp_path):
    cfg = make_cfg(tmp_path)
    now = now_ms()
    cutoff = local_midnight_ms(now, 90)
    old = cutoff - 2 * HOUR
    old_day = datetime.fromtimestamp(old / 1000).strftime("%Y-%m-%d")
    today = datetime.fromtimestamp(now / 1000).strftime("%Y-%m-%d")
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "old doc", old, ["SECRET-OLD-LINE"])
        frame(w, sid, b, old, ids, seed=1)
        pid = w.piece_put(old, old + 60_000, app="a.exe", title="old doc", segment_ids=[sid]).result(5)
        w.summary_put("piece", str(pid), "旧片段概括", model="m").result(5)
        lo, hi = store_delete.period_span("day", old_day)
        w.summary_put("day", old_day, "旧的一天", model="m", start_ms=lo, end_ms=hi).result(5)
        w.summary_put("day", today, "今天", model="m").result(5)
        w.activity_start(old, hwnd=1, pid=1, process="a.exe", title="old doc").result(5)
        w.activity_end(old + 60_000).result(5)
        sid2, b2, ids2 = put_lines(w, "a.exe", "new doc", now - HOUR, ["新的一行"])
    vs = VectorStore(cfg.data_dir / "vectors" / "text", DIM)
    vs.append([ids[0], ids2[0]], np.stack([text_vec("SECRET"), text_vec("新的")]), [old, now], [sid, sid2])
    vs.close()
    n, st = cleanup(cfg, text_days=90)
    assert n >= 1 and st["text_lines"] == 1
    c = db(cfg)
    assert [r[0] for r in c.execute("SELECT text FROM lines")] == ["新的一行"]
    assert c.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM pieces").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM activity WHERE start_ms < ?", (cutoff,)).fetchone()[0] == 0
    assert [tuple(r) for r in c.execute("SELECT level, key FROM summaries")] == [("day", today)]
    vs = VectorStore(cfg.data_dir / "vectors" / "text", DIM)
    try:
        q = text_vec("SECRET") + text_vec("新的")
        left = {h.id for h in vs.search(q / np.linalg.norm(q), k=5) if h.score > 0}
    finally:
        vs.close()
    assert left == {ids2[0]}
    # 库文件里也不留原文（secure_delete + 检查点）
    from screenrecall.store import schema

    dbp = schema.db_path(cfg.data_dir)
    raw = b"".join(p.read_bytes() for p in (dbp, dbp.with_name(dbp.name + "-wal")) if p.exists())
    assert b"SECRET-OLD-LINE" not in raw
    assert cleanup(cfg, text_days=90)[0] == 0


def test_retention_follows_the_page_settings(tmp_path):
    cfg = make_cfg(tmp_path, retention={"image_days": 0})
    now = now_ms()
    with Writer(cfg.data_dir) as w:
        f_old = w.save_frame(img(), captured_ms=local_midnight_ms(now, 10) - HOUR)
        w.flush()
    with Writer(cfg.data_dir) as w:
        ctx = Ctx(cfg, w, StopFlag())
        try:
            assert FrameCleanup(ctx).run() == 0
            v2config.write_user_settings(cfg.data_dir, {"image_days": 7})
            assert FrameCleanup(ctx).retention.image_days == 7
            assert FrameCleanup(ctx).run() == 1
        finally:
            ctx.close()
    assert f_old not in alive(cfg)


# ======================= 空间上限 =======================

def test_storage_cap_deletes_oldest_unkept_frames_first(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 5 * HOUR
    with Writer(cfg.data_dir) as w:
        fids = [w.save_frame(np.random.default_rng(i).integers(0, 255, (120, 160, 3), dtype=np.uint8),
                             captured_ms=t + i * 1000, keep=(i == 0)) for i in range(6)]
        w.flush()
    c = db(cfg)
    sizes = {r[0]: r[1] for r in c.execute("SELECT frame_id, bytes FROM frames")}
    usage = work_frames.usage_bytes(c, cfg.data_dir)
    # 上限 = 现在的占用减去 1.5 张（不算保留的那张）：要删最旧的两张没标保留的
    cap = usage - int(1.5 * sizes[fids[1]])
    n, st = cleanup(cfg, storage_cap_gb=cap / work_frames.GB)
    assert n == 2 and st["cap"] == 2
    assert alive(cfg) == {fids[0], *fids[3:]}


def test_main_process_cap_check_is_cheap_when_under_the_cap(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    with Writer(cfg.data_dir) as w:
        w.save_frame(img(), captured_ms=now_ms() - HOUR)
        w.flush()
        ctx = Ctx(cfg, w, StopFlag())
        try:
            fc = FrameCleanup(ctx, v2config.RetentionSettings(storage_cap_gb=0))
            assert fc.enforce_cap() == 0  # 不限
            fc = FrameCleanup(ctx, v2config.RetentionSettings(storage_cap_gb=10))
            monkeypatch.setattr(FrameCleanup, "_cap", lambda *a: pytest.fail("没超就不该删"))
            assert fc.enforce_cap() == 0
            monkeypatch.undo()
            fc = FrameCleanup(ctx, v2config.RetentionSettings(storage_cap_gb=1e-9))
            assert fc.enforce_cap() == 1
        finally:
            ctx.close()
    assert alive(cfg) == set()


# ======================= 并发与补删 =======================

def test_skips_while_another_process_holds_the_lock(tmp_path):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["x"])
        frame(w, sid, b, t, ids, seed=1)
        frame(w, sid, b, t + 1000, ids, seed=2)
    lock = FileLock(cfg.data_dir / FRAMES_LOCK)
    assert lock.acquire()
    try:
        assert cleanup(cfg, dedupe_frames=True)[0] == 0
    finally:
        lock.release()
    assert cleanup(cfg, dedupe_frames=True)[0] == 1


def test_vectors_that_cannot_be_deleted_now_are_retried(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    t = now_ms() - 3 * HOUR
    with Writer(cfg.data_dir) as w:
        sid, b, ids = put_lines(w, "a.exe", "doc", t, ["x"])
        f1 = frame(w, sid, b, t, ids, seed=1)
        f2 = frame(w, sid, b, t + 1000, ids, seed=2)
    n1, n2 = frame_no(cfg, f1), frame_no(cfg, f2)
    image_vectors(cfg, [n1, n2])
    real = VectorStore.delete

    def busy(self, ids):
        raise TimeoutError("向量库被占用")

    monkeypatch.setattr(VectorStore, "delete", busy)
    assert cleanup(cfg, dedupe_frames=True)[0] == 1
    pend = json.loads((cfg.data_dir / PENDING_FILE).read_text(encoding="utf-8"))
    assert pend == {"text": [], "image": [n1]}  # 只有编号
    monkeypatch.setattr(VectorStore, "delete", real)
    cleanup(cfg)
    assert not (cfg.data_dir / PENDING_FILE).exists()
    assert image_ids_left(cfg, [n1, n2]) == {n2}


def test_local_midnight():
    now = int(datetime(2026, 10, 27, 15, 30).timestamp() * 1000)
    assert local_midnight_ms(now, 0) == int(datetime(2026, 10, 27).timestamp() * 1000)
    assert local_midnight_ms(now, 30) == int((datetime(2026, 10, 27) - timedelta(days=30)).timestamp() * 1000)
