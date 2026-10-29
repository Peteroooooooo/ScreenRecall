"""Query-encoder memory and first-query latency under different onnxruntime session options.

Each variant runs in a fresh child process: load the encoder, embed one synthetic query (cold), embed 20 more
(warm), unload, and report working set / private bytes at each stage. Text is synthetic.

    python experiments/search/encoder_memory.py [--out results/search/encoder-memory.json]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

VARIANTS = {
    "old: arena on, prepack on, 2 threads": {"mem_arena": True, "prepack": True, "threads": 2},
    "arena off": {"mem_arena": False, "prepack": True, "threads": 2},
    "arena off, prepack off": {"mem_arena": False, "prepack": False, "threads": 2},
    "arena off, prepack off, 1 thread": {"mem_arena": False, "prepack": False, "threads": 1},
    "arena off, prepack off, 4 threads": {"mem_arena": False, "prepack": False, "threads": 4},
}

CHILD = r"""
import gc, json, sys, time
sys.path.insert(0, ROOT)
import psutil
p = psutil.Process()
def mem():
    m = p.memory_full_info() if hasattr(p, 'memory_full_info') else p.memory_info()
    return {"ws_mib": round(m.rss / 2**20, 1), "private_mib": round(getattr(m, 'private', m.vms) / 2**20, 1)}
import numpy as np, onnxruntime, tokenizers
from screenrecall.text import encoder as E
out = {"base": mem()}
kw = json.loads(sys.argv[1])
t0 = time.perf_counter()
lazy = E.LazyTextEncoder(idle_unload_s=0, **kw)
lazy.encode(["where did I read about the finance sync on Thursday"])
out["first_query_ms"] = round((time.perf_counter() - t0) * 1000, 1)
out["loaded"] = mem()
ts = []
for i in range(20):
    t = time.perf_counter(); lazy.encode([f"synthetic query number {i} about coffee roasting"]); ts.append(time.perf_counter() - t)
ts.sort()
out["warm_ms_p50"] = round(ts[10] * 1000, 2)
out["after_warm"] = mem()
lazy.unload(); gc.collect()
out["unloaded"] = mem()
lazy.encode(["again"]); lazy.unload(); gc.collect()
out["unloaded_2nd_cycle"] = mem()
print(json.dumps(out))
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--variants", nargs="*")
    args = ap.parse_args()
    res = {}
    for name, kw in VARIANTS.items():
        if args.variants and name not in args.variants:
            continue
        r = subprocess.run([sys.executable, "-c", CHILD.replace("ROOT", repr(str(ROOT))), json.dumps(kw)],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=300)
        if r.returncode != 0:
            print(name, "FAILED", r.stderr[-800:])
            continue
        res[name] = json.loads(r.stdout.strip().splitlines()[-1])
        print(f"{name:40s} {res[name]}")
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
