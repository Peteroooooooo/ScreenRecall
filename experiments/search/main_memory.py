"""Main-process view of semantic search memory and latency, old settings vs new.

Builds a temp data dir with the synthetic eval corpus and its vectors (real worker text step), then for each
setting starts a fresh child process that behaves like the main process: import the app modules, open a
SearchService, and measure working set / private bytes and the latency of the first semantic query, with
and without the search-page warm-up. Temp dirs are deleted afterwards. Only synthetic text is used.

    python experiments/search/main_memory.py [--out results/search/main-memory.json]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

CHILD = r"""
import json, sys, time, gc
sys.path.insert(0, ROOT)
import screenrecall
import psutil
from screenrecall.v2 import web  # what main imports (numpy, store, searchsvc)
from screenrecall.v2.searchsvc import SearchService
p = psutil.Process()
def mem():
    m = p.memory_full_info()
    return {"ws_mib": round(m.rss / 2**20, 1), "private_mib": round(m.private / 2**20, 1)}
cfg = json.loads(sys.argv[1])
svc = SearchService(DATA, idle_unload_s=0, threads=2)
svc._enc_kwargs["mem_arena"] = cfg["mem_arena"]
out = {"idle": mem()}
if cfg["warm"]:
    # the page calls /search/warm when it opens; the user types for a second or more before searching
    t = time.perf_counter(); svc.warm(); out["warm_call_ms"] = round((time.perf_counter() - t) * 1000, 1)
    svc._warming.join(); out["warm_load_ms"] = round((time.perf_counter() - t) * 1000, 1)
t = time.perf_counter(); r = svc.search("budget meeting", limit=10, sort="relevant")
out["query_ms"] = round((time.perf_counter() - t) * 1000, 1)
out["semantic"] = r["semantic"]
out["loaded"] = mem()
svc._encoder.unload(); gc.collect()
out["unloaded"] = mem()
print(json.dumps(out))
"""


def build() -> Path:
    from run_eval import build as build_store, embed

    tmp = Path(tempfile.mkdtemp(prefix="sr_search_mem_"))
    cfg, stack, svc, _web, _q, _ids = build_store(tmp)
    embed(cfg, stack)
    svc.close()
    stack.close()
    return tmp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    args = ap.parse_args()
    tmp = build()
    data = tmp / "data"
    settings = {
        "old (OpenBLAS 16 threads, arena on), cold first query": ({"OPENBLAS_NUM_THREADS": "16"}, {"mem_arena": True, "warm": False}),
        "new (OpenBLAS 4 threads, arena off), cold first query": ({}, {"mem_arena": False, "warm": False}),
        "new, after search-page warm-up": ({}, {"mem_arena": False, "warm": True}),
    }
    res = {}
    try:
        for name, (env_over, cfg) in settings.items():
            env = {k: v for k, v in os.environ.items() if k != "OPENBLAS_NUM_THREADS"}
            env.update(env_over)
            env["SCREENRECALL_HOME"] = str(tmp / "home")
            code = CHILD.replace("ROOT", repr(str(ROOT))).replace("DATA", repr(str(data)))
            r = subprocess.run([sys.executable, "-c", code, json.dumps(cfg)], capture_output=True, text=True,
                               cwd=str(ROOT), env=env, timeout=300)
            if r.returncode != 0:
                print(name, "FAILED", r.stderr[-1500:])
                continue
            res[name] = json.loads(r.stdout.strip().splitlines()[-1])
            print(f"{name:55s} {res[name]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
