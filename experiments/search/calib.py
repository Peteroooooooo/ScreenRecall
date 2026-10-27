"""Score calibration sweep for single-line (+ short chunk) vectors on the synthetic eval set.

For each rule it reports: share of related queries (paraphrase/xling/typo) with a relevant line accepted in the
top 5, false-positive rate (unrelated queries with anything accepted) and the mean number of accepted
non-relevant units per related query.

    python experiments/search/calib.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import evalset  # noqa: E402
from explore import SEM_KINDS, units  # noqa: E402
from screenrecall.text.encoder import OnnxTextEncoder  # noqa: E402

ANCHORS = [
    # generic background sentences (topics absent from the eval corpus), zh + en
    "The weather forecast says it will be sunny tomorrow.", "I need to buy a new pair of running shoes.",
    "The museum is closed on Mondays.", "She plays the piano every evening.", "The train was delayed by ten minutes.",
    "Please close the door when you leave.", "The children are playing in the park.", "He fixed the bike chain himself.",
    "We watched a documentary about whales.", "The bakery opens at seven in the morning.",
    "明天可能会下雨，记得带伞。", "这家店的饺子很好吃。", "他每天早上跑步五公里。", "图书馆周一闭馆。", "我们周末去看了电影。",
    "这个手机壳是蓝色的。", "孩子们在操场上踢球。", "公交车晚点了十分钟。", "她在学习弹吉他。", "楼下新开了一家花店。",
    "OK", "好的", "Thanks!", "收到", "Settings", "Search", "File Edit View Help", "12:30", "确定", "取消",
]


def main() -> None:
    segs, queries = evalset.load()
    enc = OnnxTextEncoder(threads=4)
    qv = enc.encode([q.q for q in queries])
    A = enc.encode(ANCHORS)
    for v in ["line", "line+chunk2", "line+chunk3"]:
        us = units(segs, v)
        U = enc.encode([t for t, _ in us])
        S = qv @ U.T
        SA = qv @ A.T
        print(f"\n######## {v} ({len(us)} units)")
        if v == "line":
            for qi, q in enumerate(queries):
                if q.kind != "unrelated":
                    continue
                o = np.argsort(-S[qi])[:3]
                print(f"  unrelated {q.q!r}: " + " | ".join(f"{S[qi, j]:.2f} {us[j][0][:30]!r}" for j in o)
                      + f"   anchors p90 {np.percentile(SA[qi], 90):.2f} max {SA[qi].max():.2f}")
            for qi, q in enumerate(queries):
                if q.kind not in SEM_KINDS:
                    continue
                s = S[qi]
                rel = set(q.rel)
                best = max(float(s[j]) for j in range(len(us)) if set(us[j][1]) & rel)
                o = np.argsort(-s)
                rank = next(i for i, j in enumerate(o) if set(us[j][1]) & rel)
                if rank > 2 or best < 0.5:
                    print(f"  weak {q.kind} {q.q!r}: best rel {best:.2f} rank {rank}  top "
                          + " | ".join(f"{s[j]:.2f} {us[j][0][:28]!r}" for j in o[:2])
                          + f"  mean {s.mean():.2f} anchors max {SA[qi].max():.2f}")

        def rule_eval(accept) -> tuple[float, float, float]:
            ok, fp, junk, n, nu = 0, 0, 0.0, 0, 0
            for qi, q in enumerate(queries):
                s = S[qi]
                acc = accept(qi, s)
                if q.kind == "unrelated":
                    nu += 1
                    fp += bool(acc.any())
                    continue
                if q.kind not in SEM_KINDS:
                    continue
                n += 1
                rel = set(q.rel)
                idx = [j for j in np.argsort(-s) if acc[j]]
                top = idx[:5]
                ok += any(set(us[j][1]) & rel for j in top)
                junk += sum(1 for j in idx if not (set(us[j][1]) & rel))
            return ok / n, fp / nu, junk / n

        print("  rule                          found@5  FP    junk/q")
        for t in (0.35, 0.40, 0.45, 0.50, 0.55):
            r = rule_eval(lambda qi, s, t=t: s >= t)
            print(f"  abs >= {t:.2f}                   {r[0]:.3f}   {r[1]:.2f}  {r[2]:.1f}")
        for g in (0.20, 0.25, 0.30, 0.35):
            r = rule_eval(lambda qi, s, g=g: (s - s.mean()) >= g)
            print(f"  s - corpus mean >= {g:.2f}       {r[0]:.3f}   {r[1]:.2f}  {r[2]:.1f}")
        for g in (0.05, 0.08, 0.10, 0.12, 0.15):
            r = rule_eval(lambda qi, s, g=g: s >= SA[qi].max() + g)
            print(f"  s >= anchors max + {g:.2f}       {r[0]:.3f}   {r[1]:.2f}  {r[2]:.1f}")
        for g in (0.10, 0.15, 0.20):
            r = rule_eval(lambda qi, s, g=g: s >= np.percentile(SA[qi], 90) + g)
            print(f"  s >= anchors p90 + {g:.2f}       {r[0]:.3f}   {r[1]:.2f}  {r[2]:.1f}")
        for t, g in ((0.40, 0.05), (0.40, 0.08), (0.42, 0.08), (0.45, 0.05)):
            r = rule_eval(lambda qi, s, t=t, g=g: (s >= t) & (s >= SA[qi].max() + g))
            print(f"  abs >= {t:.2f} & anchors max+{g:.2f}  {r[0]:.3f}   {r[1]:.2f}  {r[2]:.1f}")
        for t, d in ((0.40, 0.10), (0.40, 0.15), (0.45, 0.10)):
            r = rule_eval(lambda qi, s, t=t, d=d: (s >= t) & (s >= s.max() - d))
            print(f"  abs >= {t:.2f} & top-{d:.2f}         {r[0]:.3f}   {r[1]:.2f}  {r[2]:.1f}")


if __name__ == "__main__":
    main()
