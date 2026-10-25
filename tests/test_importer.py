"""批量导入（方案 6.8）：格式与解码校验、时间未知标注、批次幂等与安全重试、与实时截图同一条流水线。"""

import io
import json
import logging
import threading

import numpy as np
from PIL import Image

from screenrecall import db, evidence, importer, search
from screenrecall.importer import FAILED, OK, SKIPPED, ImportFile
from screenrecall.ocr import OcrWorker

from .test_retrieval import box


def _png(seed: int, size=(64, 48), fmt="PNG") -> bytes:
    arr = np.random.default_rng(seed).integers(0, 255, size=(size[1], size[0], 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, fmt)
    return buf.getvalue()


PLENTY = lambda p: 100 * 1024**3  # noqa: E731


def _run(h, files, **kw):
    return importer.import_batch(h.conn, h.cfg, files, free_bytes=kw.pop("free_bytes", PLENTY), **kw)


def test_formats_validated_and_bad_file_does_not_block_others(h):
    files = [
        ImportFile("a.png", _png(1)),
        ImportFile("b.jpg", _png(2, fmt="JPEG")),
        ImportFile("broken.png", b"\x89PNG\r\n\x1a\nnot really"),
        ImportFile("c.webp", _png(3, fmt="WEBP")),
        ImportFile("note.txt", b"hello"),
        ImportFile("fake.png", _png(4, fmt="GIF")),  # 扩展名对、实际格式不对
        ImportFile("tiny.png", _png(5, size=(8, 8))),
    ]
    res = _run(h, files)
    assert [i.status for i in res.items] == [OK, OK, FAILED, OK, FAILED, FAILED, FAILED]
    reasons = {i.name: i.reason for i in res.items}
    assert "无法解码" in reasons["broken.png"]
    assert "不支持的文件类型" in reasons["note.txt"]
    assert "GIF" in reasons["fake.png"]
    assert "尺寸" in reasons["tiny.png"]
    frames = h.conn.execute("SELECT * FROM frames").fetchall()
    assert len(frames) == 3
    # 与实时截图同一条流水线：WebP 原子落盘 + OCR 待办
    assert {f["image_format"] for f in frames} == {"webp-lossless"}  # 默认无损（config.default.toml）
    assert h.conn.execute("SELECT COUNT(*) FROM index_jobs WHERE stage = 'ocr' AND status = 'pending'").fetchone()[0] == 3
    for f in frames:
        with Image.open(h.cfg.data_dir / f["image_path"]) as im:
            assert im.format == "WEBP" and im.size == (f["width"], f["height"])


def test_unknown_time_is_marked_and_given_time_is_used(h):
    res = _run(h, [ImportFile("a.png", _png(1))])
    f = h.conn.execute("SELECT * FROM frames").fetchone()
    meta = json.loads(f["capture_meta"])
    assert meta["method"] == "import" and meta["time_known"] is False
    assert not importer.is_time_known(meta)
    assert importer.time_unknown_frames(h.conn, [f["frame_id"]]) == {f["frame_id"]}
    assert not res.time_known

    t = 1_600_000_000_000
    res2 = _run(h, [ImportFile("b.png", _png(2))], captured_at_ms=t, label="旧截图")
    fid = res2.items[0].frame_id
    occ = h.conn.execute("SELECT * FROM occurrences WHERE frame_id = ?", (fid,)).fetchone()
    assert occ["first_seen_ms"] == occ["last_seen_ms"] == t
    assert occ["source_type"] == "import" and occ["window_title"] == "b.png"
    assert importer.time_unknown_frames(h.conn, [fid]) == set()
    sess = h.conn.execute("SELECT * FROM sessions WHERE session_id = ?", (res2.session_id,)).fetchone()
    assert sess["source_type"] == "import" and sess["target_title"] == "旧截图"


def test_resubmitting_same_batch_is_idempotent_and_retries_failures(h):
    files = [ImportFile("a.png", _png(1)), ImportFile("b.png", _png(2))]
    calls = {"n": 0}

    def low_space_once(p):
        calls["n"] += 1
        return 1024 if calls["n"] == 2 else 100 * 1024**3  # 第 2 个文件时空间不足

    first = _run(h, files, free_bytes=low_space_once)
    assert [i.status for i in first.items] == [OK, FAILED]
    assert "空间不足" in first.items[1].reason
    second = _run(h, files)
    assert second.resubmitted and second.batch_id == first.batch_id
    assert [i.status for i in second.items] == [SKIPPED, OK]
    assert second.items[0].reason == "本批次之前已导入"
    third = _run(h, files)
    assert [i.status for i in third.items] == [SKIPPED, SKIPPED]
    assert h.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 2
    assert h.conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1


def test_duplicate_content_within_batch_skipped_but_other_batch_not_reused(h):
    same = _png(7)
    res = _run(h, [ImportFile("x.png", same), ImportFile("y.png", same)])
    assert [i.status for i in res.items] == [OK, SKIPPED]
    assert "第 1 个文件内容相同" in res.items[1].reason
    other = _run(h, [ImportFile("z.png", same)])  # 不同批次：指纹只管导入幂等，照常导入
    assert [i.status for i in other.items] == [OK]
    assert other.batch_id != res.batch_id


def test_imported_frames_flow_through_ocr_and_search(h):
    res = _run(h, [ImportFile("err.png", _png(1)), ImportFile("ok.png", _png(2))])

    class E:
        version = "fake"
        texts = [["error 0x80070005"], ["all good"]]

        def recognize(self, image):
            return [box(self.texts.pop(0)[0])]

    w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    while (job := w._claim(h.conn)) is not None:
        w._process(h.conn, job)
    hits = search.search(h.conn, search.parse_query('"0x80070005"'))
    assert [r.frame_id for r in hits.results] == [res.items[0].frame_id]
    assert hits.results[0].latest.source_type == "import"


def test_import_batch_timeline_follows_file_order_not_time(h):
    names = ["c.png", "a.png", "b.png"]  # 调用方给定的顺序就是处理顺序
    res = _run(h, [ImportFile(n, _png(i)) for i, n in enumerate(names)])
    # 让时间顺序与文件顺序相反，确认时间线仍按文件顺序
    for k, item in enumerate(res.items):
        h.conn.execute("UPDATE occurrences SET first_seen_ms = ?, last_seen_ms = ? WHERE frame_id = ?",
                       (1000 - k, 1000 - k, item.frame_id))
    occ = h.conn.execute("SELECT occurrence_id FROM occurrences WHERE frame_id = ?", (res.items[1].frame_id,)).fetchone()
    ctx = evidence.context(h.conn, occ[0])
    assert ctx.timeline.session.is_import
    assert [s.window_title for s in ctx.timeline.steps] == names
    assert ctx.prev.window_title == "c.png" and ctx.next.window_title == "b.png"
    assert ctx.timeline.session.end_label == "导入批次"


def test_migration_to_v4_on_existing_db(h):
    assert h.conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    tables = {r[0] for r in h.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"import_batches", "import_items"} <= tables


def test_cli_imports_directory_in_name_order(h, tmp_path, monkeypatch, capsys):
    d = tmp_path / "shots"
    d.mkdir()
    for i, n in enumerate(["2.png", "1.png", "skip.txt"]):
        (d / n).write_bytes(_png(i) if n.endswith(".png") else b"x")
    monkeypatch.setenv("SCREENRECALL_DATA_DIR", str(h.cfg.data_dir))
    monkeypatch.setattr(importer.shutil, "disk_usage", lambda p: type("U", (), {"free": 100 * 1024**3})())
    assert importer.main([str(d), "--label", "cli"]) == 0
    out = capsys.readouterr().out
    assert "成功 2" in out and out.index("1.png") < out.index("2.png")
