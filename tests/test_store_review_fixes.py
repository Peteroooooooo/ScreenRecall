"""v2 存储复核后修的问题：写线程不卡死、回滚的结果不被引用、晚到写入不让删掉的内容复活、
OCR 结果一步登记、隐藏名单的大小写、混合短词、向量分支的时间语义、向量文件被映射时的修复与清零。"""

import threading

import numpy as np
import pytest

from screenrecall.store import delete, schema
from screenrecall.store.fts import lexical
from screenrecall.store.queries import Queries
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import BlockGone, LineIn, Writer, WriterConfig, frame_rel_path

DIM = 8


def L(text, y, h=20, x0=10, x1=500, source="uia"):
    return LineIn(text, x0, y, x1, y + h, source)


def block(w, app="a.exe", title="t", t=1000):
    sid = w.segment_for(app, title, t).result(5)
    return sid, w.block_new(sid, t).result(5)


def raw_bytes(data_dir):
    return b"".join(p.read_bytes() for p in data_dir.rglob("*") if p.is_file())


# ---------------- 写线程 ----------------

def test_raw_op_leaving_transaction_open_does_not_wedge_writer(tmp_path):
    with Writer(tmp_path) as w:
        def bad(conn):
            conn.execute("BEGIN IMMEDIATE")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            w.run_raw(bad).result(5)
        # 成功返回但没提交：同样回滚并报错
        with pytest.raises(RuntimeError):
            w.run_raw(lambda conn: conn.execute("BEGIN IMMEDIATE") and 1).result(5)
        assert w.segment_for("x", "y", 1).result(5) > 0


def test_rolled_back_result_is_not_reused_by_later_ops(tmp_path):
    with Writer(tmp_path) as w:
        gate = threading.Event()
        w.run_raw(lambda conn: gate.wait(5))  # 写线程先卡住，让下面几个操作进同一批
        g = w.gap_open(100, "locked")

        def severe(conn):
            conn.execute("ROLLBACK")  # 整个事务没了：ROLLBACK TO op 也会失败，本批作废
            raise RuntimeError("severe")

        bad = w.call(severe)
        gate.set()
        with pytest.raises(RuntimeError):
            bad.result(5)
        with pytest.raises(Exception):
            g.result(5)
        # 引用作废结果的后续操作要失败，不能拿着回滚掉的 gap_id 去改别的行
        with pytest.raises(Exception):
            w.gap_close(g, 900).result(5)
        c = schema.connect_ro(w.db_path)
        assert c.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0
        c.close()


def test_flush_waits_for_every_encoder_thread(tmp_path):
    with Writer(tmp_path, WriterConfig(webp_threads=3)) as w:
        fids = [w.save_frame(np.full((300, 300, 3), i, np.uint8), captured_ms=1000 + i) for i in range(9)]
        w.flush()
        c = schema.connect_ro(w.db_path)
        assert [r[0] for r in c.execute("SELECT status FROM frames")] == ["ok"] * 9
        c.close()
        assert all((tmp_path / frame_rel_path(f, 1000)).parent.exists() for f in fids)


def test_save_frame_after_close_raises_without_queuing(tmp_path):
    w = Writer(tmp_path)
    w.close()
    with pytest.raises(RuntimeError):
        w.save_frame(np.zeros((4, 4, 3), np.uint8), captured_ms=1)


def test_heartbeat_does_not_bump_revision(tmp_path):
    with Writer(tmp_path) as w:
        w.segment_for("a", "t", 1).result(5)
        c = schema.connect_ro(w.db_path)
        rev = schema.get_revision(c)
        for _ in range(5):
            w.heartbeat(recorder_state="recording").result(5)
        assert schema.get_revision(c) == rev
        w.control_set(desired_state="recording").result(5)
        assert schema.get_revision(c) == rev + 1
        c.close()


def test_sweep_frames_after_crash(tmp_path):
    clock = lambda: 10_000_000  # noqa: E731
    with Writer(tmp_path, clock=clock) as w:
        sid, b = block(w)
        w.flush()

        def fake_crash(conn):  # 上次进程在编码中途退出：两帧停在 encoding，一帧文件已写好、一帧只剩 .tmp
            for fid, done in (("a" * 32, True), ("b" * 32, False)):
                rel = frame_rel_path(fid, 1000)
                conn.execute("INSERT INTO frames (frame_id, frame_no, image_path, width, height, captured_ms, "
                             "last_seen_ms, status) VALUES (?, ?, ?, 4, 4, 1000, 1000, 'encoding')",
                             (fid, 100 + done, rel))
                p = tmp_path / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                (p if done else p.with_suffix(".tmp")).write_bytes(b"SWEEPSCREEN" * 10)

        w.call(fake_crash).result(5)
        assert w.sweep_frames().result(5) == 2
        c = schema.connect_ro(w.db_path)
        st = dict(c.execute("SELECT frame_id, status FROM frames").fetchall())
        assert st == {"a" * 32: "ok", "b" * 32: "failed"}
        c.close()
    assert not list(tmp_path.rglob("*.tmp"))


# ---------------- 晚到写入与隐私删除 ----------------

def test_late_writes_inside_deleted_range_are_dropped(tmp_path):
    """主进程执行删除；录制进程（另一个 Writer）队列里时间更早的写入在删除之后才提交。"""
    rec = Writer(tmp_path)
    main = Writer(tmp_path)
    try:
        sid, b = block(rec, "secret.exe", "窗口", 1000)
        rec.record_lines(b, 1000, (0, 300), [L("LATESENTINELA 第一行", 10)]).result(5)
        main.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=5000,
                                                     now_ms=5000)).result(10)
        # 内容源空了但还在用：编号保留、标题清空
        c = schema.connect_ro(rec.db_path)
        assert tuple(c.execute("SELECT app, title FROM segments WHERE segment_id = ?", (sid,)).fetchone()) == (
            "secret.exe", "")
        c.close()
        # 删除之后才到的：候选帧（captured 在范围内）、那时读到的字、划过记录
        ids = rec.record_lines(b, 3000, (0, 300), [L("LATESENTINELB 第二行", 40)]).result(5)
        fid = rec.save_frame(np.full((20, 20, 3), 9, np.uint8), captured_ms=3000, segment_id=sid, block_id=b,
                             app="secret.exe", line_ids=ids, line_rects=[(0, 0, 1, 1)], ocr_rects=[(0, 0, 20, 20)])
        assert rec.skim(sid, b, 2000, 2500).result(5) is None
        assert ids == [None]
        # 范围之后的照常保存
        later = rec.record_lines(b, 6000, (0, 300), [L("之后的内容", 70)]).result(5)
        assert later[0] is not None
        rec.flush()
        c = schema.connect_ro(rec.db_path)
        assert c.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM ocr_regions").fetchone()[0] == 0
        assert [r[0] for r in c.execute("SELECT text FROM lines")] == ["之后的内容"]
        c.close()
        assert not (tmp_path / frame_rel_path(fid, 3000)).exists()
    finally:
        rec.close()
        main.close()
    assert b"latesentinel" not in raw_bytes(tmp_path).lower()


def test_app_only_delete_blocks_only_writes_before_the_delete(tmp_path):
    with Writer(tmp_path) as w:
        sid, b = block(w, "secret.exe", "t", 1000)
        now = 50_000
        w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, app="Secret.EXE", now_ms=now)).result(10)
        assert w.record_lines(b, now - 1, (0, 300), [L("before", 10)]).result(5) == [None]
        assert w.record_lines(b, now + 1, (0, 300), [L("after", 10)]).result(5)[0] is not None
        other_sid, ob = block(w, "other.exe", "t", 1000)
        assert w.record_lines(ob, now - 1, (0, 300), [L("other app", 10)]).result(5)[0] is not None
        # 晚到的窗口焦点：从删除那一刻算起
        w.activity_start(now - 100, hwnd=1, pid=1, process="secret.exe", title="t").result(5)
        c = schema.connect_ro(w.db_path)
        assert c.execute("SELECT start_ms FROM activity").fetchone()[0] == now
        c.close()


def test_old_empty_segment_dropped_and_writer_reports_block_gone(tmp_path):
    with Writer(tmp_path) as w:
        sid, b = block(w, "a.exe", "很久以前", 1000)
        w.record_lines(b, 1000, (0, 300), [L("x", 10)]).result(5)
        rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=2000,
                                                        now_ms=10 ** 9)).result(10)
        assert rep.segments == 1
        with pytest.raises(BlockGone):
            w.record_lines(b, 10 ** 9 + 1, (0, 300), [L("y", 10)]).result(5)


def test_range_reaching_into_future_does_not_block_new_recording(tmp_path):
    """"删除今天"时范围的结束在将来：删除之后新录的内容照常保存。"""
    with Writer(tmp_path) as w:
        sid, b = block(w, "a.exe", "t", 1000)
        w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=100_000,
                                                  now_ms=20_000)).result(10)
        assert w.record_lines(b, 19_000, (0, 300), [L("late", 10)]).result(5) == [None]
        assert w.record_lines(b, 21_000, (0, 300), [L("new", 10)]).result(5)[0] is not None


def test_ocr_result_maps_frame_coords_and_completes(tmp_path):
    with Writer(tmp_path) as w:
        sid, b = block(w)
        fid = w.save_frame(np.zeros((400, 300, 3), np.uint8), captured_ms=2000, segment_id=sid, block_id=b,
                           pos_y=1000, ocr_rects=[(0, 100, 300, 300)])
        w.flush()
        reg = w.ocr_claim().result(5)[0]
        ids = w.ocr_result(reg["region_id"], [L("识别出的文字", 120, source="uia")]).result(5)
        q = Queries(tmp_path)
        f = q.frame(fid)
        assert [x["line_id"] for x in f["lines"]] == ids and f["lines"][0]["rect"] == [10, 120, 500, 140]
        c = q.conn()
        ln = c.execute("SELECT y0, y1, source, first_seen_ms FROM lines WHERE line_id = ?", (ids[0],)).fetchone()
        assert tuple(ln) == (1120, 1140, "ocr", 2000)  # 块坐标 = 帧坐标 + pos_y；时间 = 帧的拍摄时间
        assert c.execute("SELECT status FROM ocr_regions").fetchone()[0] == "done"
        q.close()


def test_ocr_result_for_region_deleted_meanwhile_writes_nothing(tmp_path):
    with Writer(tmp_path) as w:
        sid, b = block(w)
        w.save_frame(np.zeros((40, 40, 3), np.uint8), captured_ms=2000, segment_id=sid, block_id=b, pos_y=0,
                     ocr_rects=[(0, 0, 40, 40)])
        w.flush()
        reg = w.ocr_claim().result(5)[0]
        # OCR 进行中，用户删掉了这段时间
        w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=3000)).result(10)
        assert w.ocr_result(reg["region_id"], [L("OCRRESURRECT", 5)]).result(5) is None
        c = schema.connect_ro(w.db_path)
        assert c.execute("SELECT COUNT(*) FROM lines").fetchone()[0] == 0
        c.close()


# ---------------- 检索与隐藏名单 ----------------

def test_mixed_short_term_is_a_plain_substring(tmp_path):
    with Writer(tmp_path) as w:
        _, b = block(w)
        w.record_lines(b, 1000, (0, 500), [L("订单21号已发货", 10), L("第1号", 40), L("rain AIGC", 70)]).result(5)
        c = schema.connect_ro(w.db_path)
        for mode in (None, "exact", "recent"):
            assert lexical.search(c, "1号", mode=mode).total == 2
            assert lexical.search(c, "ai", mode=mode).total == 1  # 纯字母数字仍是"以它开头的词"
        c.close()


def test_hidden_lists_fold_non_ascii_case_everywhere(tmp_path):
    with Writer(tmp_path) as w:
        sid, b = block(w, "Банк.exe", "Отчёт БАНКА", 1000)
        ids = w.record_lines(b, 1000, (0, 300), [L("隐藏的内容", 10)])
        fid = w.save_frame(np.zeros((8, 8, 3), np.uint8), captured_ms=1000, segment_id=sid, block_id=b,
                           app="Банк.exe", title="Отчёт БАНКА", line_ids=ids, line_rects=[(0, 0, 1, 1)])
        p = w.piece_put(900, 1100, app="Банк.exe", title="Отчёт БАНКА", segment_ids=[sid]).result(5)
        w.summary_put("piece", str(p), "概括")
        w.activity_start(1000, hwnd=1, pid=1, process="Банк.exe", title="Отчёт БАНКА")
        w.gap(2000, 3000, "excluded", detail="Отчёт БАНКА")  # 缺口的 detail 常是窗口标题
        w.flush()
    q = Queries(tmp_path, clock=lambda: 5000)
    for kw in ({"exclude_title_keywords": ["банка"]}, {"exclude_apps": ["БАНК.EXE"]},
               {"exclude_title_keywords": ["отЧЁТ"]}):
        assert q.search("隐藏", **kw)["total"] == 0
        items = q.timeline(0, 10_000, **kw)["items"]
        if "exclude_title_keywords" in kw:
            assert [(x["kind"], x["detail"]) for x in items] == [("gap", None)]
        else:
            assert [x["kind"] for x in items] == ["gap"]
        assert q.piece(p, **kw) is None
        assert q.frame(fid, **kw) is None
        assert q.summary("piece", str(p), **kw) is None
    assert q.apps(exclude_apps=["банк.EXE"]) == []
    # 不传名单时照常可见
    assert q.search("隐藏")["total"] == 1 and q.piece(p) is not None and q.frame(fid) is not None
    q.close()


def test_hidden_title_keyword_full_width(tmp_path):
    with Writer(tmp_path) as w:
        _, b = block(w, "a.exe", "网上银行 ＢＡＮＫ", 1000)
        w.record_lines(b, 1000, (0, 300), [L("余额", 10)]).result(5)
    q = Queries(tmp_path)
    assert q.search("余额", exclude_title_keywords=["bank"])["total"] == 0
    q.close()


def test_vector_branch_uses_line_existence_interval(tmp_path):
    """向量库 ts = first_seen；之后又看到（last_seen 变大）的行，按 time_from 筛选仍要能命中。"""
    with Writer(tmp_path) as w:
        s1, b1 = block(w, "a.exe", "doc", 1000)
        a = w.record_lines(b1, 1000, (0, 300), [L("长期可见的一行", 10)]).result(5)
        w.record_lines(b1, 9000, (0, 300), [L("长期可见的一行", 10)]).result(5)
        s2, b2 = block(w, "b.exe", "old", 1000)
        old = w.record_lines(b2, 1000, (0, 300), [L("早就消失的一行", 10)]).result(5)
    vs = VectorStore(tmp_path / "vectors" / "text", DIM)
    e = np.eye(DIM, dtype=np.float32)
    vs.append([a[0], old[0]], np.stack([e[0], e[0]]), [1000, 1000], [s1, s2])
    q = Queries(tmp_path, text_vectors=VectorStore(tmp_path / "vectors" / "text", DIM, readonly=True))
    res = q.search("毫不相干", time_from=5000, encoder=lambda text: e[0])
    assert [g["segment_id"] for g in res["groups"]] == [s1] and res["groups"][0]["sources"] == ["vector"]
    res = q.search("毫不相干", time_to=500, encoder=lambda text: e[0])
    assert res["groups"] == []
    q.close()


def test_day_overview_lines_new_uses_index(tmp_path):
    with Writer(tmp_path):
        pass
    c = schema.connect_ro(schema.db_path(tmp_path))
    plan = " ".join(r[-1] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM lines WHERE last_seen_ms >= 1 AND first_seen_ms >= 1 "
        "AND first_seen_ms < 2"))
    assert "idx_lines_last_seen" in plan
    c.close()


def test_queries_close_closes_connections_of_all_threads(tmp_path):
    with Writer(tmp_path):
        pass
    q = Queries(tmp_path)
    conns = []
    t = threading.Thread(target=lambda: conns.append(q.conn()))
    t.start()
    t.join()
    conns.append(q.conn())
    q.close()
    for c in conns:
        with pytest.raises(Exception):
            c.execute("SELECT 1")
    assert q.conn().execute("SELECT 1").fetchone()[0] == 1  # 之后仍可重新打开
    q.close()


# ---------------- 向量库 ----------------

def test_repair_when_reader_maps_the_files(tmp_path):
    root = tmp_path / "v"
    vs = VectorStore(root, 4)
    vs.append([1, 2], np.ones((2, 4), np.float32), [1, 2], [1, 1])
    rd = VectorStore(root, 4, readonly=True)
    assert len(rd) == 2
    with open(root / "s00000.g0.vec", "ab") as fh:  # 上次追加写到一半：vec 多了一行
        fh.write(np.full(4, 9, np.float32).tobytes())
    w2 = VectorStore(root, 4)  # 读取方映射着文件，没法截短：应另开一段而不是报错
    w2.append([3], np.full((1, 4), 2, np.float32), [3], [2])
    rd.refresh()
    hits = {h.id: h for h in rd.search(np.ones(4, np.float32), 10)}
    assert sorted(hits) == [1, 2, 3] and hits[3].group == 2 and hits[3].ts == 3
    rd.close()


def test_delete_zeroes_old_generation_files_a_reader_still_maps(tmp_path):
    root = tmp_path / "v"
    vs = VectorStore(root, DIM, segment_rows=4)
    marker = np.full(DIM, 77.25, np.float32)
    vecs = np.ones((8, DIM), np.float32)
    vecs[1] = marker
    vs.append(list(range(1, 9)), vecs, list(range(8)), [0] * 8)
    rd = VectorStore(root, DIM, readonly=True)
    rd.search(np.ones(DIM, np.float32), 3)  # 映射着第一段的旧一代文件
    vs.delete([1])
    vs.compact()  # 新一代只留活行；旧文件被读取方占着，删不掉
    vs.delete([2])  # 之后又删了一行：旧一代文件里的这一行也要清零
    blob = b"".join(p.read_bytes() for p in root.iterdir() if p.is_file())
    assert marker.tobytes() not in blob
    rd.close()


def test_concurrent_searches_while_rows_arrive(tmp_path):
    """网页服务的多个线程共用一个只读实例，同时空闲整理进程在追加：检索不能因 refresh 换映射而出错。"""
    root = tmp_path / "v"
    wr = VectorStore(root, DIM, segment_rows=64)
    rng = np.random.default_rng(3)
    wr.append(range(1, 65), rng.standard_normal((64, DIM)).astype(np.float32), range(64), [0] * 64)
    rd = VectorStore(root, DIM, readonly=True)
    errors = []
    stop = threading.Event()

    def searcher():
        while not stop.is_set():
            try:
                rd.search(rng.standard_normal(DIM).astype(np.float32), 5, best_per_group=True)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    ts = [threading.Thread(target=searcher) for _ in range(4)]
    for t in ts:
        t.start()
    for i in range(40):
        base = 65 + i * 16
        wr.append(range(base, base + 16), rng.standard_normal((16, DIM)).astype(np.float32),
                  range(base, base + 16), [i % 5] * 16)
        if i % 10 == 9:
            wr.delete(range(base, base + 4))
    stop.set()
    for t in ts:
        t.join()
    assert errors == []
    rd.refresh()
    assert len(rd) == 64 + 40 * 16 - 4 * 4
    rd.close()


def test_search_with_non_positive_k(tmp_path):
    vs = VectorStore(tmp_path / "v", DIM)
    vs.append([1], np.ones((1, DIM), np.float32), [1])
    assert vs.search(np.ones(DIM, np.float32), 0) == []
