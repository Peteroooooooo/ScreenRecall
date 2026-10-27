"""Vector-only exploration on the synthetic eval set (no store, no worker): which text units to embed and how
to calibrate the score. Prints, per variant, recall@5 / MRR over the semantic query kinds and the score
distributions of related vs unrelated queries. Used to pick the design that run_eval.py then measures end to end.

    python experiments/search/explore.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evalset  # noqa: E402
from screenrecall.text.encoder import OnnxTextEncoder  # noqa: E402

SEM_KINDS = ("paraphrase", "xling", "typo")


def units(segs, variant: str) -> list[tuple[str, list[str]]]:
    """(text, [labels]) for a variant. Labels are 'seg:n'."""
    out = []
    for s in segs:
        n0 = 0
        for b in s.blocks:
            ids = [f"{s.key}:{n0 + i}" for i in range(len(b))]
            n0 += len(b)
            parts = variant.split("+")
            for p in parts:
                if p == "line":
                    out += [(t, [i]) for t, i in zip(b, ids)]
                elif p.startswith("chunk"):
                    k = int(p[5:])
                    for j in range(0, len(b), k):
                        out.append(("\n".join(b[j:j + k]), ids[j:j + k]))
                elif p.startswith("win"):
                    k = int(p[3:])
                    h = k // 2
                    for j in range(len(b)):
                        lo, hi = max(0, j - h), min(len(b), j + h + 1)
                        out.append(("\n".join(b[lo:hi]), [ids[j]] + [x for x in ids[lo:hi] if x != ids[j]]))
    return out


def main() -> None:
    segs, queries = evalset.load()
    t0 = time.perf_counter()
    enc = OnnxTextEncoder(threads=4)
    print(f"encoder load {time.perf_counter() - t0:.2f}s")
    qv = enc.encode([q.q for q in queries])
    # anchors: the corpus mean direction (a query-specific baseline)
    variants = ["chunk6", "chunk3", "line", "line+chunk2", "line+chunk3", "win3", "line+win3", "line+chunk3+chunk6"]
    cache: dict[str, np.ndarray] = {}
    for v in variants:
        us = units(segs, v)
        texts = [t for t, _ in us]
        need = [t for t in dict.fromkeys(texts) if t not in cache]
        if need:
            for t, vec in zip(need, enc.encode(need)):
                cache[t] = vec
        U = np.stack([cache[t] for t in texts])
        S = qv @ U.T  # [q, units]
        rec, mrr, n = 0.0, 0.0, 0
        rel_top, unrel_top, z_rel, z_unrel, gap_rel, gap_unrel = [], [], [], [], [], []
        per_kind: dict[str, list[float]] = {}
        for qi, q in enumerate(queries):
            s = S[qi]
            order = np.argsort(-s)
            mu, sd = float(s.mean()), float(s.std()) + 1e-6
            if q.kind == "unrelated":
                unrel_top.append(float(s[order[0]]))
                z_unrel.append((float(s[order[0]]) - mu) / sd)
                gap_unrel.append(float(s[order[0]] - mu))
                continue
            if q.kind not in SEM_KINDS:
                continue
            seen: list[str] = []
            for j in order:
                for lab in us[j][1][:1] if v.startswith("win") or "+win" in v else us[j][1]:
                    if lab not in seen:
                        seen.append(lab)
                if len(seen) >= 30:
                    break
            rel = set(q.rel)
            top5 = seen[:5]
            r = len(rel & set(top5)) / min(len(rel), 5)
            first = next((i for i, x in enumerate(seen) if x in rel), None)
            m = 0.0 if first is None else 1.0 / (first + 1)
            rec += r
            mrr += m
            n += 1
            per_kind.setdefault(q.kind, []).append(r)
            best_rel = max(float(s[j]) for j in range(len(us)) if set(us[j][1][:1] if "win" in v else us[j][1]) & rel)
            rel_top.append(best_rel)
            z_rel.append((best_rel - mu) / sd)
            gap_rel.append(best_rel - mu)
        def q5(a):
            a = np.asarray(a)
            return f"min {a.min():.2f} p10 {np.percentile(a, 10):.2f} med {np.median(a):.2f} max {a.max():.2f}"
        print(f"\n== {v}: {len(us)} units  recall@5 {rec / n:.3f}  MRR {mrr / n:.3f}  "
              + "  ".join(f"{k} {np.mean(x):.2f}" for k, x in per_kind.items()))
        print(f"   best relevant score : {q5(rel_top)}")
        print(f"   unrelated top score : {q5(unrel_top)}")
        print(f"   best relevant z     : {q5(z_rel)}")
        print(f"   unrelated top z     : {q5(z_unrel)}")
        print(f"   relevant  - mean    : {q5(gap_rel)}")
        print(f"   unrelated - mean    : {q5(gap_unrel)}")


if __name__ == "__main__":
    main()
