"""内容分类器（方案 6.4、详细方案 5.3 / 7.2）：python -m screenrecall.classify <子命令>

  train    在冻结的全图向量上训练 Logistic Regression；只用 val（开发）选超参数，test 只在最后评一次。
           同时报告多数类基线与"只用应用 / 进程名"的规则基线（规则只从 train 学）。
  predict  用训练产物给库里已有全图向量的画面写预测标签（页面显示与主动筛选用，默认不过滤搜索）

训练产物是纯数据（npz 系数 + JSON 说明），不用 pickle；说明里记录特征模型、预处理版本、划分清单与超参数。
没有真实标注数据时不要运行 train 来"产出"结果：合成数据只用于测试流程。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import dataset, db, models, vectors
from .timeutil import utc_ms

C_GRID = (0.01, 0.1, 1.0, 10.0)
CLASS_WEIGHTS = (None, "balanced")


# ---------------- 特征 ----------------

def load_features(conn: sqlite3.Connection, frame_ids: list[str], model_id: str,
                  preprocess: str = models.IMAGE_PREPROCESS) -> tuple[list[str], np.ndarray]:
    rows = conn.execute(
        "SELECT target_id, dim, vector FROM embeddings WHERE target_kind = 'frame' AND branch = 'image' "
        "AND model_id = ? AND preprocess_version = ? AND target_id IN (SELECT value FROM json_each(?))",
        (model_id, preprocess, json.dumps(frame_ids)),
    ).fetchall()
    got = {r["target_id"]: vectors.from_blob(r["vector"], r["dim"]) for r in rows}
    kept = [f for f in frame_ids if f in got]
    X = np.vstack([got[f] for f in kept]).astype(np.float32) if kept else np.zeros((0, 0), dtype=np.float32)
    return kept, X


# ---------------- 指标 ----------------

def metrics(y_true: list[str], y_pred: list[str], labels: list[str]) -> dict:
    """逐类 P / R / F1 与 Macro F1；零分母一律记 0（事先约定），混淆矩阵行为真实、列为预测。"""
    idx = {lb: i for i, lb in enumerate(labels)}
    cm = np.zeros((len(labels), len(labels)), dtype=int)
    for t, p in zip(y_true, y_pred):
        cm[idx[t], idx[p]] += 1
    per = {}
    for lb, i in idx.items():
        tp, fp, fn = cm[i, i], cm[:, i].sum() - cm[i, i], cm[i, :].sum() - cm[i, i]
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[lb] = {"precision": float(prec), "recall": float(rec), "f1": float(f1), "support": int(cm[i, :].sum())}
    n = len(y_true)
    return {
        "n": n,
        "accuracy": float(np.trace(cm) / n) if n else 0.0,
        "macro_f1": float(np.mean([per[lb]["f1"] for lb in labels])) if labels else 0.0,
        "per_class": per,
        "labels": labels,
        "confusion_matrix": cm.tolist(),
        "zero_division": 0,
    }


# ---------------- 基线 ----------------

def majority_baseline(train_y: list[str]):
    top = Counter(train_y).most_common(1)[0][0]
    return lambda apps: [top] * len(apps), {"majority_label": top}


def app_rule_baseline(train_apps: list[str], train_y: list[str]):
    """每个应用 / 进程名映射到 train 里它最常见的类别；没见过的应用用多数类。"""
    top = Counter(train_y).most_common(1)[0][0]
    by_app: dict[str, Counter] = {}
    for a, y in zip(train_apps, train_y):
        by_app.setdefault((a or "").lower(), Counter())[y] += 1
    rule = {a: c.most_common(1)[0][0] for a, c in by_app.items()}
    return lambda apps: [rule.get((a or "").lower(), top) for a in apps], {"rule": rule, "fallback": top}


# ---------------- 模型 ----------------

@dataclass
class LinearClassifier:
    classes: list[str]
    coef: np.ndarray        # (k, d) 或二分类时 (1, d)
    intercept: np.ndarray
    card: dict

    @property
    def version(self) -> str:
        return self.card["version"]

    def proba(self, X: np.ndarray) -> np.ndarray:
        z = X @ self.coef.T + self.intercept
        if z.shape[1] == 1:  # 二分类：sklearn 只给正类一列
            z = np.hstack([np.zeros_like(z), z])
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def predict(self, X: np.ndarray) -> tuple[list[str], np.ndarray]:
        p = self.proba(X)
        return [self.classes[i] for i in p.argmax(axis=1)], p.max(axis=1)

    def save(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        np.savez(out_dir / "model.npz", coef=self.coef, intercept=self.intercept)
        (out_dir / "card.json").write_text(json.dumps(self.card, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, out_dir: Path) -> "LinearClassifier":
        card = json.loads((out_dir / "card.json").read_text(encoding="utf-8"))
        with np.load(out_dir / "model.npz", allow_pickle=False) as z:
            return cls(card["classes"], z["coef"], z["intercept"], card)


def _fit(X, y, C, class_weight, seed):
    from sklearn.linear_model import LogisticRegression

    m = LogisticRegression(C=C, class_weight=class_weight, max_iter=5000, random_state=seed)
    m.fit(X, y)
    return m


def train(conn: sqlite3.Connection, items: list[dataset.Labeled], split: dict, labels: list[str],
          feature_model_id: str, *, seed: int = 0) -> tuple[LinearClassifier, dict]:
    """返回（模型, 报告）。超参数只按 val 的 Macro F1 选；最终模型用 train+val 重训后在 test 评一次。"""
    frames = split["frames"]
    by_id = {it.frame_id: it for it in items if it.frame_id in frames}
    kept, X = load_features(conn, list(by_id), feature_model_id)
    missing = sorted(set(by_id) - set(kept))
    pos = {f: i for i, f in enumerate(kept)}

    def part(name):
        ids = [f for f in kept if frames[f]["split"] == name]
        return ids, X[[pos[f] for f in ids]] if ids else np.zeros((0, X.shape[1] if X.size else 0)), \
            [by_id[f].label for f in ids], [by_id[f].app for f in ids]

    tr_ids, Xtr, ytr, atr = part("train")
    va_ids, Xva, yva, ava = part("val")
    te_ids, Xte, yte, ate = part("test")
    if len(set(ytr)) < 2:
        raise dataset.DatasetError("train 至少要有两个类别的样本")
    used = [lb for lb in labels if lb in set(ytr) | set(yva) | set(yte)]

    search = []
    if va_ids:
        for C in C_GRID:
            for cw in CLASS_WEIGHTS:
                m = _fit(Xtr, ytr, C, cw, seed)
                search.append({"C": C, "class_weight": cw,
                               "val_macro_f1": metrics(yva, list(m.predict(Xva)), used)["macro_f1"]})
        best = max(search, key=lambda r: (r["val_macro_f1"], -abs(np.log10(r["C"]))))
    else:
        best = {"C": 1.0, "class_weight": None, "val_macro_f1": None}
    final_X = np.vstack([Xtr, Xva]) if va_ids else Xtr
    final_y = ytr + yva
    m = _fit(final_X, final_y, best["C"], best["class_weight"], seed)

    card = {
        "classes": [str(c) for c in m.classes_],
        "feature_model_id": feature_model_id,
        "feature_preprocess": models.IMAGE_PREPROCESS,
        "hyperparameters": {"C": best["C"], "class_weight": best["class_weight"], "max_iter": 5000, "seed": seed},
        "split_manifest": {"fractions": split.get("fractions"), "seed": split.get("seed"),
                           "labels_csv_blake2b": split.get("labels_csv_blake2b")},
        "trained_on": {"train": len(ytr), "val": len(yva)},
        "created_at_ms": utc_ms(),
    }
    card["version"] = hashlib.blake2b(json.dumps(card, sort_keys=True).encode(), digest_size=8).hexdigest()
    clf = LinearClassifier(card["classes"], m.coef_.astype(np.float32), m.intercept_.astype(np.float32), card)

    report = {
        "labels": used,
        "counts": {s: dict(Counter(y)) for s, y in (("train", ytr), ("val", yva), ("test", yte))},
        "missing_features": len(missing),
        "hyperparameter_search_on_val": search,
        "chosen": best,
        "model_version": card["version"],
    }
    maj, maj_info = majority_baseline(ytr + yva)
    rule, rule_info = app_rule_baseline(atr + ava, ytr + yva)
    report["baselines"] = {"majority": maj_info, "app_rule": {"n_apps": len(rule_info["rule"]),
                                                              "fallback": rule_info["fallback"]}}
    for name, ids, Xs, ys, apps in (("val", va_ids, Xva, yva, ava), ("test", te_ids, Xte, yte, ate)):
        if not ids:
            continue
        pred, _ = clf.predict(Xs)
        report[name] = {
            "classifier": metrics(ys, pred, used),
            "majority": metrics(ys, maj(apps), used),
            "app_rule": metrics(ys, rule(apps), used),
        }
    report["note"] = ("val 用于选择超参数（开发集）；test 只评一次。val 指标来自用 train 训练的候选，"
                      "表中 val 行是用 train+val 重训后的最终模型，只作参考，结论以 test 为准。")
    return clf, report


# ---------------- 预测写库 ----------------

def predict_frames(conn: sqlite3.Connection, clf: LinearClassifier, frame_ids: list[str] | None = None) -> int:
    """给有对应全图向量的画面写预测标签（覆盖旧模型的预测）。返回写入数。"""
    if frame_ids is None:
        frame_ids = [r[0] for r in conn.execute("SELECT frame_id FROM frames WHERE image_status != 'deleted'")]
    kept, X = load_features(conn, frame_ids, clf.card["feature_model_id"], clf.card["feature_preprocess"])
    if not kept:
        return 0
    labels, scores = clf.predict(X)
    now = utc_ms()
    with db.write_txn(conn):
        alive = {r[0] for r in conn.execute(
            "SELECT frame_id FROM frames WHERE image_status != 'deleted' AND frame_id IN (SELECT value FROM json_each(?))",
            (json.dumps(kept),))}
        rows = [(f, lb, float(s), clf.version, now) for f, lb, s in zip(kept, labels, scores) if f in alive]
        conn.executemany(
            "INSERT INTO frame_labels (frame_id, label, score, model_version, created_at_ms) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (frame_id) DO UPDATE SET label = excluded.label, score = excluded.score, "
            "model_version = excluded.model_version, created_at_ms = excluded.created_at_ms",
            rows,
        )
    return len(rows)


def write_prediction(conn: sqlite3.Connection, clf: LinearClassifier, frame_id: str, vec: np.ndarray) -> None:
    """在调用方事务内给一帧写预测（向量 worker 写回全图向量时顺带调用）。"""
    labels, scores = clf.predict(vec.reshape(1, -1).astype(np.float32))
    conn.execute(
        "INSERT INTO frame_labels (frame_id, label, score, model_version, created_at_ms) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (frame_id) DO UPDATE SET label = excluded.label, score = excluded.score, "
        "model_version = excluded.model_version, created_at_ms = excluded.created_at_ms",
        (frame_id, labels[0], float(scores[0]), clf.version, utc_ms()),
    )


def frames_needing_prediction(conn: sqlite3.Connection, clf: LinearClassifier, limit: int = 200) -> list[str]:
    """有当前特征模型的全图向量、但没有本模型预测的画面（老数据、换模型后、分类器后来才配置）。"""
    return [r[0] for r in conn.execute(
        "SELECT f.frame_id FROM frames f "
        "JOIN embeddings e ON e.target_kind = 'frame' AND e.target_id = f.frame_id AND e.branch = 'image' "
        "AND e.model_id = ? AND e.preprocess_version = ? "
        "LEFT JOIN frame_labels l ON l.frame_id = f.frame_id "
        "WHERE f.image_status != 'deleted' AND (l.frame_id IS NULL OR l.model_version != ?) "
        "ORDER BY f.frame_id LIMIT ?",
        (clf.card["feature_model_id"], clf.card["feature_preprocess"], clf.version, limit),
    )]


def load_for(cfg) -> LinearClassifier | None:
    """按配置加载分类器；特征模型与当前全图模型不一致时不加载（不能混用不同版本的向量）。"""
    path = getattr(getattr(cfg, "classify", None), "model_dir", "")
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        from .config import REPO_ROOT
        p = REPO_ROOT / p
    if not (p / "card.json").exists():
        return None
    clf = LinearClassifier.load(p)
    if clf.card["feature_model_id"] != models.image_model_id(cfg.models):
        return None
    return clf


def known_labels(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT label FROM frame_labels ORDER BY label")]


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m screenrecall.classify", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--labels", required=True)
    t.add_argument("--labelset", required=True)
    t.add_argument("--split", required=True, help="dataset split 生成的划分清单")
    t.add_argument("--out", required=True, help="模型目录（model.npz + card.json）")
    t.add_argument("--report", required=True, help="评测报告 JSON")
    t.add_argument("--seed", type=int, default=0)
    pr = sub.add_parser("predict")
    pr.add_argument("--model", required=True)
    args = ap.parse_args(argv)
    cfg = load_config(args.data_dir)
    conn = db.connect(cfg.db_path)
    try:
        if args.cmd == "train":
            labels = dataset.load_labelset(Path(args.labelset))
            rep = dataset.read_labels(conn, Path(args.labels), labels)
            if not rep.ok:
                for e in rep.errors:
                    print("错误：" + e, file=sys.stderr)
                return 1
            split = json.loads(Path(args.split).read_text(encoding="utf-8"))
            if split.get("labels_csv_blake2b") != dataset.file_hash(Path(args.labels)):
                print("错误：标注文件在划分之后改过，请重新 split（不能在看过结果后改标注）", file=sys.stderr)
                return 1
            clf, report = train(conn, rep.items, split, labels, models.image_model_id(cfg.models), seed=args.seed)
            clf.save(Path(args.out))
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            te = report.get("test", {})
            print(f"模型 {clf.version} 已保存到 {args.out}；test Macro F1："
                  + ", ".join(f"{k} {v['macro_f1']:.3f}" for k, v in te.items()) if te else "没有 test 划分")
        else:
            n = predict_frames(conn, LinearClassifier.load(Path(args.model)))
            print(f"写入 {n} 条预测标签")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
