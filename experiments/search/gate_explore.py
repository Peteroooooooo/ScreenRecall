"""Feature exploration for a query-adaptive vector gate (search2). Builds the synthetic store once (same as
run_eval.py: real Writer, real text step, real ONNX encoder, temp dirs deleted afterwards), then scores every
query against every stored vector and compares related vs unrelated queries under several normalisations,
including a hubness correction against a fixed set of generic probe queries (CSLS-style: a hit that is close to
many generic probes, such as "韩梅梅 10:07" or "向量检索入门", must stand out more). Numbers only; nothing is written.

The hit-level sweep is an approximation of the API (no literal branch, no grouping); run_eval.py / gate_sweep.py
measure the real path.

    python experiments/search/gate_explore.py [--split tune] [--cases]
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


def topk_mean(M: np.ndarray, k: int) -> np.ndarray:
    k = min(k, M.shape[1])
    return np.sort(M, axis=1)[:, -k:].mean(axis=1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="tune")
    ap.add_argument("--cases", action="store_true", help="list the high-scoring unrelated and low-scoring related")
    ap.add_argument("--fit", action="store_true", help="grid-fit a linear gate on --split, report the other split")
    ap.add_argument("--max-fp", type=float, default=0.03)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="sr_gate_explore_"))
    try:
        cfg, stack, svc, web, queries, label_ids = run_eval.build(tmp)
        run_eval.embed(cfg, stack)
        from screenrecall.store import schema
        from screenrecall.store.vectors import VectorStore
        from screenrecall.text.encoder import OnnxTextEncoder
        from screenrecall.v2.searchsvc import text_vectors_dir
        from screenrecall.store.gate import PROBES

        enc = OnnxTextEncoder(threads=4)
        vs = VectorStore(text_vectors_dir(cfg.data_dir), 384, readonly=True)
        segs = [s for s in vs._segments.values() if s.rows]
        V = np.concatenate([s.vec[s.alive] for s in segs])
        ids = np.concatenate([s.ids[s.alive] for s in segs])
        del segs  # drop the memory maps so the temp dir can be deleted on Windows
        db = sqlite3.connect(str(schema.db_path(cfg.data_dir)))
        members: dict[int, list[int]] = {}
        for cid, lid in db.execute("SELECT chunk_id, line_id FROM text_chunks"):
            members.setdefault(int(cid), []).append(int(lid))
        db.close()
        lid_label = {v: k for k, v in label_ids.items()}
        labs = [[lid_label.get(x, "?") for x in (members.get(int(i), []) if i < 0 else [int(i)])] for i in ids]
        P = enc.encode(PROBES)
        qs = [q for q in queries if q.split == args.split or args.split == "all"]
        QV = enc.encode([q.q for q in qs])
        S = QV @ V.T
        SP_q = QV @ P.T
        SP_h = V @ P.T
        out = {}
        for k in (1, 3, 5, 10):
            out[k] = (topk_mean(SP_q, k), topk_mean(SP_h, k))
        rows = []
        for qi, q in enumerate(qs):
            rel = set(q.rel)
            relmask = np.array([bool(set(lb) & rel) for lb in labs])
            rows.append({"q": q.q, "kind": q.kind, "tag": q.tag, "s": S[qi], "rel": relmask, "qi": qi})

        def evaluate(score_fn, accept_fn) -> dict:
            """hit-level approx: FP = any accepted hit; found = a relevant hit among the first 5 accepted."""
            fp, nu, ok, n, by = 0, 0, 0, 0, {}
            for r in rows:
                sc = score_fn(r)
                acc = accept_fn(r, sc)
                if r["kind"] == "unrelated":
                    nu += 1
                    fp += bool(acc.any())
                    by[r["tag"]] = by.get(r["tag"], 0) + bool(acc.any())
                    continue
                if r["kind"] not in SEM:
                    continue
                n += 1
                order = [j for j in np.argsort(-r["s"]) if acc[j]][:5]
                ok += any(r["rel"][j] for j in order)
            return {"found5": round(ok / n, 3), "fp": round(fp / nu, 3), "fp_by": by}

        def dist(name, vals):
            a = np.asarray(vals)
            print(f"  {name:28s} n={a.size:3d} min {a.min():.3f} p10 {np.percentile(a, 10):.3f} "
                  f"med {np.median(a):.3f} p90 {np.percentile(a, 90):.3f} max {a.max():.3f}")

        for k in (1, 3, 5, 10):
            rq, rh = out[k]
            print(f"== CSLS k={k}: 2s - r_q - r_h")
            c = lambda r, rq=rq, rh=rh: 2 * r["s"] - rq[r["qi"]] - rh  # noqa: E731
            dist("related best relevant", [c(r)[r["rel"]].max() for r in rows if r["kind"] in SEM and r["rel"].any()])
            dist("unrelated max", [c(r).max() for r in rows if r["kind"] == "unrelated"])
            print(f"== s - r_h k={k}")
            d = lambda r, rh=rh: r["s"] - rh  # noqa: E731
            dist("related best relevant", [d(r)[r["rel"]].max() for r in rows if r["kind"] in SEM and r["rel"].any()])
            dist("unrelated max", [d(r).max() for r in rows if r["kind"] == "unrelated"])

        print("\n== hit-level sweep (approximate)")
        print("  current max(0.45, top-0.15):", evaluate(lambda r: r["s"],
                                                       lambda r, s: s >= max(0.45, s.max() - 0.15)))
        for t in (0.55, 0.6, 0.65, 0.7):
            print(f"  raw >= {t}:", evaluate(lambda r: r["s"], lambda r, s, t=t: s >= t))
        for k in (3, 5, 10):
            rq, rh = out[k]
            for t in (0.4, 0.5, 0.55, 0.6, 0.65, 0.7):
                print(f"  csls k={k} >= {t}:", evaluate(lambda r, rq=rq, rh=rh: 2 * r["s"] - rq[r["qi"]] - rh,
                                                        lambda r, s, t=t: s >= t))
            for t in (0.2, 0.25, 0.3, 0.35):
                print(f"  s - r_h k={k} >= {t}:", evaluate(lambda r, rh=rh: r["s"] - rh, lambda r, s, t=t: s >= t))

        if args.fit:
            # linear gate s - a*r_q - b*r_h >= t, picked on this split at FP <= max_fp; the other split is reported
            other = [q for q in queries if q.split != args.split]
            QO = enc.encode([q.q for q in other])
            SO = QO @ V.T
            SPO = QO @ P.T
            orows = []
            for qi, q in enumerate(other):
                rel = set(q.rel)
                orows.append({"q": q.q, "kind": q.kind, "tag": q.tag, "s": SO[qi], "qi": qi,
                              "rel": np.array([bool(set(lb) & rel) for lb in labs])})
            res = []
            for k in (3, 5, 10):
                rq, rh = out[k]
                rqo = topk_mean(SPO, k)
                for a in (0.0, 0.5, 1.0, 1.5):
                    for b in (0.0, 0.5, 1.0, 1.5):
                        for t in np.arange(0.2, 0.9, 0.02):
                            r1 = evaluate(lambda r: r["s"] - a * rq[r["qi"]] - b * rh, lambda r, s, t=t: s >= t)
                            if r1["fp"] <= args.max_fp:
                                res.append((r1["found5"], k, a, b, round(float(t), 2), r1))
            res.sort(key=lambda x: -x[0])
            saved = rows[:]
            for found, k, a, b, t, r1 in res[:12]:
                rows[:] = orows
                rqo = topk_mean(SPO, k)
                rh = out[k][1]
                r2 = evaluate(lambda r: r["s"] - a * rqo[r["qi"]] - b * rh, lambda r, s, t=t: s >= t)
                rows[:] = saved
                print(f"  k={k} a={a} b={b} t={t}: {args.split} {r1}  other {r2}")

        if args.cases:
            rq, rh = out[5]
            for r in rows:
                c = 2 * r["s"] - rq[r["qi"]] - rh
                j = int(np.argmax(c))
                if r["kind"] == "unrelated" and c[j] >= 0.45:
                    print(f"  UNREL csls {c[j]:.3f} s {r['s'][j]:.3f} rq {rq[r['qi']]:.3f} rh {rh[j]:.3f} "
                          f"{r['tag']:8s} {r['q']!r} -> {labs[j]}")
                if r["kind"] in SEM and r["rel"].any():
                    jr = int(np.flatnonzero(r["rel"])[np.argmax(c[r["rel"]])])
                    if c[jr] < 0.6:
                        print(f"  REL   csls {c[jr]:.3f} s {r['s'][jr]:.3f} rq {rq[r['qi']]:.3f} rh {rh[jr]:.3f} "
                              f"{r['kind']} {r['q']!r} -> {labs[jr]}  (top csls {c[j]:.3f} {labs[j]})")
        vs.close()
        svc.close()
        stack.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
