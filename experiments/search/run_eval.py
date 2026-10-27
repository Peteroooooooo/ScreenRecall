"""End-to-end search eval on the synthetic set: real store (Writer), real idle-worker text step with the real
ONNX encoder, real SearchService and the real /api/v2/search handler. Everything runs in a fresh temp data dir
and temp SCREENRECALL_HOME that are deleted afterwards; no real screen content is involved.

Metrics (over the hit lines in the order the API returns them, groups first, then the lines shown per group):
- found@5: a relevant line is among the first 5 lines; recall@5: share of the relevant lines (max 5) in the first 5;
- MRR: 1 / rank of the first relevant line (0 when none in the first 50);
- FP (unrelated queries): any group returned; junk: groups without a relevant line, per related query;
- latency: first (cold) semantic query including encoder load, then warm p50 / p95.

    python experiments/search/run_eval.py --label after [--sort relevant|recent] [--out results/search/x.json]
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evalset  # noqa: E402

KINDS = ("exact", "partial", "paraphrase", "xling", "typo", "unrelated")


def build(tmp: Path):
    os.environ["SCREENRECALL_HOME"] = str(tmp / "home")
    os.environ.pop("SCREENRECALL_DATA_DIR", None)
    from screenrecall.store.writer import LineIn
    from screenrecall.timeutil import utc_ms
    from screenrecall.v2 import web
    from screenrecall.v2.adapters import StoreStack
    from screenrecall.v2.config import load_v2
    from screenrecall.v2.searchsvc import SearchService

    cfg = load_v2(tmp / "data", home=tmp / "home")
    svc = SearchService(cfg.data_dir, idle_unload_s=0, threads=cfg.encoder.threads, batch_size=cfg.encoder.batch_size)
    stack = StoreStack(cfg, search=svc)
    w = stack.writer
    segs, queries = evalset.load()
    now = utc_ms()
    label_ids: dict[str, int] = {}
    for s in segs:
        t0 = now - int(s.age_h * 3_600_000)
        sid = w.segment_for(s.app, s.title, t0).result(10)
        n0 = 0
        for bi, block in enumerate(s.blocks):
            t = t0 + bi * 1_000
            b = w.block_new(sid, t).result(10)
            lines = [LineIn(x, 10, 40 + i * 30, 900, 60 + i * 30) for i, x in enumerate(block)]
            ids = w.record_lines(b, t, (0, 60 + len(block) * 30), lines).result(10)
            for i, lid in enumerate(ids):
                label_ids[f"{s.key}:{n0 + i}"] = int(lid)
            n0 += len(block)
    w.flush()
    return cfg, stack, svc, web, queries, label_ids


def embed(cfg, stack) -> dict:
    from screenrecall.text.encoder import OnnxTextEncoder
    from screenrecall.v2.work_common import Ctx, StopFlag
    from screenrecall.v2.work_text import TextVectorStep

    ctx = Ctx(cfg, stack.writer, StopFlag())
    step = TextVectorStep(ctx, lambda: OnnxTextEncoder(threads=cfg.idle.encoder_threads,
                                                       batch_size=cfg.encoder.batch_size))
    t0 = time.perf_counter()
    n = step.run()
    secs = time.perf_counter() - t0
    vs = step.vs
    vs.refresh()
    out = {"lines": n, "seconds": round(secs, 2), "vectors": len(vs), **ctx.stat("text").extra}
    step.close()
    ctx.close()
    stack.writer.flush()
    return out


def run(args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="sr_search_eval_"))
    try:
        cfg, stack, svc, web, queries, label_ids = build(tmp)
        emb = embed(cfg, stack)
        ctx = web.V2Context(stack, tmp / "home")
        id_label = {v: k for k, v in label_ids.items()}
        supports_sort = "sort" in inspect.signature(svc.search).parameters
        per_kind: dict[str, list[dict]] = {k: [] for k in KINDS}
        lat: list[float] = []
        cold = None
        rows = []
        for i, q in enumerate(queries):
            qp = {"q": q.q, "limit": str(args.limit)}
            if supports_sort:
                qp["sort"] = args.sort
            t0 = time.perf_counter()
            res = web.api_search(ctx, {"query": qp, "path": {}})
            dt = (time.perf_counter() - t0) * 1000
            if i == 0:
                cold = dt
            else:
                lat.append(dt)
            groups = res["groups"]
            shown = [id_label.get(int(h["line_id"]), "?") for g in groups for h in g["hits"]]
            rel = set(q.rel)
            m = {"q": q.q, "kind": q.kind, "groups": len(groups), "semantic": res.get("semantic")}
            if q.kind == "unrelated":
                m["fp"] = bool(groups)
                m["fp_lines"] = shown[:3]
            else:
                first = next((j for j, x in enumerate(shown[:50]) if x in rel), None)
                m["found5"] = any(x in rel for x in shown[:5])
                m["recall5"] = len(rel & set(shown[:5])) / min(len(rel), 5)
                m["mrr"] = 0.0 if first is None else 1.0 / (first + 1)
                m["junk"] = sum(1 for g in groups
                                if not any(id_label.get(int(h["line_id"])) in rel for h in g["hits"]))
                m["top"] = shown[:3]
            per_kind[q.kind].append(m)
            rows.append(m)
        summary: dict = {}
        related = [m for k in KINDS if k != "unrelated" for m in per_kind[k]]
        for name, ms in list(per_kind.items()) + [("all_related", related)]:
            if not ms:
                continue
            if name == "unrelated":
                summary[name] = {"n": len(ms), "fp_rate": round(sum(m["fp"] for m in ms) / len(ms), 3)}
            else:
                summary[name] = {"n": len(ms),
                                 "found@5": round(sum(m["found5"] for m in ms) / len(ms), 3),
                                 "recall@5": round(sum(m["recall5"] for m in ms) / len(ms), 3),
                                 "MRR": round(sum(m["mrr"] for m in ms) / len(ms), 3),
                                 "junk_groups": round(sum(m["junk"] for m in ms) / len(ms), 2)}
        lat.sort()
        out = {"label": args.label, "sort": args.sort if supports_sort else "api-default", "limit": args.limit,
               "corpus": evalset.stats(), "embed": emb, "summary": summary,
               "latency_ms": {"cold_first": round(cold or 0, 1), "warm_p50": round(statistics.median(lat), 1),
                              "warm_p95": round(lat[int(0.95 * (len(lat) - 1))], 1)},
               "queries": rows}
        svc.close()
        stack.close()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--sort", default="relevant", choices=["relevant", "recent"])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None)
    ap.add_argument("--per-query", action="store_true", help="also write the per-query rows to --out")
    args = ap.parse_args()
    out = run(args)
    s = out["summary"]
    print(f"[{out['label']}] sort={out['sort']} embed={out['embed']} latency={out['latency_ms']}")
    for k, v in s.items():
        print(f"  {k:12s} {v}")
    if args.out:
        if not args.per_query:
            out.pop("queries", None)  # results/ keeps numbers only
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
