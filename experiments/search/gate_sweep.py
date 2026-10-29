"""Pick the vector-gate thresholds (store.gate.GateConfig) on the real search path, tune split only, and report
the held-out split for the picked settings. Builds the synthetic store once like run_eval.py (real Writer, real
text step, real ONNX encoder, real SearchService and /api/v2/search; temp dirs deleted afterwards).

Selection: among settings with tune unrelated FP <= --max-fp, the highest tune found@5 over all related queries
(ties: MRR, then the stricter setting). Also prints the best tune setting at a few other FP budgets, so the
trade-off is visible. Numbers only (no per-query rows).

    python experiments/search/gate_sweep.py [--max-fp 0.03] [--out results/search/gate-sweep.json]
"""
from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

import run_eval  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-fp", type=float, default=0.03)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="sr_gate_sweep_"))
    try:
        cfg, stack, svc, web, queries, label_ids = run_eval.build(tmp)
        run_eval.embed(cfg, stack)
        from screenrecall.store.gate import PROBES, GateConfig, VectorGate

        ctx = web.V2Context(stack, tmp / "home")
        tune = [q for q in queries if q.split == "tune"]
        held = [q for q in queries if q.split == "heldout"]
        run_eval.measure(web, ctx, svc, tune[:1], label_ids)  # loads the model
        probes = np.asarray(svc._get_encoder().encode(list(PROBES)), dtype=np.float32)

        def score(gc: GateConfig | None, qs) -> dict:
            svc.gate_config = gc
            svc._gate = None if gc is None else VectorGate(probes, gc)
            rows, _ = run_eval.measure(web, ctx, svc, qs, label_ids)
            return run_eval.summarize(rows)

        def brief(s: dict) -> dict:
            r = s["all_related"]
            return {"found@5": r["found@5"], "MRR": r["MRR"], "fp": s["unrelated"]["fp_rate"],
                    "junk_groups": r["junk_groups"]}

        results = []
        grid = itertools.product((3, 5, 10), np.round(np.arange(0.26, 0.56, 0.02), 2), (0.10, 0.15, 0.20, 1.0),
                                 (0.0, 0.05, 0.10))
        for k, floor, margin, lit in grid:
            gc = GateConfig(floor=float(floor), margin=float(margin), floor_with_literal=float(floor + lit), k=k)
            results.append((gc, brief(score(gc, tune))))
        out: dict = {"max_fp": args.max_fp, "settings_tried": len(results), "budgets": {}}
        old = {"tune": brief(score(None, tune)), "heldout": brief(score(None, held))}
        out["fixed_rule"] = old
        print(f"fixed rule (before): {old}")
        for budget in sorted({0.0, args.max_fp, 0.05, 0.10}):
            ok = [(gc, r) for gc, r in results if r["fp"] <= budget + 1e-9]
            if not ok:
                continue
            gc, r = max(ok, key=lambda x: (x[1]["found@5"], x[1]["MRR"], x[0].floor))
            h = score(gc, held)
            row = {"config": gc.__dict__, "tune": r, "heldout": brief(h)}
            out["budgets"][str(budget)] = row
            print(f"tune FP <= {budget:.2f}: {gc} tune {r} | heldout {brief(h)}")
        if args.out:
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        svc.close()
        stack.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
