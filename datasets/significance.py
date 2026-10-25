"""五组检索实验的配对显著性（只读正式结果 JSON，不重跑检索）。

- Hit@5：同一批查询上两组实验的成败配对，精确 McNemar（只看不一致的查询：A 中 B 不中 / B 中 A 不中，
  二项分布 p=0.5 双侧）。
- MRR@10：配对 bootstrap（按查询重抽样 10,000 次，固定种子）给差值的 95% 区间。
- 分 dev、blind 各算一次；开发集阶段没有调任何规则（见 PROGRESS 冻结记录），所以也给 dev+blind 合并（60 条）。
  合并只是增大样本，不代替分开报告。

    python datasets/significance.py --out results/retrieval/20261022-significance.md
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = {"dev": ROOT / "results/retrieval/20261022-dev/results.json",
        "blind": ROOT / "results/retrieval/20261022-blind/results.json"}
NAMES = {"exp1_lexical": "Exp1 词法", "exp2_semantic": "Exp2 语义", "exp3_image": "Exp3 画面",
         "exp4_rrf_text": "Exp4 文字 RRF", "exp5_rrf_three": "Exp5 三路 RRF"}
PAIRS = [("exp2_semantic", "exp1_lexical"), ("exp4_rrf_text", "exp1_lexical"), ("exp4_rrf_text", "exp2_semantic"),
         ("exp5_rrf_three", "exp4_rrf_text"), ("exp4_rrf_text", "exp3_image")]
B = 10_000
SEED = 20261022


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def boot_ci(diffs: list[float], rng: random.Random) -> tuple[float, float]:
    n = len(diffs)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(B))
    return means[int(0.025 * B)], means[int(0.975 * B) - 1]


def load(splits: list[str]) -> list[dict]:
    out = []
    for s in splits:
        out += json.loads(RUNS[s].read_text(encoding="utf-8"))["per_query"]
    return out


def table(per_query: list[dict], rng: random.Random) -> list[str]:
    n = len(per_query)
    lines = ["| 对比（A vs B） | Hit@5 A / B | A 中 B 不中 | B 中 A 不中 | McNemar p | MRR@10 差 A−B | 95% 区间 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for a, b in PAIRS:
        ha = [p["results"][a]["hit5"] for p in per_query]
        hb = [p["results"][b]["hit5"] for p in per_query]
        only_a = sum(x and not y for x, y in zip(ha, hb))
        only_b = sum(y and not x for x, y in zip(ha, hb))
        d = [p["results"][a]["rr10"] - p["results"][b]["rr10"] for p in per_query]
        lo, hi = boot_ci(d, rng)
        lines.append(f"| {NAMES[a]} vs {NAMES[b]} | {sum(ha)}/{n} / {sum(hb)}/{n} | {only_a} | {only_b} | "
                     f"{mcnemar_exact(only_a, only_b):.3f} | {sum(d) / n:+.3f} | [{lo:+.3f}, {hi:+.3f}] |")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rng = random.Random(SEED)
    lines = ["# 检索实验配对显著性（正式结果，冻结真值）", "",
             f"Hit@5 用精确 McNemar（双侧）；MRR@10 用配对 bootstrap（{B} 次，种子 {SEED}）。"
             "p ≥ 0.05 或区间跨 0 表示这批查询上看不出可靠差异，不等于没有差异。"
             "Exp3 是画面模式，在文字题上低是预期，对比只作参照。", ""]
    for title, splits in (("dev（30 条）", ["dev"]), ("blind（30 条）", ["blind"]),
                          ("dev + blind 合并（60 条，开发阶段未调规则）", ["dev", "blind"])):
        lines += [f"## {title}", ""] + table(load(splits), rng) + [""]
    a.out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
