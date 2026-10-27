"""v2 存储：表结构、连接与单写线程（行匹配、消失、替换、跨行 tail、证据帧、OCR 区域、待办、控制）。"""

import sqlite3
import threading
import time
from concurrent.futures import Future

import cv2
import numpy as np
import pytest

from screenrecall.store import schema
from screenrecall.store.writer import LineIn, Writer, WriterConfig, frame_rel_path


class Clock:
    def __init__(self, t=1_700_000_000_000):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def w(tmp_path):
    wr = Writer(tmp_path, clock=Clock())
    yield wr
    wr.close()


def L(text, y, h=20, x0=10, x1=500, source="uia", conf=1.0):
    return LineIn(text, x0, y, x1, y + h, source, conf)


def ro(w):
    return schema.connect_ro(w.db_path)


def lines_of(conn, block):
    return conn.execute("SELECT * FROM lines WHERE block_id = ? ORDER BY order_key, line_id", (block,)).fetchall()


# ---------------- 表结构与连接 ----------------

def test_connect_pragmas_and_schema(tmp_path):
    c = schema.connect(tmp_path / schema.DB_NAME)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert c.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    for t in ("activity", "gaps", "segments", "blocks", "lines", "lines_tri", "lines_bi", "frames", "line_in_frame",
              "ocr_regions", "skims", "pieces", "piece_segments", "summaries", "jobs", "control"):
        assert t in names
    assert c.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == "1"
    c.close()
    # 重复打开不重建
    schema.connect(tmp_path / schema.DB_NAME).close()


def test_connect_ro_creates_missing_db_and_is_read_only(tmp_path):
    p = tmp_path / schema.DB_NAME
    assert not p.exists()
    c = schema.connect_ro(p)  # 录制进程从没运行过
    assert c.execute("SELECT desired_state FROM control").fetchone()[0] == "paused"
    with pytest.raises(sqlite3.OperationalError):
        c.execute("UPDATE control SET desired_state = 'recording'")
    c.close()


def test_connect_ro_after_all_writers_closed(tmp_path):
    p = tmp_path / schema.DB_NAME
    rw = schema.connect(p)
    rw.execute("INSERT INTO segments (app, title, first_seen_ms, last_seen_ms) VALUES ('a', 'b', 1, 2)")
    rw.close()
    c = schema.connect_ro(p)
    assert c.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    c.close()


def test_newer_schema_and_fts_version_rejected(tmp_path):
    p = tmp_path / schema.DB_NAME
    c = schema.connect(p)
    c.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    c.close()
    with pytest.raises(schema.SchemaError):
        schema.connect(p)
    c = schema.connect(p, migrate=False)
    c.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    c.execute("UPDATE meta SET value = 'f0' WHERE key = 'fts_version'")
    c.close()
    with pytest.raises(schema.SchemaError):
        schema.connect(p)


# ---------------- 写线程 ----------------

def test_results_visible_to_other_connections_when_future_done(w):
    f = w.segment_for("Code.exe", "a.py", 1000)
    sid = f.result(5)
    c = ro(w)
    assert c.execute("SELECT app FROM segments WHERE segment_id = ?", (sid,)).fetchone()[0] == "Code.exe"
    c.close()


def test_submit_never_waits_for_db(w):
    # 另一个连接占着写锁：排队仍立即返回
    other = schema.connect(w.db_path)
    other.execute("BEGIN IMMEDIATE")
    t0 = time.perf_counter()
    futs = [w.segment_for("a", f"t{i}", 1000 + i) for i in range(500)]
    assert time.perf_counter() - t0 < 0.5
    assert not futs[-1].done()
    other.execute("COMMIT")
    other.close()
    assert len({f.result(10) for f in futs}) == 500


def test_failed_op_does_not_break_batch(w):
    sid = w.segment_for("a", "t", 1).result(5)
    bad = w.record_lines(999_999, 1, (0, 100), [L("x", 0)])
    good = w.block_new(sid, 1)
    with pytest.raises(ValueError):
        bad.result(5)
    assert good.result(5) > 0


def test_activity_start_closes_previous(w):
    a = w.activity_start(1000, hwnd=1, pid=2, process="a.exe", title="A")
    b = w.activity_start(5000, hwnd=3, pid=4, process="b.exe", title="B", excluded=True)
    w.activity_end(9000).result(5)
    c = ro(w)
    rows = c.execute("SELECT activity_id, start_ms, end_ms, excluded FROM activity ORDER BY start_ms").fetchall()
    assert [tuple(r) for r in rows] == [(a.result(), 1000, 5000, 0), (b.result(), 5000, 9000, 1)]
    c.close()


def test_gaps(w):
    g = w.gap_open(100, "locked", detail="锁屏")
    w.gap_close(g, 900)
    w.gap(1000, 2000, "minimised", process="x.exe").result(5)
    with pytest.raises(ValueError):
        w.gap(1, 2, "nope")
    c = ro(w)
    assert [tuple(r) for r in c.execute("SELECT start_ms, end_ms, reason FROM gaps ORDER BY start_ms")] == [
        (100, 900, "locked"), (1000, 2000, "minimised")]
    c.close()


def test_segment_reuse_window(w):
    s1 = w.segment_for("chrome.exe", "Doc", 0).result(5)
    s2 = w.segment_for("chrome.exe", "Doc", 29 * 60_000).result(5)
    s3 = w.segment_for("chrome.exe", "Doc", 29 * 60_000 + 31 * 60_000).result(5)
    s4 = w.segment_for("chrome.exe", "Other", 29 * 60_000).result(5)
    assert s1 == s2 and s3 != s1 and s4 not in (s1, s3)


def test_block_merge_and_cycles(w):
    sid = w.segment_for("a", "t", 1).result(5)
    b1, b2, b3 = (w.block_new(sid, 1).result(5) for _ in range(3))
    w.block_anchor(b1, "top").result(5)
    w.block_merge(b2, b1, 500).result(5)
    w.block_merge(b3, b2, 300).result(5)
    with pytest.raises(ValueError):
        w.block_merge(b1, b3, 0).result(5)
    with pytest.raises(ValueError):
        w.block_anchor(b1, "middle")
    from screenrecall.store.writer import block_root
    c = ro(w)
    assert block_root(c, b3) == (b1, 800)
    assert c.execute("SELECT anchor FROM blocks WHERE block_id = ?", (b1,)).fetchone()[0] == "top"
    c.close()


def _block(w, t=1000):
    sid = w.segment_for("app.exe", "title", t).result(5)
    return sid, w.block_new(sid, t).result(5)


def test_record_lines_match_insert_disappear_replace(w):
    _, b = _block(w)
    ids1 = w.record_lines(b, 1000, (0, 300), [L("Hello  World", 10), L("第二行", 40), L("会被改写", 70),
                                              L("   ", 100)]).result(5)
    assert ids1[3] is None and len(set(ids1[:3])) == 3
    # 同一行：大小写 / 空白 / 全角不同、y 偏 6 px → 仍是同一行；改写的一行消失并被替换
    ids2 = w.record_lines(b, 2000, (0, 300), [L("hello world", 16), L("第 二 行", 40), L("已经改写", 72)]).result(5)
    assert ids2[0] == ids1[0] and ids2[1] == ids1[1] and ids2[2] not in ids1
    c = ro(w)
    rows = {r["line_id"]: r for r in lines_of(c, b)}
    assert rows[ids1[0]]["first_seen_ms"] == 1000 and rows[ids1[0]]["last_seen_ms"] == 2000
    assert rows[ids1[0]]["text"] == "Hello  World"  # 显示用原文保持第一次的写法
    assert rows[ids1[2]]["disappeared_ms"] == 2000
    assert rows[ids2[2]]["replaces_line_id"] == ids1[2]
    # y 偏太多 → 新行
    ids3 = w.record_lines(b, 3000, (0, 300), [L("hello world", 40)]).result(5)
    assert ids3[0] != ids1[0]
    c.close()


def test_lines_outside_or_partly_outside_viewport_are_kept(w):
    _, b = _block(w)
    ids = w.record_lines(b, 1000, (0, 1000), [L("top", 10), L("edge", 290), L("below", 500)]).result(5)
    # 滚动后视口 300..600：只有 "below" 在视口内，没出现 → 消失；"edge" 跨视口边界不判消失
    w.record_lines(b, 2000, (300, 600), [L("new", 400)]).result(5)
    c = ro(w)
    d = {r["line_id"]: r["disappeared_ms"] for r in lines_of(c, b)}
    assert d[ids[0]] is None and d[ids[1]] is None and d[ids[2]] == 2000
    c.close()


def test_late_ocr_does_not_mark_newer_lines_disappeared(w):
    _, b = _block(w)
    ids = w.record_lines(b, 5000, (0, 300), [L("uia line", 10)]).result(5)
    # 空闲 OCR 处理一张 t=3000 的旧帧：那时这一行还没出现，不能因此记"消失"
    ocr = w.record_lines(b, 3000, (0, 300), [L("older text", 50, source="ocr", conf=0.9)]).result(5)
    c = ro(w)
    rows = {r["line_id"]: r for r in lines_of(c, b)}
    assert rows[ids[0]]["disappeared_ms"] is None
    assert rows[ocr[0]]["source"] == "ocr" and rows[ocr[0]]["confidence"] == pytest.approx(0.9)
    # 匹配到已有行时 first_seen 往前挪
    w.record_lines(b, 2000, (0, 300), [L("uia line", 12, source="ocr")]).result(5)
    assert c.execute("SELECT first_seen_ms FROM lines WHERE line_id = ?", (ids[0],)).fetchone()[0] == 2000
    c.close()


def test_duplicate_texts_match_nearest(w):
    _, b = _block(w)
    ids = w.record_lines(b, 1000, (0, 300), [L("OK", 10), L("OK", 100)]).result(5)
    again = w.record_lines(b, 2000, (0, 300), [L("OK", 102), L("OK", 12)]).result(5)
    assert again == [ids[1], ids[0]]


def test_tails_link_next_line_and_update_predecessor(w):
    _, b = _block(w)
    a, = w.record_lines(b, 1000, (0, 1000), [L("first line ends with mach", 10)]).result(5)
    # 下面一行之后才出现（滚动露出），上一行的 tail 要补上
    nb, = w.record_lines(b, 2000, (0, 1000), [L("first line ends with mach", 10), L("ine learning is fun", 40)]).result(5)[1:]
    c = ro(w)
    r = c.execute("SELECT tail, tail_line_id FROM lines WHERE line_id = ?", (a,)).fetchone()
    assert r["tail"] == "inelearningisfun"[:16] and r["tail_line_id"] == nb
    # 中间插入一行：上一行改指向新行，新行指向原来的下一行
    mid = w.record_lines(b, 3000, (0, 1000), [L("first line ends with mach", 10), L("插入", 25),
                                              L("ine learning is fun", 40)]).result(5)[1]
    assert c.execute("SELECT tail_line_id FROM lines WHERE line_id = ?", (a,)).fetchone()[0] == mid
    assert c.execute("SELECT tail, tail_line_id FROM lines WHERE line_id = ?", (mid,)).fetchone()[:] == (
        "inelearningisfun", nb)
    # 全文索引与 tail 同步：旧文档已删，新文档可查
    n = c.execute("SELECT COUNT(*) FROM lines_tri WHERE lines_tri MATCH '\"hin\" AND \"ine\"'").fetchone()[0]
    assert n == 0
    c.close()


def test_save_frame_lossless_with_links_and_ocr_regions(w, tmp_path):
    sid, b = _block(w)
    img = np.random.default_rng(1).integers(0, 255, (60, 80, 4), dtype=np.uint8)
    ids = w.record_lines(b, 1000, (0, 300), [L("abc", 10), L("def", 40)])
    fid = w.save_frame(img, captured_ms=1000, segment_id=sid, block_id=b, pos_y=0, hwnd=5, app="app.exe",
                       title="title", line_ids=ids, line_rects=[(1, 2, 3, 4), (5, 6, 7, 8)],
                       ocr_rects=[(0, 0, 80, 30)])
    img[:] = 0  # 调用方之后改写原数组不影响保存的图
    w.flush()
    c = ro(w)
    f = c.execute("SELECT * FROM frames WHERE frame_id = ?", (fid,)).fetchone()
    assert f["status"] == "ok" and f["width"] == 80 and f["height"] == 60 and f["frame_no"] == 1
    assert f["image_path"] == frame_rel_path(fid, 1000) and f["image_path"].startswith("frames/")
    dec = cv2.imread(str(tmp_path / f["image_path"]), cv2.IMREAD_COLOR)
    expect = np.random.default_rng(1).integers(0, 255, (60, 80, 4), dtype=np.uint8)[..., :3]
    assert np.array_equal(dec, expect)
    assert c.execute("SELECT COUNT(*) FROM line_in_frame WHERE frame_id = ?", (fid,)).fetchone()[0] == 2
    r = c.execute("SELECT * FROM ocr_regions WHERE frame_id = ?", (fid,)).fetchone()
    assert r["status"] == "pending" and r["block_id"] == b and (r["x1"], r["y1"]) == (80, 30)
    c.close()


def test_ocr_claim_complete_retry(tmp_path):
    clock = Clock(10_000)
    w = Writer(tmp_path, WriterConfig(ocr_max_attempts=2, claim_stale_ms=1000), clock=clock)
    try:
        sid, b = _block(w)
        fid = w.save_frame(np.zeros((10, 10, 3), np.uint8), captured_ms=1, segment_id=sid, block_id=b)
        rid = w.ocr_region_add(fid, (0, 0, 10, 10), block_id=b, pos_y=0).result(5)
        w.flush()
        got = w.ocr_claim(5).result(5)
        assert [g["region_id"] for g in got] == [rid] and got[0]["image_abs_path"].endswith(".webp")
        assert w.ocr_claim(5).result(5) == []  # 已被领取
        w.ocr_complete(rid, ok=False, error="boom").result(5)
        assert [g["region_id"] for g in w.ocr_claim(5).result(5)] == [rid]  # 第 2 次
        w.ocr_complete(rid, ok=False, error="boom").result(5)
        c = ro(w)
        assert c.execute("SELECT status FROM ocr_regions WHERE region_id = ?", (rid,)).fetchone()[0] == "failed"
        # 领取后进程退出：超时可重新领取
        rid2 = w.ocr_region_add(fid, (0, 0, 5, 5)).result(5)
        assert [g["region_id"] for g in w.ocr_claim(5).result(5)] == [rid2]
        clock.t += 2000
        assert [g["region_id"] for g in w.ocr_claim(5).result(5)] == [rid2]
        w.ocr_complete(rid2).result(5)
        assert c.execute("SELECT status FROM ocr_regions WHERE region_id = ?", (rid2,)).fetchone()[0] == "done"
        c.close()
    finally:
        w.close()


def test_jobs_idempotent_claim_fail_done(tmp_path):
    clock = Clock(0)
    w = Writer(tmp_path, clock=clock)
    try:
        j = w.job_add("summary", "piece:1").result(5)
        assert w.job_add("summary", "piece:1").result(5) == j
        got = w.job_claim(["summary"]).result(5)
        assert got == [{"job_id": j, "kind": "summary", "target": "piece:1", "attempts": 1}]
        assert w.job_claim().result(5) == []
        w.job_fail(j, "timeout", retry_ms=100, max_attempts=3).result(5)
        assert w.job_claim().result(5) == []  # 还没到重试时间
        clock.t += 200
        assert [x["job_id"] for x in w.job_claim().result(5)] == [j]
        w.job_done(j).result(5)
        assert w.job_add("summary", "piece:1").result(5) == j  # 已完成的重新排队
        assert [x["attempts"] for x in w.job_claim(["summary"]).result(5)] == [1]
        assert w.job_claim(["other"]).result(5) == []
    finally:
        w.close()


def test_control_and_heartbeat(w):
    w.control_set(desired_state="recording", pinned_windows=[{"hwnd": 1}], agent_enabled=True,
                  hidden_from_agents={"apps": ["KeePass.exe"], "title_keywords": ["银行"]}).result(5)
    w.heartbeat(recorder_state="recording", recorder_pid=123, current_title="x").result(5)
    with pytest.raises(ValueError):
        w.control_set(recorder_state="recording")
    with pytest.raises(ValueError):
        w.heartbeat(desired_state="paused")
    with pytest.raises(ValueError):
        w.control_set(desired_state="maybe")
    c = ro(w)
    r = c.execute("SELECT * FROM control").fetchone()
    assert r["desired_state"] == "recording" and r["request_seq"] == 1 and r["agent_enabled"] == 1
    assert r["recorder_pid"] == 123 and r["recorder_heartbeat_ms"] > 0
    assert "KeePass.exe" in r["hidden_from_agents"]
    c.close()


def test_pieces_and_summaries(w):
    s1 = w.segment_for("a", "t", 1).result(5)
    s2 = w.segment_for("a", "u", 1).result(5)
    p = w.piece_put(0, 100, app="a", title="t", segment_ids=[s1]).result(5)
    assert w.piece_put(0, 200, app="a", title="t", segment_ids=[s1, s2], task_label="写报告",
                       piece_id=p).result(5) == p
    w.summary_put("piece", str(p), "第一版", refs=[{"piece": p}], model="m", endpoint_host="h").result(5)
    w.summary_stale("piece", str(p)).result(5)
    sid = w.summary_put("piece", str(p), "第二版", model="m").result(5)
    c = ro(w)
    assert c.execute("SELECT end_ms, task_label FROM pieces WHERE piece_id = ?", (p,)).fetchone()[:] == (200, "写报告")
    assert c.execute("SELECT COUNT(*) FROM piece_segments WHERE piece_id = ?", (p,)).fetchone()[0] == 2
    r = c.execute("SELECT * FROM summaries WHERE summary_id = ?", (sid,)).fetchone()
    assert (r["text"], r["status"]) == ("第二版", "ok")
    with pytest.raises(ValueError):
        w.summary_put("week", "x", "y")
    c.close()


def test_skim_and_volatile(w):
    sid, b = _block(w)
    k = w.skim(sid, b, 100, 50, 0, 2000).result(5)  # end < start 被纠正
    w.segment_volatile(sid, [[0, 1]]).result(5)
    c = ro(w)
    assert c.execute("SELECT start_ms, end_ms FROM skims WHERE skim_id = ?", (k,)).fetchone()[:] == (100, 100)
    assert c.execute("SELECT volatile_mask FROM segments WHERE segment_id = ?", (sid,)).fetchone()[0] == "[[0, 1]]"
    c.close()


def test_threads_can_submit_concurrently(w):
    _, b = _block(w)
    out = []

    def worker(k):
        out.append(w.record_lines(b, 1000 + k, (k * 1000, k * 1000 + 900),
                                  [L(f"t{k}-{i}", k * 1000 + i * 30) for i in range(20)]))

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(len(f.result(10)) for f in out) == 160
    c = ro(w)
    assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 160
    c.close()


def test_close_flushes_pending_frames(tmp_path):
    w = Writer(tmp_path)
    sid, b = _block(w)
    fids = [w.save_frame(np.full((50, 50, 3), i, np.uint8), captured_ms=1000 + i, segment_id=sid) for i in range(5)]
    w.close()
    c = schema.connect_ro(schema.db_path(tmp_path))
    assert [r[0] for r in c.execute("SELECT status FROM frames")] == ["ok"] * 5
    for fid in fids:
        assert (tmp_path / frame_rel_path(fid, 1000)).parent.exists()
    c.close()
    assert isinstance(Future(), Future)
