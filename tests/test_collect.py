"""数据集采集清单、检查、导入、标注表与事实→相关帧（collect.py），以及只建索引的 indexer。"""

import csv
import io
import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from screenrecall import collect, dataset, importer, indexer
from screenrecall.importer import FileMeta, ImportFile

from .conftest import make_image


def _img(seed: int, size=(640, 520)) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, size=(size[1], size[0], 3), dtype=np.uint8)


def _save(path, arr, fmt="PNG", **kw):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(path, fmt, **kw)


def _rows(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _fill(task_dir, **cols):
    cols.setdefault("source_kind", "real_app")
    rows = _rows(task_dir / collect.MANIFEST)
    for r in rows:
        for k, v in cols.items():
            if not r.get(k):
                r[k] = v
    collect.write_csv(task_dir / collect.MANIFEST, collect.MANIFEST_COLS, rows)
    return rows


def _edit(task_dir, image_id, **cols):
    rows = _rows(task_dir / collect.MANIFEST)
    for r in rows:
        if r["image_id"] == image_id:
            r.update(cols)
    collect.write_csv(task_dir / collect.MANIFEST, collect.MANIFEST_COLS, rows)


@pytest.mark.parametrize("name,expect", [
    ("截屏2026-10-17 09.30.12.png", "2026-10-17 09:30:12"),
    ("截屏2026-10-17 下午2.05.09.png", "2026-10-17 14:05:09"),
    ("Screenshot 2026-10-17 at 9.30.12 AM.png", "2026-10-17 09:30:12"),
    ("Screenshot 2026-10-17 at 12.01.02 PM.png", "2026-10-17 12:01:02"),
    ("屏幕截图 2026-10-17 093012.png", "2026-10-17 09:30:12"),
    ("Screenshot 2026-10-17 213012.png", "2026-10-17 21:30:12"),
    ("cao-code-01-003.png", None),          # 改过名：不猜
    ("IMG_2026-10-17 09.30.12.png", None),  # 不是截图工具的名字：不猜
])
def test_time_only_from_screenshot_tool_names(name, expect):
    assert collect.time_from_filename(name) == expect


def test_manifest_builds_and_keeps_human_columns(tmp_path):
    td = tmp_path / "root" / "cao" / "cao-code-01"
    _save(td / "截屏2026-10-17 09.30.12.png", _img(1))
    _save(td / "b.png", _img(2))
    added, total, notes = collect.build_manifest(td, device="cao-mbp")
    assert (added, total, notes) == (2, 2, [])
    rows = {r["file"]: r for r in _rows(td / collect.MANIFEST)}
    a, b = rows["截屏2026-10-17 09.30.12.png"], rows["b.png"]
    assert a["image_id"] == "cao-code-01-001" and a["time_source"] == "filename"
    assert a["captured_local"] == "2026-10-17 09:30:12"
    assert b["time_source"] == "unknown" and b["captured_local"] == ""   # 不用修改时间冒充
    assert a["collector"] == "cao" and a["scenario"] == "code" and a["keep"] == "yes"
    _edit(td, a["image_id"], app="Terminal", note="终端")
    _save(td / "c.png", _img(3))
    added, total, _ = collect.build_manifest(td)
    rows = {r["file"]: r for r in _rows(td / collect.MANIFEST)}
    assert (added, total) == (1, 3)
    assert rows["截屏2026-10-17 09.30.12.png"]["app"] == "Terminal"
    assert rows["c.png"]["image_id"] == "cao-code-01-003"   # 编号不重排
    with pytest.raises(collect.CollectError):
        collect.build_manifest(tmp_path / "root" / "cao" / "code-01")


def _two_tasks(tmp_path):
    root = tmp_path / "root"
    t1, t2 = root / "cao" / "cao-code-01", root / "xiao" / "xiao-chat-01"
    base = _img(10)
    _save(t1 / "a.png", base)
    _save(t1 / "b.png", _img(11))
    _save(t2 / "c.png", _img(12))
    for t, dev in ((t1, "cao-mbp"), (t2, "xiao-win")):
        collect.build_manifest(t, device=dev)
        _fill(t, app="Terminal", language="en")
    (root / "cao" / "facts.csv").write_text("fact_id,task_id,content\ncao-code-01#1,cao-code-01,0x8007A1F3\n", encoding="utf-8")
    return root, t1, t2, base


def test_check_passes_clean_set_and_counts(tmp_path):
    root, *_ = _two_tasks(tmp_path)
    res = collect.check(root)
    assert res.ok, res.errors
    assert res.counts["by_collector_scenario"] == {"cao/code": 2, "xiao/chat": 1}
    assert res.counts["time_unknown"] == 3


def test_check_rejects_copies_cross_task_near_dups_and_bad_fields(tmp_path):
    root, t1, t2, base = _two_tasks(tmp_path)
    near = base.copy()
    near[:4, :20] = 0                       # 只动极少像素：近重复
    _save(t2 / "near.png", near)
    _save(t1 / "dup.png", base, compress_level=1)   # 同像素、不同文件字节（另存一份）
    _save(t1 / "same-bytes.png", base)               # 字节完全相同：清单只留一行并提示
    _save(t1 / "small.png", _img(13, size=(400, 300)))
    _, _, mnotes = collect.build_manifest(t1)
    assert any("内容完全相同" in n for n in mnotes)
    collect.build_manifest(t2)
    _fill(t1, app="Terminal", language="en")
    _fill(t2, app="Slack", language="xx")
    ids = {r["file"]: r["image_id"] for r in _rows(t1 / collect.MANIFEST)}
    _edit(t1, ids["b.png"], facts="cao-code-01#9", captured_local="2026-10-17 09:00:00")
    res = collect.check(root)
    text = "\n".join(res.errors)
    assert "像素完全相同" in text
    assert "跨任务近重复" in text
    assert "短边小于" in text
    assert "language 只能是" in text
    assert "cao-code-01#9 不在 facts.csv" in text
    assert "captured_local 必须留空" in text
    cross = {(a, b) for a, b, same in res.near_pairs if not same}
    assert cross
    # 处理：drop 掉跨任务的那几张（要写原因），复制件也 drop
    rows2 = {r["file"]: r["image_id"] for r in _rows(t2 / collect.MANIFEST)}
    _edit(t2, rows2["near.png"], keep="drop")
    res = collect.check(root)
    assert any("drop_reason" in e for e in res.errors)


def _judge(root, rows):
    collect.write_csv(root / "cao" / collect.NEAR_DUPS, collect.NEAR_DUP_COLS, rows)


def test_within_task_near_dup_needs_pairwise_judgement(tmp_path):
    root, t1, _, base = _two_tasks(tmp_path)
    near = base.copy()
    near[:4, :20] = 0
    _save(t1 / "a2.png", near)
    collect.build_manifest(t1)
    _fill(t1, app="Terminal", language="en")
    res = collect.check(root)
    assert res.ok, res.errors
    assert any("任务内近重复" in w for w in res.warnings)
    assert res.counts["near_same_task"] == 1 and res.counts["near_pending"] == 1
    ids = {r["file"]: r["image_id"] for r in _rows(t1 / collect.MANIFEST)}
    a, a2 = ids["a.png"], ids["a2.png"]
    # 旧写法（note 标记）不再放行，只提示迁移
    _edit(t1, a2, note=collect.NEAR_OK_MARK + "batch_size 16→18")
    res = collect.check(root)
    assert res.counts["near_pending"] == 1 and any("旧标记" in w for w in res.warnings)
    # 空理由 / 缺判定人：不算判定
    _judge(root, [{"image_id_a": a, "image_id_b": a2, "decision": "keep_both", "reason": "", "reviewer": "xiao"}])
    res = collect.check(root)
    assert res.counts["near_pending"] == 1 and any("reason" in w for w in res.warnings)
    _judge(root, [{"image_id_a": a2, "image_id_b": a, "decision": "keep_both", "reason": "batch_size 16→18",
                   "reviewer": ""}])
    assert collect.check(root).counts["near_pending"] == 1
    # 完整判定（顺序无关）
    _judge(root, [{"image_id_a": a2, "image_id_b": a, "decision": "keep_both", "reason": "batch_size 16→18",
                   "reviewer": "xiao"}])
    res = collect.check(root)
    assert not any("待人工判定" in w for w in res.warnings)
    # 近重复始终计数并逐对列出，不会因为判定过就从报告里消失
    assert res.counts["near_same_task"] == 1 and res.counts["near_pending"] == 0
    text = collect.render_check(res)
    assert "任务内 1 对（待人工判定 0 对）" in text and "已逐对判定保留" in text and "未统计" in text


def test_judging_one_pair_does_not_release_others(tmp_path):
    root, t1, _, base = _two_tasks(tmp_path)
    for k, name in ((1, "n1.png"), (2, "n2.png")):
        near = base.copy()
        near[:4, k * 20:k * 20 + 20] = 0
        _save(t1 / name, near)
    collect.build_manifest(t1)
    _fill(t1, app="Terminal", language="en")
    ids = {r["file"]: r["image_id"] for r in _rows(t1 / collect.MANIFEST)}
    res = collect.check(root)
    assert res.counts["near_same_task"] == 3 and res.counts["near_pending"] == 3
    _judge(root, [{"image_id_a": ids["a.png"], "image_id_b": ids["n1.png"], "decision": "keep_both",
                   "reason": "左上角计数 1→2", "reviewer": "xiao"}])
    res = collect.check(root)
    assert res.counts["near_pending"] == 2      # 只放行判过的那一对，a~n2、n1~n2 仍待判
    _judge(root, [{"image_id_a": ids["a.png"], "image_id_b": ids["n2.png"], "decision": "maybe",
                   "reason": "x", "reviewer": "xiao"}])
    res = collect.check(root)
    assert res.counts["near_pending"] == 3 and any("decision 只能是" in w for w in res.warnings)


def test_source_kind_required_and_mock_ui_is_dev_only(tmp_path):
    root, t1, _, _ = _two_tasks(tmp_path)
    rows = _rows(t1 / collect.MANIFEST)
    _edit(t1, rows[0]["image_id"], source_kind="")
    assert any("source_kind" in e for e in collect.check(root).errors)
    _edit(t1, rows[0]["image_id"], source_kind="mock_ui")
    res = collect.check(root)
    assert res.ok and res.counts["dev_only"] == 1


def test_old_manifest_without_source_kind_column_is_readable(tmp_path):
    root, t1, _, _ = _two_tasks(tmp_path)
    rows = _rows(t1 / collect.MANIFEST)
    old_cols = [c for c in collect.MANIFEST_COLS if c != "source_kind"]
    collect.write_csv(t1 / collect.MANIFEST, old_cols, rows)
    res = collect.check(root)
    assert not any("缺少列" in e for e in res.errors)
    assert any("source_kind" in e for e in res.errors)          # 能读，但要求补上
    collect.build_manifest(t1)                                  # 刷新清单会补出这一列（空值）
    assert "source_kind" in _rows(t1 / collect.MANIFEST)[0]


@pytest.mark.parametrize("text,expect", [
    ("file:///Users/x/scenes-html/cao-code-01/001.html", ["cao-code-01"]),
    ("Slack | chat - 01 workspace", ["chat-01"]),
    ("RuntimeError: shape '[16, 3, 224, 224]'", []),
    ("web page 2026-10-17", []),
])
def test_task_id_leaks(text, expect):
    assert collect.task_id_leaks(text) == expect


def test_import_labels_and_facts_round_trip(tmp_path, h):
    root, t1, t2, _ = _two_tasks(tmp_path)
    a = next(r for r in _rows(t1 / collect.MANIFEST) if r["file"] == "a.png")
    _edit(t1, a["image_id"], facts="cao-code-01#1", time_source="manual", captured_local="2026-10-17 10:15:00",
          window_title="zsh — train.py")
    data_dir = str(h.cfg.data_dir)
    out = collect.import_root(root, data_dir)
    assert len(out) == 2 and all("成功" in x for x in out)
    occ = {r["window_title"]: r for r in h.conn.execute("SELECT * FROM occurrences")}
    assert occ["zsh — train.py"]["app"] == "Terminal"              # 清单里的应用和标题进库
    assert "cao-code-01-002.png" in occ                            # 没填标题时用图片编号
    # 重复导入不产生新画面
    assert all("跳过" in x for x in collect.import_root(root, data_dir))
    assert h.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3
    # 任务里补采了一张：只导入新的，旧的不会因为批次变了而重复入库
    _save(t1 / "new.png", _img(20))
    collect.build_manifest(t1)
    _fill(t1, app="Terminal", language="en")
    assert "成功 1" in collect.import_root(root, data_dir, only_task="cao-code-01")[0]
    assert h.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 4

    labels = tmp_path / "labels.csv"
    n, kept, notes = collect.build_labels(root, data_dir, labels)
    assert (n, kept) == (4, 0) and any("OCR" in x for x in notes)
    rows = {r["image_id"]: r for r in _rows(labels)}
    ra = rows[a["image_id"]]
    assert ra["captured_local"] == "2026-10-17 10:15:00" and ra["time_known"] == "yes"
    assert rows["xiao-chat-01-001"]["captured_local"] == "" and rows["xiao-chat-01-001"]["time_known"] == "no"
    assert ra["task_id"] == "cao-code-01" and ra["scenario"] == "code" and ra["label"] == ""
    # 已填的标注在重新生成时保留
    all_rows = _rows(labels)
    for r in all_rows:
        r.update(label="code", annotator="cao", review_label="code", reviewer="xiao")
    collect.write_csv(labels, list(all_rows[0].keys()), all_rows)
    collect.build_labels(root, data_dir, labels)
    assert {r["label"] for r in _rows(labels)} == {"code"}
    # dataset.check 认这份表（多出来的列不影响）
    labelset = tmp_path / "labelset.json"
    labelset.write_text(json.dumps({"labels": ["code", "chat", "web", "table_doc"]}), encoding="utf-8")
    rep = dataset.read_labels(h.conn, labels, dataset.load_labelset(labelset))
    assert rep.ok, rep.errors
    assert len(rep.items) == 4
    assert {it.app for it in rep.items} == {"Terminal"}

    mapping = collect.fact_frames(root, data_dir)
    assert list(mapping) == ["cao-code-01#1"] and len(mapping["cao-code-01#1"]) == 1
    q = tmp_path / "queries.json"
    q.write_text(json.dumps([{"id": "dev-code-en-01", "facts": ["cao-code-01#1"], "relevant": []},
                             {"id": "dev-code-en-02", "facts": ["cao-code-01#1"], "relevant": ["x"]}]),
                 encoding="utf-8")
    notes = collect.fill_relevant(q, mapping)
    qs = json.loads(q.read_text(encoding="utf-8"))
    assert qs[0]["relevant"] == mapping["cao-code-01#1"]
    assert qs[1]["relevant"] == ["x"] and "未覆盖" in notes[0]       # 已有答案不被覆盖


def test_import_refuses_when_check_fails(tmp_path, h):
    root, t1, *_ = _two_tasks(tmp_path)
    _edit(t1, "cao-code-01-001", app="")
    with pytest.raises(collect.CollectError, match="app 没填"):
        collect.import_root(root, str(h.cfg.data_dir))
    assert h.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 0


def test_reviewer_must_differ_from_annotator(tmp_path, h):
    root, *_ = _two_tasks(tmp_path)
    collect.import_root(root, str(h.cfg.data_dir))
    labels = tmp_path / "labels.csv"
    collect.build_labels(root, str(h.cfg.data_dir), labels)
    rows = _rows(labels)
    for r in rows:
        r.update(label="code", annotator="cao", review_label="code", reviewer="cao")
    collect.write_csv(labels, list(rows[0].keys()), rows)
    rep = dataset.read_labels(h.conn, labels, ["code", "chat", "web", "table_doc"])
    assert not rep.ok and "不能与标注人" in rep.errors[0]


def test_mock_ui_frames_are_excluded_from_formal_dataset(tmp_path, h):
    root, t1, *_ = _two_tasks(tmp_path)
    first = _rows(t1 / collect.MANIFEST)[0]["image_id"]
    _edit(t1, first, source_kind="mock_ui")
    collect.import_root(root, str(h.cfg.data_dir))
    labels = tmp_path / "labels.csv"
    _, _, notes = collect.build_labels(root, str(h.cfg.data_dir), labels)
    assert any("mock_ui" in n for n in notes)
    rows = _rows(labels)
    assert {r["image_id"]: r["source_kind"] for r in rows}[first] == "mock_ui"
    for r in rows:
        r.update(label="code", annotator="cao", review_label="code", reviewer="xiao")
    collect.write_csv(labels, list(rows[0].keys()), rows)
    cats = ["code", "chat", "web", "table_doc"]
    rep = dataset.read_labels(h.conn, labels, cats)
    assert rep.ok and rep.dev_only_skipped == 1 and len(rep.items) == len(rows) - 1
    assert len(dataset.read_labels(h.conn, labels, cats, include_dev_only=True).items) == len(rows)


def test_accepted_gate_needs_near_dup_judgements_and_live_manifest(tmp_path, h):
    root, t1, _, base = _two_tasks(tmp_path)
    near = base.copy()
    near[:4, :20] = 0
    _save(t1 / "a2.png", near)
    collect.build_manifest(t1)
    _fill(t1, app="Terminal", language="en")
    collect.import_root(root, str(h.cfg.data_dir))
    labels = tmp_path / "labels.csv"
    collect.build_labels(root, str(h.cfg.data_dir), labels)
    rows = _rows(labels)
    for r in rows:
        r.update(label="code", annotator="cao", review_label="code", reviewer="xiao")
    collect.write_csv(labels, list(rows[0].keys()), rows)
    fids = [r["frame_id"] for r in rows]
    # 近重复没判：拒绝
    problems = dataset.raw_gate(root, labels, fids)
    assert len(problems) == 1 and "近重复还有 1 对" in problems[0]
    ids = {r["file"]: r["image_id"] for r in _rows(t1 / collect.MANIFEST)}
    _judge(root, [{"image_id_a": ids["a.png"], "image_id_b": ids["a2.png"], "decision": "keep_both",
                   "reason": "左上角多一条黑边", "reviewer": "xiao"}])
    assert dataset.raw_gate(root, labels, fids) == []
    # 清单里剔除了但库和标注表里还在：拒绝
    _edit(t1, ids["a2.png"], keep="drop", drop_reason="near-dup of a")
    problems = dataset.raw_gate(root, labels, fids)
    assert len(problems) == 1 and "keep=drop" in problems[0]
    # 命令行同样拒绝，不写名单
    labelset = tmp_path / "labelset.json"
    labelset.write_text(json.dumps({"labels": ["code", "chat", "web", "table_doc"]}), encoding="utf-8")
    out = tmp_path / "accepted.txt"
    rc = dataset.main(["--data-dir", str(h.cfg.data_dir), "accepted", "--labels", str(labels),
                       "--labelset", str(labelset), "--raw", str(root), "--out", str(out)])
    assert rc == 1 and not out.exists()


def test_accepted_gate_exempts_live_recorded_frames(tmp_path, h):
    """后台实时录制的帧没有原始截图清单：不因"清单里找不到"被拒；导入的帧照常要在清单里。"""
    root, t1, _, _ = _two_tasks(tmp_path)
    collect.import_root(root, str(h.cfg.data_dir))
    labels = tmp_path / "labels.csv"
    collect.build_labels(root, str(h.cfg.data_dir), labels)
    imported = [r["frame_id"] for r in _rows(labels)]
    h.show(make_image(77))
    h.start()
    h.tick()
    live = dataset.live_frames(h.conn)
    assert len(live) == 1 and not live & set(imported)
    fids = imported + sorted(live)
    assert any("原始清单里找不到" in p for p in dataset.raw_gate(root, labels, fids))
    assert dataset.raw_gate(root, labels, fids, live=live) == []
    # 导入的帧不会被当成录制帧放行
    assert dataset.raw_gate(root, labels, imported + ["nope"], live=live) != []


def test_task_frame_cap_ignores_mock_ui(tmp_path, monkeypatch):
    root, t1, *_ = _two_tasks(tmp_path)
    monkeypatch.setattr(collect, "MAX_TASK_FRAMES", 1)
    assert any("超过 1" in w for w in collect.check(root, near=False).warnings)
    first = _rows(t1 / collect.MANIFEST)[0]["image_id"]
    _edit(t1, first, source_kind="mock_ui")
    assert not any("超过 1" in w for w in collect.check(root, near=False).warnings)


def test_importer_per_file_meta(h):
    buf = io.BytesIO()
    Image.fromarray(_img(1, (64, 48))).save(buf, "PNG")
    buf2 = io.BytesIO()
    Image.fromarray(_img(2, (64, 48))).save(buf2, "PNG")
    res = importer.import_batch(
        h.conn, h.cfg, [ImportFile("a.png", buf.getvalue()), ImportFile("b.png", buf2.getvalue())],
        metas=[FileMeta(1_760_000_000_000, "Slack", "general"), None], free_bytes=lambda p: 100 * 1024**3)
    assert not res.time_known                      # 有一张未知，整批就不是"时间已知"
    known = importer.time_unknown_frames(h.conn, [i.frame_id for i in res.items])
    assert known == {res.items[1].frame_id}
    o = h.conn.execute("SELECT * FROM occurrences WHERE frame_id = ?", (res.items[0].frame_id,)).fetchone()
    assert (o["app"], o["window_title"], o["first_seen_ms"]) == ("Slack", "general", 1_760_000_000_000)
    with pytest.raises(ValueError):
        importer.import_batch(h.conn, h.cfg, [ImportFile("a.png", b"")], metas=[])


def test_indexer_runs_real_ocr_without_capture(tmp_path, h):
    pytest.importorskip("rapidocr_onnxruntime")
    img = Image.new("RGB", (900, 520), "white")
    from PIL import ImageFont
    ImageDraw.Draw(img).text((40, 200), "ERROR 0x8007A1F3 batch_size=16", fill="black",
                             font=ImageFont.load_default(size=36))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    importer.import_batch(h.conn, h.cfg, [ImportFile("e.png", buf.getvalue())], free_bytes=lambda p: 100 * 1024**3)
    rc = indexer.main(["--data-dir", str(h.cfg.data_dir), "--no-vectors"])
    text = h.conn.execute("SELECT text FROM frame_text").fetchone()[0]
    assert "0x8007A1F3" in text.replace(" ", "")
    # 向量待办被保留（--no-vectors），所以返回 1 表示还有未完成
    assert rc == 1
    assert h.conn.execute("SELECT COUNT(*) FROM index_jobs WHERE stage = 'ocr' AND status = 'done'").fetchone()[0] == 1


def test_split_label_tables_merge_and_conflict(tmp_path, h):
    root, *_ = _two_tasks(tmp_path)
    data_dir = str(h.cfg.data_dir)
    collect.import_root(root, data_dir)
    cao, xiao, total = tmp_path / "labels-cao.csv", tmp_path / "labels-xiao.csv", tmp_path / "labels.csv"
    assert collect.build_labels(root, data_dir, cao, collector="cao")[0] == 2
    assert collect.build_labels(root, data_dir, xiao, collector="xiao")[0] == 1
    assert {r["source_file"] for r in _rows(xiao)} == {"xiao/xiao-chat-01/c.png"}
    for path, who, other, lab in ((cao, "cao", "xiao", "code"), (xiao, "xiao", "cao", "chat")):
        rows = _rows(path)
        for r in rows:
            r.update(label=lab, annotator=who, review_label=lab, reviewer=other)
        collect.write_csv(path, list(rows[0].keys()), rows)
    n, _, _ = collect.build_labels(root, data_dir, total, keep_from=[cao, xiao])
    assert n == 3 and sorted(r["label"] for r in _rows(total)) == ["chat", "code", "code"]
    rows = _rows(xiao)
    rows[0]["label"] = "web"                       # 分表改了但总表还是旧值：不一致要报错
    collect.write_csv(xiao, list(rows[0].keys()), rows)
    with pytest.raises(collect.CollectError, match="不一致"):
        collect.build_labels(root, data_dir, total, keep_from=[cao, xiao])
