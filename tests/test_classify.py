"""标注、分组划分与分类器流程（方案 6.1、6.4）。

**全部是合成数据上的流程测试**：画面是纯色加噪声的假图、全图向量来自替身编码器（平均颜色），
类别天然可分。这里的指标只证明代码路径正确，**不是分类器的实验结果**，不能写进报告。
"""

import csv
import json
import logging
import threading

import numpy as np
import pytest

from screenrecall import classify, dataset, lifecycle, search
from screenrecall.embed import EmbedWorker
from screenrecall.ocr import OcrWorker

from .test_retrieval import FakeImage, FakeText, box

COLORS = {"code": (20, 20, 20), "chat": (40, 40, 220), "web": (230, 230, 230)}
APPS = {"code": "Code.exe", "chat": "WeChat.exe", "web": "msedge.exe"}


def _img(color, seed):
    rng = np.random.default_rng(seed)
    base = np.full((30, 40, 3), color, dtype=np.int16)
    return np.clip(base + rng.integers(-15, 15, size=base.shape), 0, 255).astype(np.uint8)


def _add(h, img, text, embed):
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
            return [box(text)]

    w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    w._process(h.conn, w._claim(h.conn))
    while embed.run_once(h.conn):
        pass
    return fid


@pytest.fixture
def lib(h, tmp_path):
    """3 类 × 4 个任务 × 2 帧；每个任务的第二帧与第一帧 OCR 文字相同（近重复，必须同组）。"""
    embed = EmbedWorker(h.cfg, threading.Event(), threading.Event(), logging.getLogger("t"),
                        text_encoder_factory=FakeText, image_encoder_factory=FakeImage, classifier=None)
    truth = {}
    seed = 0
    for label, color in COLORS.items():
        for task in range(4):
            for k in range(2):
                seed += 1
                fid = _add(h, _img(color, seed), f"{label} task {task}", embed)
                truth[fid] = (label, f"{label}-{task}")
    out = tmp_path / "labels.csv"
    assert dataset.export_frames(h.conn, out) == len(truth)
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    for r in rows:
        label, task = truth[r["frame_id"]]
        r.update(label=label, review_label=label, task_id=task, annotator="A", reviewer="B",
                 app=APPS[label], scenario="code" if label == "code" else label, language="zh")
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=dataset.ALL_COLS + ["source_kind"])
        w.writeheader()
        w.writerows(rows)
    labelset = tmp_path / "labelset.json"
    labelset.write_text(json.dumps({"labels": list(COLORS) + ["table_doc"]}), encoding="utf-8")
    return out, labelset, truth, embed


def _rewrite(path, fn):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    fn(rows)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=dataset.ALL_COLS + ["source_kind"])
        w.writeheader()
        w.writerows(rows)


def test_check_reports_unknown_label_missing_review_and_unexplained_change(lib, h):
    out, labelset, truth, _ = lib
    labels = dataset.load_labelset(labelset)
    assert dataset.read_labels(h.conn, out, labels).ok

    def spoil(rows):
        rows[0]["review_label"] = "nonsense"
        rows[1]["review_label"] = ""
        rows[2]["review_label"] = "web" if rows[2]["label"] != "web" else "chat"  # 改了却没写理由
        rows[3]["label"] = rows[3]["review_label"] = rows[3]["annotator"] = rows[3]["reviewer"] = ""  # 未标注：跳过
    _rewrite(out, spoil)
    rep = dataset.read_labels(h.conn, out, labels)
    text = " ".join(rep.errors)
    assert "nonsense" in text and "记录不完整" in text and "change_reason" in text
    assert rep.unlabeled == 1


def test_formal_check_rejects_final_label_only_same_person_and_bad_source(lib, h):
    out, labelset, truth, _ = lib
    labels = dataset.load_labelset(labelset)

    def spoil(rows):
        for k in ("label", "annotator", "review_label", "reviewer"):   # 只填 final_label：绕过复核
            rows[0][k] = ""
        rows[0]["final_label"] = "code"
        rows[1]["reviewer"] = rows[1]["annotator"]                      # 自己复核自己
        rows[2]["source_kind"] = "whatever"                             # 来源不合法
        rows[3]["review_label"] = "web" if rows[3]["label"] != "web" else "chat"  # 分歧但没定稿
        rows[3]["change_reason"] = "看错了"
    _rewrite(out, spoil)
    rep = dataset.read_labels(h.conn, out, labels)
    text = " ".join(rep.errors)
    assert "只填 final_label 不算标注" in text and "不能与标注人" in text and "source_kind" in text
    assert "要填定稿 final_label" in text
    assert len(rep.errors) == 4


def test_formal_check_requires_source_kind_column(lib, h, tmp_path):
    out, labelset, truth, _ = lib
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    old = tmp_path / "old.csv"
    with open(old, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=dataset.ALL_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    labels = dataset.load_labelset(labelset)
    with pytest.raises(dataset.DatasetError, match="source_kind"):
        dataset.read_labels(h.conn, old, labels)
    assert dataset.read_labels(h.conn, old, labels, require_review=False).ok  # 试标阶段照常读取


def test_grouped_split_keeps_tasks_and_duplicates_together(lib, h):
    out, labelset, truth, _ = lib
    rep = dataset.read_labels(h.conn, out, dataset.load_labelset(labelset))
    groups = dataset.source_groups(h.conn, rep.items)
    assert len(set(groups.values())) == 12  # 每个任务一组；同任务的两帧文字相同，也只会在同一组
    res = dataset.grouped_split(rep.items, groups, {"train": 0.5, "val": 0.25, "test": 0.25}, seed=1)
    for g in set(groups.values()):
        assert len({res.assignment[f] for f, gg in groups.items() if gg == g}) == 1
    assert sum(sum(c.values()) for c in res.counts.values()) == len(truth)
    with pytest.raises(dataset.DatasetError):
        dataset.grouped_split(rep.items, groups, {"train": 0.7, "val": 0.2}, seed=1)


def test_grouped_split_stratifies_class_held_in_big_groups():
    # 4 个 chat 大组（30 帧）+ 12 个其他类小组（20 帧）：只按总帧数分配时 chat 会全进 train
    items, groups = [], {}
    for g in range(16):
        label, n = ("chat", 30) if g < 4 else (("code", "web", "table_doc")[g % 3], 20)
        for k in range(n):
            fid = f"f{g:02d}-{k:02d}"
            items.append(dataset.Labeled(fid, label, f"t{g}", "x", "en", "app", "s"))
            groups[fid] = f"g{g:02d}"
    res = dataset.grouped_split(items, groups, {"train": 0.5, "val": 0.25, "test": 0.25}, seed=0)
    for s in ("train", "val", "test"):
        assert res.counts[s].get("chat"), (s, res.counts)
    assert not res.warnings or all("缺" not in w and "没有类别" not in w for w in res.warnings)


def test_duplicate_text_merges_groups_across_tasks(lib, h):
    out, labelset, truth, _ = lib
    rep = dataset.read_labels(h.conn, out, dataset.load_labelset(labelset))
    a, b = rep.items[0], rep.items[-1]
    h.conn.execute("UPDATE frame_text SET text_fold = (SELECT text_fold FROM frame_text WHERE frame_id = ?) "
                   "WHERE frame_id = ?", (a.frame_id, b.frame_id))
    groups = dataset.source_groups(h.conn, rep.items)
    assert groups[a.frame_id] == groups[b.frame_id]  # 不同任务的同一份文字也必须同组


def test_train_reports_classifier_and_both_baselines_on_test(lib, h, tmp_path):
    out, labelset, truth, _ = lib
    labels = dataset.load_labelset(labelset)
    rep = dataset.read_labels(h.conn, out, labels)
    res = dataset.grouped_split(rep.items, dataset.source_groups(h.conn, rep.items),
                                {"train": 0.5, "val": 0.25, "test": 0.25}, seed=3)
    split = res.manifest(labels, dataset.file_hash(out))
    clf, report = classify.train(h.conn, rep.items, split, labels, FakeImage.model_id)
    assert set(report["test"]) == {"classifier", "majority", "app_rule"}
    for m in report["test"].values():
        assert len(m["confusion_matrix"]) == len(report["labels"]) and m["zero_division"] == 0
    assert "table_doc" not in report["labels"]  # 没有样本的类别不进指标分母
    assert report["hyperparameter_search_on_val"]  # 超参数只在 val 上选
    assert report["chosen"]["C"] in classify.C_GRID
    # 合成数据可分：只用来确认流程，不是实验结论
    assert report["test"]["classifier"]["macro_f1"] >= report["test"]["majority"]["macro_f1"]

    clf.save(tmp_path / "model")
    again = classify.LinearClassifier.load(tmp_path / "model")
    ids, X = classify.load_features(h.conn, list(truth), FakeImage.model_id)
    assert again.predict(X)[0] == clf.predict(X)[0]
    assert again.card["feature_model_id"] == FakeImage.model_id and again.version == clf.version


def test_metrics_zero_division_and_macro_average():
    m = classify.metrics(["a", "a", "b"], ["a", "a", "a"], ["a", "b", "c"])
    assert m["per_class"]["b"]["f1"] == 0.0 and m["per_class"]["c"]["support"] == 0
    assert m["confusion_matrix"] == [[2, 0, 0], [1, 0, 0], [0, 0, 0]]
    assert abs(m["macro_f1"] - (0.8 + 0 + 0) / 3) < 1e-9


def test_app_rule_baseline_learns_only_from_training_apps():
    rule, info = classify.app_rule_baseline(["Code.exe", "Code.exe", "msedge.exe"], ["code", "code", "web"])
    assert rule(["code.exe", "msedge.exe", "unknown.exe"]) == ["code", "web", "code"]


def test_predictions_label_frames_filter_search_and_are_deleted_with_frame(lib, h, tmp_path):
    out, labelset, truth, _ = lib
    labels = dataset.load_labelset(labelset)
    rep = dataset.read_labels(h.conn, out, labels)
    split = dataset.grouped_split(rep.items, dataset.source_groups(h.conn, rep.items),
                                  {"train": 0.5, "val": 0.25, "test": 0.25}, seed=3).manifest(labels, "x")
    clf, _ = classify.train(h.conn, rep.items, split, labels, FakeImage.model_id)
    assert classify.predict_frames(h.conn, clf) == len(truth)
    assert set(classify.known_labels(h.conn)) <= set(COLORS)
    every = search.search(h.conn, search.parse_query('"task"'), limit=50)
    assert len(every.results) == len(truth)  # 默认不按类别过滤
    pick = classify.known_labels(h.conn)[0]
    only = search.search(h.conn, search.parse_query('"task"'), search.Filters(labels=(pick,)), limit=50)
    want = {r[0] for r in h.conn.execute("SELECT frame_id FROM frame_labels WHERE label = ?", (pick,))}
    assert {r.frame_id for r in only.results} == want and 0 < len(want) < len(truth)
    fid = next(iter(want))
    lifecycle.delete_frame(h.conn, h.cfg.data_dir, fid, confirmed_favorites=frozenset())
    assert h.conn.execute("SELECT COUNT(*) FROM frame_labels WHERE frame_id = ?", (fid,)).fetchone()[0] == 0


def test_embed_worker_writes_prediction_with_matching_feature_model(lib, h, tmp_path):
    out, labelset, truth, _ = lib
    labels = dataset.load_labelset(labelset)
    rep = dataset.read_labels(h.conn, out, labels)
    split = dataset.grouped_split(rep.items, dataset.source_groups(h.conn, rep.items),
                                  {"train": 0.5, "val": 0.25, "test": 0.25}, seed=3).manifest(labels, "x")
    clf, _ = classify.train(h.conn, rep.items, split, labels, FakeImage.model_id)
    w = EmbedWorker(h.cfg, threading.Event(), threading.Event(), logging.getLogger("t"),
                    text_encoder_factory=FakeText, image_encoder_factory=FakeImage, classifier=clf)
    fid = _add(h, _img(COLORS["web"], 999), "new web page", w)
    row = h.conn.execute("SELECT label, model_version FROM frame_labels WHERE frame_id = ?", (fid,)).fetchone()
    assert row is not None and row["model_version"] == clf.version

    clf.card["feature_model_id"] = "other-model@x"  # 特征模型不一致：不写预测，不混用向量版本
    fid2 = _add(h, _img(COLORS["code"], 1000), "new code", w)
    assert h.conn.execute("SELECT COUNT(*) FROM frame_labels WHERE frame_id = ?", (fid2,)).fetchone()[0] == 0


def test_embed_worker_backfills_old_frames_when_idle(lib, h, tmp_path):
    out, labelset, truth, _ = lib  # lib 建库时没有分类器：所有画面都没有预测
    labels = dataset.load_labelset(labelset)
    rep = dataset.read_labels(h.conn, out, labels)
    split = dataset.grouped_split(rep.items, dataset.source_groups(h.conn, rep.items),
                                  {"train": 0.5, "val": 0.25, "test": 0.25}, seed=3).manifest(labels, "x")
    clf, _ = classify.train(h.conn, rep.items, split, labels, FakeImage.model_id)
    assert h.conn.execute("SELECT COUNT(*) FROM frame_labels").fetchone()[0] == 0
    w = EmbedWorker(h.cfg, threading.Event(), threading.Event(), logging.getLogger("t"),
                    text_encoder_factory=FakeText, image_encoder_factory=FakeImage, classifier=clf)
    assert w.run_once(h.conn) is True  # 没有待办：空闲时补写一批
    assert {r[0] for r in h.conn.execute("SELECT frame_id FROM frame_labels WHERE model_version = ?",
                                         (clf.version,))} == set(truth)
    assert w.run_once(h.conn) is False  # 补完后不再重复写

    clf.card["version"] = "newer"  # 换模型后旧预测按新模型重写
    assert w.run_once(h.conn) is True
    assert h.conn.execute("SELECT COUNT(*) FROM frame_labels WHERE model_version = 'newer'").fetchone()[0] == len(truth)


def test_cli_refuses_training_after_labels_changed(lib, h, tmp_path):
    out, labelset, truth, _ = lib
    split = tmp_path / "split.json"
    common = ["--data-dir", str(h.cfg.data_dir)]
    assert dataset.main(common + ["split", "--labels", str(out), "--labelset", str(labelset),
                                  "--fractions", "0.5,0.25,0.25", "--out", str(split)]) == 0
    manifest = json.loads(split.read_text(encoding="utf-8"))
    assert manifest["fractions"] == {"train": 0.5, "val": 0.25, "test": 0.25} and len(manifest["frames"]) == len(truth)
    _rewrite(out, lambda rows: rows[0].update(change_reason="late edit"))
    rc = classify.main(common + ["train", "--labels", str(out), "--labelset", str(labelset), "--split", str(split),
                                 "--out", str(tmp_path / "m"), "--report", str(tmp_path / "r.json")])
    assert rc == 1 and not (tmp_path / "m").exists()
