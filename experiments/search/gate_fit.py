"""Does a richer per-hit feature set beat the plain hubness-corrected score? (search2 exploration)

Builds the synthetic store once (like run_eval.py; temp dirs deleted afterwards), scores every query against every
stored vector and fits a small logistic model on the tune split: positives = the best relevant unit of each related
query, negatives = the top 20 units of each unrelated query and the non-relevant units above the best relevant one.
The threshold is chosen on tune for FP <= --max-fp and the held-out split is reported (hit-level approximation of
the API: no literal branch, no grouping). Numbers only; nothing is written.

    python experiments/search/gate_fit.py
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import run_eval  # noqa: E402

SEM = ("paraphrase", "xling", "typo")

# candidate extension of store.gate.PROBES: more generic topics plus generic screen text (chat name + time,
# window chrome, dates), none taken from the eval corpus or the eval queries
EXTRA_PROBES = [
    "how to repaint a bedroom wall", "history of the printing press", "beginner watercolor techniques",
    "how volcanoes form islands", "migrating birds in autumn", "learning to juggle", "tips for growing a beard",
    "rules of cricket", "building a treehouse", "the life of honeybees", "renting a kayak", "glass blowing",
    "how submarines dive", "bonsai tree care", "ancient Greek theatre", "the Great Wall of China",
    "how to brew kombucha", "a guide to bird watching", "making candles at home", "jigsaw puzzle strategies",
    "怎么给墙刷漆", "印刷术的历史", "水彩画入门", "候鸟迁徙", "学杂技", "板球规则", "搭树屋", "划皮划艇",
    "吹玻璃", "盆景养护", "古希腊戏剧", "长城的历史", "自制蜡烛", "拼图技巧", "观鸟指南", "剪纸艺术",
    "张伟 09:15", "Alice 3:42 PM", "王芳 14:20", "Bob 11:05", "李娜 08:30", "Tom 22:10",
    "New tab", "Back", "Close", "Minimize", "Untitled", "Loading...", "Home", "Menu",
    "2025-03-14", "Wednesday", "星期三", "下午 3:15", "第 2 页，共 5 页", "100%", "共 12 项",
]


def cjk_share(t: str) -> float:
    from screenrecall.store.fts.docs import is_cjk

    letters = [c for c in t if c.isalpha()]
    return sum(is_cjk(c) for c in letters) / len(letters) if letters else 0.0


def topk_mean(M: np.ndarray, k: int) -> np.ndarray:
    return np.sort(M, axis=1)[:, -min(k, M.shape[1]):].mean(axis=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-fp", type=float, default=0.03)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="sr_gate_fit_"))
    try:
        cfg, stack, svc, web, queries, label_ids = run_eval.build(tmp)
        run_eval.embed(cfg, stack)
        from screenrecall.store import schema
        from screenrecall.store.gate import PROBES
        from screenrecall.store.vectors import VectorStore
        from screenrecall.text.encoder import OnnxTextEncoder
        from screenrecall.v2.searchsvc import text_vectors_dir

        enc = OnnxTextEncoder(threads=4)
        vs = VectorStore(text_vectors_dir(cfg.data_dir), 384, readonly=True)
        segs = [s for s in vs._segments.values() if s.rows]
        V = np.concatenate([s.vec[s.alive] for s in segs])
        ids = np.concatenate([s.ids[s.alive] for s in segs])
        db = sqlite3.connect(str(schema.db_path(cfg.data_dir)))
        text_of = {int(a): str(b) for a, b in db.execute("SELECT line_id, text FROM lines")}
        members: dict[int, list[int]] = {}
        for cid, lid in db.execute("SELECT chunk_id, line_id FROM text_chunks ORDER BY chunk_id, line_id"):
            members.setdefault(int(cid), []).append(int(lid))
        db.close()
        lid_label = {v: k for k, v in label_ids.items()}
        mem = [members.get(int(i), []) if i < 0 else [int(i)] for i in ids]
        labs = [[lid_label.get(x, "?") for x in m] for m in mem]
        utext = ["\n".join(text_of.get(x, "") for x in m) for m in mem]
        ucjk = np.array([cjk_share(t) for t in utext])
        n = len(ids)
        sets = [set(m) for m in mem]
        overlap = np.array([[i != j and bool(sets[i] & sets[j]) for j in range(n)] for i in range(n)])
        P = enc.encode(PROBES)
        rh = topk_mean(V @ P.T, 5)
        PX = enc.encode(list(PROBES) + EXTRA_PROBES)
        rhx = {k: topk_mean(V @ PX.T, k) for k in (3, 5, 10)}
        is_chunk = (ids < 0).astype(float)
        QV = enc.encode([q.q for q in queries])
        S = QV @ V.T
        rq = topk_mean(QV @ P.T, 5)

        def feats(qi: int) -> np.ndarray:
            s = S[qi]
            partner = np.where(overlap.any(axis=1), np.where(overlap, s[None, :], -1).max(axis=1), s)
            q = queries[qi].q
            short = float(len(q.split()) <= 2 and len(q) <= 12)
            cross = np.abs(cjk_share(q) - ucjk)
            return np.stack([s, np.full(n, rq[qi]), rh, s - s.max(), partner, is_chunk,
                             np.full(n, short), cross], axis=1)

        F = [feats(qi) for qi in range(len(queries))]
        names = ["s", "r_q", "r_h", "s-top", "partner", "chunk", "short_q", "cross_script"]

        def train_set(split):
            X, y = [], []
            for qi, q in enumerate(queries):
                if q.split != split:
                    continue
                s = S[qi]
                order = np.argsort(-s)
                if q.kind == "unrelated":
                    X += [F[qi][j] for j in order[:20]]
                    y += [0] * 20
                elif q.kind in SEM:
                    rel = set(q.rel)
                    relj = [j for j in order if set(labs[j]) & rel]
                    if not relj:
                        continue
                    X.append(F[qi][relj[0]])
                    y.append(1)
                    for j in order:
                        if j == relj[0]:
                            break
                        X.append(F[qi][j])
                        y.append(0)
            return np.array(X), np.array(y, float)

        def fit(X, y, cols, l2=1e-2, iters=4000, lr=0.5):
            Xc = X[:, cols]
            mu, sd = Xc.mean(0), Xc.std(0) + 1e-9
            Z = np.c_[(Xc - mu) / sd, np.ones(len(Xc))]
            w = np.zeros(Z.shape[1])
            pos = y.mean()
            sw = np.where(y > 0, 0.5 / pos, 0.5 / (1 - pos))
            for _ in range(iters):
                p = 1 / (1 + np.exp(-Z @ w))
                g = Z.T @ (sw * (p - y)) / len(y) + l2 * np.r_[w[:-1], 0]
                w -= lr * g
            return lambda Xq: np.c_[(Xq[:, cols] - mu) / sd, np.ones(len(Xq))] @ w, w

        def evaluate(split, score, t):
            fp, nu, ok, nr, by = 0, 0, 0, 0, {}
            for qi, q in enumerate(queries):
                if q.split != split:
                    continue
                g = score(qi)
                acc = g >= t
                if q.kind == "unrelated":
                    nu += 1
                    fp += bool(acc.any())
                    by[q.tag] = by.get(q.tag, 0) + bool(acc.any())
                elif q.kind in SEM:
                    nr += 1
                    rel = set(q.rel)
                    top = [j for j in np.argsort(-S[qi]) if acc[j]][:5]
                    ok += any(set(labs[j]) & rel for j in top)
            return round(ok / nr, 3), round(fp / nu, 3), by

        def pick(score):
            # the loosest threshold that keeps tune FP <= max_fp: just above the (allowed+1)-th highest
            # unrelated-query maximum
            maxes = sorted((float(score(qi).max()) for qi, q in enumerate(queries)
                            if q.split == "tune" and q.kind == "unrelated"), reverse=True)
            allowed = int(np.floor(args.max_fp * len(maxes) + 1e-9))
            t = maxes[allowed] + 1e-6 if allowed < len(maxes) else -np.inf
            return evaluate("tune", score, t)[0], t

        X, y = train_set("tune")
        variants = {
            "raw s": None,
            "s - r_h": None,
            "s - .5 r_q - r_h": None,
            "s - r_h extra k=3": None,
            "s - r_h extra k=5": None,
            "s - r_h extra k=10": None,
            "logit s,r_q,r_h": [0, 1, 2],
            "logit +s-top": [0, 1, 2, 3],
            "logit +partner": [0, 1, 2, 4],
            "logit +chunk,short": [0, 1, 2, 5, 6],
            "logit +cross": [0, 1, 2, 7],
            "logit all": list(range(len(names))),
        }
        for name, cols in variants.items():
            if name == "raw s":
                score = lambda qi: S[qi]  # noqa: E731
            elif name == "s - r_h":
                score = lambda qi: S[qi] - rh  # noqa: E731
            elif name == "s - .5 r_q - r_h":
                score = lambda qi: S[qi] - 0.5 * rq[qi] - rh  # noqa: E731
            elif name.startswith("s - r_h extra"):
                kk = int(name.split("k=")[1])
                score = lambda qi, kk=kk: S[qi] - rhx[kk]  # noqa: E731
            else:
                fn, w = fit(X, y, cols)
                score = lambda qi, fn=fn: fn(F[qi])  # noqa: E731
            b = pick(score)
            if b is None:
                print(f"  {name:22s} no threshold meets FP <= {args.max_fp} on tune")
                continue
            ft, t = b
            fh, fph, by = evaluate("heldout", score, t)
            extra = "" if cols is None else "  w=" + ", ".join(f"{names[c]} {x:+.2f}" for c, x in zip(cols, w))
            print(f"  {name:22s} tune found5 {ft:.3f} | heldout found5 {fh:.3f} fp {fph:.3f} {by}{extra}")
        vs.close()
        svc.close()
        stack.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
