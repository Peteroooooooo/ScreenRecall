"""End-to-end search eval on the synthetic set: real store (Writer), real idle-worker text step with the real
ONNX encoder, real SearchService and the real /api/v2/search handler. Everything runs in a fresh temp data dir
and temp SCREENRECALL_HOME that are deleted afterwards; no real screen content is involved.

Metrics (over the hit lines in the order the API returns them, groups first, then the lines shown per group):
- found@5: a relevant line is among the first 5 lines; recall@5: share of the relevant lines (max 5) in the first 5;
- MRR: 1 / rank of the first relevant line (0 when none in the first 50);
- FP (unrelated queries): any meaning-only group returned (match == "semantic"; a literal hit is what the user
  typed, so it is not counted); junk: groups without a relevant line, per related query;
- latency: first (cold) semantic query including encoder load, then warm p50 / p95.
Every number is reported for all queries and per split (tune / heldout, see evalset.assign_splits), by kind, and
for unrelated queries also by tag (offtopic / near / short / number).

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
UNRELATED_TAGS = ("offtopic", "near", "short", "number")


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


def measure(web, ctx, svc, queries, label_ids: dict[str, int], *, sort: str = "relevant", limit: int = 10):
    """Run every query through /api/v2/search; per-query metric rows + latencies (first query = cold)."""
    id_label = {v: k for k, v in label_ids.items()}
    supports_sort = "sort" in inspect.signature(svc.search).parameters
    rows, lat = [], []
    for q in queries:
        qp = {"q": q.q, "limit": str(limit)}
        if supports_sort:
            qp["sort"] = sort
        t0 = time.perf_counter()
        res = web.api_search(ctx, {"query": qp, "path": {}})
        lat.append((time.perf_counter() - t0) * 1000)
        groups = res["groups"]
        shown = [id_label.get(int(h["line_id"]), "?") for g in groups for h in g["hits"]]
        rel = set(q.rel)
        m = {"q": q.q, "kind": q.kind, "tag": q.tag, "split": q.split, "groups": len(groups),
             "semantic": res.get("semantic")}
        if q.kind == "unrelated":
            sem = [g for g in groups if g.get("match", "semantic") == "semantic"]
            m["fp"] = bool(sem)
            m["fp_groups"] = len(sem)
            m["fp_lines"] = [id_label.get(int(h["line_id"]), "?") for g in sem for h in g["hits"]][:3]
        else:
            first = next((j for j, x in enumerate(shown[:50]) if x in rel), None)
            m["found5"] = any(x in rel for x in shown[:5])
            m["recall5"] = len(rel & set(shown[:5])) / min(len(rel), 5)
            m["mrr"] = 0.0 if first is None else 1.0 / (first + 1)
            m["junk"] = sum(1 for g in groups
                            if not any(id_label.get(int(h["line_id"])) in rel for h in g["hits"]))
            m["top"] = shown[:3]
        rows.append(m)
    return rows, lat


def _rel_summary(ms: list[dict]) -> dict:
    return {"n": len(ms),
            "found@5": round(sum(m["found5"] for m in ms) / len(ms), 3),
            "recall@5": round(sum(m["recall5"] for m in ms) / len(ms), 3),
            "MRR": round(sum(m["mrr"] for m in ms) / len(ms), 3),
            "junk_groups": round(sum(m["junk"] for m in ms) / len(ms), 2)}


def _unrel_summary(ms: list[dict]) -> dict:
    return {"n": len(ms), "fp_rate": round(sum(m["fp"] for m in ms) / len(ms), 3),
            "fp_groups": round(sum(m["fp_groups"] for m in ms) / len(ms), 2)}


def summarize(rows: list[dict]) -> dict:
    """{kind: ..., all_related: ..., unrelated_by_tag: {...}, short_related: ...} for one set of rows."""
    out: dict = {}
    for k in KINDS:
        ms = [m for m in rows if m["kind"] == k]
        if ms:
            out[k] = _unrel_summary(ms) if k == "unrelated" else _rel_summary(ms)
    related = [m for m in rows if m["kind"] != "unrelated"]
    if related:
        out["all_related"] = _rel_summary(related)
    short = [m for m in related if m["tag"] == "short"]
    if short:
        out["short_related"] = _rel_summary(short)
    by_tag = {t: _unrel_summary(ms) for t in UNRELATED_TAGS
              if (ms := [m for m in rows if m["kind"] == "unrelated" and m["tag"] == t])}
    if by_tag:
        out["unrelated_by_tag"] = by_tag
    return out


def summarize_splits(rows: list[dict]) -> dict:
    return {"all": summarize(rows), **{s: summarize([m for m in rows if m["split"] == s]) for s in evalset.SPLITS}}


def run(args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="sr_search_eval_"))
    try:
        cfg, stack, svc, web, queries, label_ids = build(tmp)
        emb = embed(cfg, stack)
        ctx = web.V2Context(stack, tmp / "home")
        supports_sort = "sort" in inspect.signature(svc.search).parameters
        rows, lat = measure(web, ctx, svc, queries, label_ids, sort=args.sort, limit=args.limit)
        cold, warm = lat[0], sorted(lat[1:])
        out = {"label": args.label, "sort": args.sort if supports_sort else "api-default", "limit": args.limit,
               "corpus": evalset.stats(), "embed": emb, "summary": summarize_splits(rows),
               "latency_ms": {"cold_first": round(cold, 1), "warm_p50": round(statistics.median(warm), 1),
                              "warm_p95": round(warm[int(0.95 * (len(warm) - 1))], 1)},
               "queries": rows}
        svc.close()
        stack.close()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def print_summary(summary: dict) -> None:
    for split, s in summary.items():
        print(f" -- {split}")
        for k, v in s.items():
            print(f"  {k:16s} {v}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--sort", default="relevant", choices=["relevant", "recent"])
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None)
    ap.add_argument("--per-query", action="store_true", help="also write the per-query rows to --out")
    args = ap.parse_args()
    out = run(args)
    print(f"[{out['label']}] sort={out['sort']} embed={out['embed']} latency={out['latency_ms']}")
    print_summary(out["summary"])
    if args.out:
        if not args.per_query:
            out.pop("queries", None)  # results/ keeps numbers only
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
