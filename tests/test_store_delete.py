"""v2 隐私删除：哨兵字符串删除后在库文件、WAL、向量文件里都找不到；裁剪区间、片段与概括、应用删除。"""

import numpy as np
import pytest

from screenrecall.store import delete, schema
from screenrecall.store.fts import lexical
from screenrecall.store.queries import Queries
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import LineIn, Writer

SENTINEL = "QZXSENTINELWORD"
SENTINEL_CN = "绝密哨兵口令"
DIM = 8


def put(w, app, title, t, texts, block=None, y0=10):
    sid = w.segment_for(app, title, t).result(5)
    b = block or w.block_new(sid, t).result(5)
    ids = w.record_lines(b, t, (0, 2000), [LineIn(x, 10, y0 + i * 30, 600, y0 + i * 30 + 20)
                                           for i, x in enumerate(texts)]).result(5)
    return sid, b, ids


def file_bytes(paths):
    return b"".join(p.read_bytes() for p in paths if p.is_file())


def needles():
    out = []
    for s in (SENTINEL, SENTINEL.lower(), SENTINEL_CN, "SENTINEL", "sentinel", "哨兵口令"):
        out += [s.encode("utf-8"), s.encode("utf-16-le")]
    return out


def test_sentinel_gone_from_db_wal_and_vectors(tmp_path):
    w = Writer(tmp_path)
    tv = VectorStore(tmp_path / "vectors" / "text", DIM)
    iv = VectorStore(tmp_path / "vectors" / "image", DIM)
    # 被删内容：t = 10_000 附近；保留内容：t = 50_000
    keep_sid, _, keep_ids = put(w, "keep.exe", "保留窗口", 50_000, ["保留的内容 hello"] * 1 + ["另一行保留"])
    sid, b, ids = put(w, "secret.exe", f"窗口 {SENTINEL}", 10_000,
                      ["上一行文字", f"{SENTINEL} 和 {SENTINEL_CN}", "下一行文字"])
    # 被删行之前的行：它的 tail 里抄着被删行的开头
    fid = w.save_frame(np.full((40, 40, 3), 7, np.uint8), captured_ms=10_000, segment_id=sid, block_id=b,
                       app="secret.exe", title=f"窗口 {SENTINEL}", line_ids=ids, line_rects=[(0, 0, 1, 1)] * 3,
                       ocr_rects=[(0, 0, 40, 40)])
    w.activity_start(9_000, hwnd=1, pid=1, process="secret.exe", title="窗口")
    w.activity_start(11_000, hwnd=2, pid=2, process="keep.exe", title="保留窗口")
    w.skim(sid, b, 10_000, 10_500, 0, 100)
    p = w.piece_put(9_000, 11_000, app="secret.exe", title="窗口", segment_ids=[sid]).result(5)
    w.summary_put("piece", str(p), f"用户看了 {SENTINEL}", refs=[p])
    w.summary_put("hour", "1970-01-01T08", f"这一小时 {SENTINEL_CN}", start_ms=0, end_ms=3_600_000)
    w.flush()
    marker = np.full(DIM, 321.5, np.float32)
    tv.append([ids[1], keep_ids[0]], np.stack([marker, np.ones(DIM, np.float32)]), [10_000, 50_000], [sid, keep_sid])
    rc = schema.connect_ro(w.db_path)
    fno, rel = rc.execute("SELECT frame_no, image_path FROM frames WHERE frame_id = ?", (fid,)).fetchone()
    rc.close()
    iv.append([fno], marker[None, :] * 2, [10_000], [sid])
    frame_path = tmp_path / rel
    assert frame_path.exists()

    db_files = [w.db_path, w.db_path.with_name(w.db_path.name + "-wal")]
    vec_files = list((tmp_path / "vectors").rglob("*.vec"))
    before = file_bytes(db_files)
    assert any(n in before for n in needles())
    assert marker.tobytes() in file_bytes(vec_files)

    rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=9_500, time_to=10_500,
                                                    text_vectors=[tv], image_vectors=[iv])).result(30)
    assert rep.lines == 3 and rep.frames == 1 and rep.files == 1 and rep.skims == 1
    assert rep.text_vectors == 1 and rep.image_vectors == 1 and rep.pieces == 0
    assert not frame_path.exists() and not rep.checkpoint_busy
    # 写连接还开着（WAL 仍在）时就检查一次，关闭后再检查一次
    live = file_bytes(db_files)
    assert [n for n in needles() if n in live] == []
    w.close()

    after = file_bytes(db_files) + file_bytes(list(tmp_path.glob("*.sqlite3*")))
    found = [n for n in needles() if n in after]
    assert found == [], f"删除后仍能在库文件里找到：{found}"
    assert marker.tobytes() not in file_bytes(list((tmp_path / "vectors").rglob("*")))
    assert (marker * 2).tobytes() not in file_bytes(list((tmp_path / "vectors").rglob("*")))

    c = schema.connect_ro(w.db_path)
    # 保留的内容仍在、仍可搜
    res = lexical.search(c, "保留的内容")
    assert [g.segment_id for g in res.groups] == [keep_sid]
    assert lexical.search(c, "哨兵").total == 0 and lexical.search(c, "上一行").total == 0
    # 片段跨删除范围：保留，概括清空并标为 stale；小时概括同样
    s = c.execute("SELECT text, status FROM summaries WHERE level = 'piece'").fetchone()
    assert tuple(s) == ("", "stale")
    assert tuple(c.execute("SELECT text, status FROM summaries WHERE level = 'hour'").fetchone()) == ("", "stale")
    # 窗口记录：9000-11000 的被裁成 9000-9500 和 10500-11000
    acts = [tuple(r) for r in c.execute("SELECT start_ms, end_ms, process FROM activity ORDER BY start_ms")]
    assert acts == [(9_000, 9_500, "secret.exe"), (10_500, 11_000, "secret.exe"), (11_000, None, "keep.exe")]
    assert c.execute("SELECT COUNT(*) FROM ocr_regions").fetchone()[0] == 0
    c.close()


def test_tail_of_previous_line_cleared(tmp_path):
    w = Writer(tmp_path)
    sid, b, ids = put(w, "a.exe", "t", 1000, ["前一行在这里结束", "ZZPRIVATETAILXX 后面"])
    # 后一行之后不再出现，但前一行一直在：只删后一行所在的时间
    w.record_lines(b, 5000, (0, 25), [LineIn("前一行在这里结束", 10, 10, 600, 30)]).result(5)
    w.run_raw(lambda conn: conn.execute("UPDATE lines SET first_seen_ms = 4000 WHERE line_id = ?", (ids[0],))).result(5)
    rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=2000)).result(10)
    assert rep.lines == 1
    w.close()
    c = schema.connect_ro(schema.db_path(tmp_path))
    r = c.execute("SELECT tail, tail_line_id FROM lines WHERE line_id = ?", (ids[0],)).fetchone()
    assert tuple(r) == ("", None)
    assert lexical.search(c, "结束zzpriv").total == 0
    assert lexical.search(c, "结束").total == 1
    c.close()
    raw = file_bytes([schema.db_path(tmp_path), schema.db_path(tmp_path).with_name("recall.sqlite3-wal")])
    assert b"zzprivatetailxx" not in raw.lower()


def test_app_delete_removes_everything_of_that_app(tmp_path):
    w = Writer(tmp_path)
    s1, _, _ = put(w, "Secret.exe", "机密APPTITLEZZ", 1000, ["机密内容 APPSECRETZZ"])
    s2, _, _ = put(w, "secret.exe", "机密APPTITLEZZ 2", 900_000_000, ["第二天 APPSECRETZZ"])
    s3, _, _ = put(w, "other.exe", "其他", 1000, ["其他内容"])
    w.activity_start(1000, hwnd=1, pid=1, process="secret.exe", title="机密APPTITLEZZ")
    w.activity_start(2000, hwnd=1, pid=1, process="other.exe", title="其他")
    w.gap(500, 600, "excluded", process="secret.exe", detail="机密APPTITLEZZ")
    w.piece_put(1000, 2000, app="secret.exe", title="机密APPTITLEZZ", segment_ids=[s1])
    w.flush()
    rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, app="SECRET.EXE")).result(10)
    assert rep.lines == 2 and rep.segments == 2 and rep.pieces == 1 and rep.activity == 1 and rep.gaps == 1
    w.close()
    raw = file_bytes([schema.db_path(tmp_path), schema.db_path(tmp_path).with_name("recall.sqlite3-wal")])
    assert b"APPSECRETZZ" not in raw and b"appsecretzz" not in raw and "机密APPTITLEZZ".encode() not in raw
    c = schema.connect_ro(schema.db_path(tmp_path))
    assert [r[0] for r in c.execute("SELECT app FROM segments")] == ["other.exe"]
    assert lexical.search(c, "其他内容").total == 1
    c.close()


def test_piece_fully_inside_range_deleted_with_summary(tmp_path):
    w = Writer(tmp_path)
    sid, _, _ = put(w, "a.exe", "t", 1000, ["内容"])
    p = w.piece_put(900, 1100, app="a.exe", title="t", segment_ids=[sid]).result(5)
    w.summary_put("piece", str(p), "概括")
    w.summary_put("day", "2030-01-01", "无关的一天")
    w.flush()
    rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=2000)).result(10)
    assert rep.pieces == 1 and rep.segments == 1
    c = schema.connect_ro(w.db_path)
    assert c.execute("SELECT COUNT(*) FROM summaries WHERE level = 'piece'").fetchone()[0] == 0
    assert c.execute("SELECT status FROM summaries WHERE level = 'day'").fetchone()[0] == "ok"
    c.close()
    w.close()


def test_frame_deleted_while_encoding_leaves_no_file(tmp_path):
    w = Writer(tmp_path)
    gate = __import__("threading").Event()
    w._encoder.submit(gate.wait)  # 编码线程先卡住
    fid = w.save_frame(np.zeros((20, 20, 3), np.uint8), captured_ms=1000)
    rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=2000)).result(10)
    assert rep.frames == 1 and rep.files == 0
    gate.set()
    w.close()
    assert list(tmp_path.rglob("*.webp")) == []
    assert Queries(tmp_path).frame(fid) is None


def test_requires_filter_and_no_transaction(tmp_path):
    c = schema.connect(schema.db_path(tmp_path))
    with pytest.raises(ValueError):
        delete.delete_data(c, tmp_path)
    c.execute("BEGIN")
    with pytest.raises(RuntimeError):
        delete.delete_data(c, tmp_path, app="x")
    c.execute("ROLLBACK")
    c.close()


def test_period_span():
    a, b = delete.period_span("day", "2026-10-27")
    assert b - a in (23 * 3600_000, 24 * 3600_000, 25 * 3600_000)
    a, b = delete.period_span("hour", "2026-10-27T13")
    assert b - a == 3600_000
    assert delete.period_span("piece", "1") is None


def test_checkpoint_busy_reported_while_reader_holds_snapshot(tmp_path):
    w = Writer(tmp_path)
    put(w, "a.exe", "t", 1000, ["BUSYSENTINELQQ 内容"])
    r = schema.connect_ro(w.db_path)
    r.execute("BEGIN")
    r.execute("SELECT COUNT(*) FROM lines").fetchone()  # 网页正在读：持有旧快照
    rep = w.run_raw(lambda conn: delete.delete_data(conn, tmp_path, time_from=0, time_to=2000)).result(30)
    assert rep.lines == 1 and rep.checkpoint_busy
    r.execute("COMMIT")
    assert w.run_raw(lambda conn: delete.checkpoint(conn)).result(10) is False
    raw = file_bytes([w.db_path, w.db_path.with_name("recall.sqlite3-wal")])
    assert b"busysentinelqq" not in raw.lower()
    r.close()
    w.close()
