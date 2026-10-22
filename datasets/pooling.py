"""事后补充判定（pooling）：检查冻结真值有没有漏标的相关帧，正式结果不变，另报一份"复判后"数字。

为什么要做：真值是出题时在来源里翻图定的，同一条消息 / 同一段代码可能还出现在别的帧（聊天列表预览、
滚动前后），出题时没翻到就成了漏标，检索把它排在前面反而被算错。IR 评测的标准做法是把各系统前 K
的结果合成一个池，由人重新判定。

规则（写进报告）：
- 池 = 五组实验在每条查询上的前 depth 名（默认 5，对应 Hit@5）里、原真值之外的帧，dev 与 blind 都做；
  对所有实验一视同仁，不只查失败的题。
- 判定时不显示是哪组实验、排第几（池内每条查询的顺序按固定种子打乱），只看查询、原判定依据和原图。
- 两人各自独立判（judge = xiao / cao），标准沿用每条查询的 rationale：原图里能看到依据所说的内容才算相关。
  两人都判"相关"才并入；有分歧的列出来，当面定稿（resolved 列）后才并入。
- 补充真值只用于"复判后"那一列；正式结果文件、冻结的 queries.json 都不改。检索不重跑：五组的前 10 名
  已经在正式结果 JSON 里，复判后的分数直接用它们重算。

    python datasets/pooling.py build --depth 5 --out results/pooling/pool.json
    （判定页）streamlit run tools/pool_app.py --server.port 8504
    python datasets/pooling.py score --pool results/pooling/pool.json --out results/pooling
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ROOT / "datasets/v1/queries.json"
RUNS = {"dev": ROOT / "results/retrieval/20261022-dev/results.json",
        "blind": ROOT / "results/retrieval/20261022-blind/results.json"}
EXPS = ["exp1_lexical", "exp2_semantic", "exp3_image", "exp4_rrf_text", "exp5_rrf_three"]
JUDGES = ("xiao", "cao")
VERDICTS = ("relevant", "not_relevant", "unsure")
SEED = 20261022


def load_runs() -> dict[str, dict]:
    out = {}
    for split, p in RUNS.items():
        for q in json.loads(p.read_text(encoding="utf-8"))["per_query"]:
            out[q["id"]] = q
    return out


def build(depth: int) -> dict:
    queries = {q["id"]: q for q in json.loads(QUERIES.read_text(encoding="utf-8"))}
    runs = load_runs()
    rng = random.Random(SEED)
    items = []
    for qid in sorted(runs):
        rel = set(queries[qid]["relevant"])
        cand = sorted({f for e in EXPS for f in runs[qid]["results"][e]["top10"][:depth]} - rel)
        rng.shuffle(cand)
        items += [{"query_id": qid, "frame_id": f} for f in cand]
    return {"depth": depth, "seed": SEED, "queries_file": str(QUERIES.relative_to(ROOT)),
            "n_pairs": len(items), "items": items}


def judgments_path(pool_path: Path, judge: str) -> Path:
    return pool_path.parent / f"judgments-{judge}.csv"


def read_judgments(path: Path) -> dict[tuple[str, str], str]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return {(r["query_id"], r["frame_id"]): r["verdict"] for r in csv.DictReader(fh)}


def read_resolved(path: Path) -> dict[tuple[str, str], str]:
    """resolved.csv：两人分歧当面定稿（query_id, frame_id, verdict, note）。"""
    return read_judgments(path)


def kappa(a: list[str], b: list[str]) -> float | None:
    n = len(a)
    if not n:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return None if pe == 1 else (po - pe) / (1 - pe)


def metrics(ranked: list[str], rel: set[str]) -> tuple[int, float]:
    rank = next((i + 1 for i, f in enumerate(ranked) if f in rel), None)
    return int(rank is not None and rank <= 5), (1.0 / rank if rank is not None and rank <= 10 else 0.0)


def score(pool_path: Path, out_dir: Path, judges: tuple[str, ...] = JUDGES) -> dict:
    """judges：参与判定的人。两人时一致或定稿才并入；只有一人时（AI 预筛池由一人复核）以其判定为准。"""
    pool = json.loads(pool_path.read_text(encoding="utf-8"))
    pairs = [(it["query_id"], it["frame_id"]) for it in pool["items"]]
    J = {j: read_judgments(judgments_path(pool_path, j)) for j in judges}
    resolved = read_resolved(pool_path.parent / "resolved.csv")
    done = [p for p in pairs if all(p in J[j] for j in judges)]
    disagree = [p for p in done if len({J[j][p] for j in judges}) > 1 and p not in resolved]
    added = defaultdict(set)
    for p in done:
        votes = {J[j][p] for j in judges}
        v = resolved.get(p) or (votes.pop() if len(votes) == 1 else None)
        if v == "relevant":
            added[p[0]].add(p[1])
    queries = {q["id"]: q for q in json.loads(QUERIES.read_text(encoding="utf-8"))}
    runs = load_runs()
    table = {}
    for split in RUNS:
        ids = [qid for qid in runs if queries[qid]["split"] == split]
        row = {}
        for e in EXPS:
            off = [metrics(runs[q]["results"][e]["top10"], set(queries[q]["relevant"])) for q in ids]
            aud = [metrics(runs[q]["results"][e]["top10"], set(queries[q]["relevant"]) | added[q]) for q in ids]
            row[e] = {"n": len(ids),
                      "official": {"hits": sum(h for h, _ in off), "mrr10": sum(m for _, m in off) / len(ids)},
                      "audited": {"hits": sum(h for h, _ in aud), "mrr10": sum(m for _, m in aud) / len(ids)}}
        table[split] = row
    two = len(judges) == 2
    both = [p for p in pairs if all(p in J[j] for j in judges)] if two else []
    a, b = (judges + judges)[:2]
    rep = {
        "pool": {"depth": pool["depth"], "pairs": len(pairs), "judged_by_all": len(done), "judges": list(judges),
                 "progress": {j: len(J[j]) for j in judges}, "method": pool.get("method", "")},
        "agreement": {"cohen_kappa": kappa([J[a][p] for p in both], [J[b][p] for p in both]) if two else None,
                      "raw_agreement": (sum(J[a][p] == J[b][p] for p in both) / len(both)) if both else None,
                      "unresolved_disagreements": len(disagree)},
        "added_relevant": {q: sorted(v) for q, v in sorted(added.items())},
        "metrics": table,
        "complete": len(done) == len(pairs) and not disagree,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "audit.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / "disagreements.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["query_id", "frame_id", "xiao", "cao", "verdict", "note"])
        for q, f in disagree:
            w.writerow([q, f, J["xiao"][(q, f)], J["cao"][(q, f)], "", ""])
    lines = ["# 事后补充判定（pooling）", "",
             (f"预筛方法：{pool['method']}\n\n" if pool.get("method") else "") +
             f"池：{len(pairs)} 对（来自五组实验前 {pool['depth']} 名中原真值之外的帧）；判定人 {'、'.join(judges)}，"
             f"都已判 {len(done)} 对；"
             f"未定稿分歧 {len(disagree)} 对；Cohen's κ = {rep['agreement']['cohen_kappa']}。"
             + ("" if rep["complete"] else " **尚未完成，下表不能写进报告。**"), "",
             "正式 = 冻结真值；复判后 = 冻结真值 + 两人一致（或定稿）判为相关的池内帧。命中数 / 查询数 · MRR@10。", ""]
    for split, row in table.items():
        lines += [f"## {split}", "", "| 实验 | 正式 | 复判后 |", "| --- | --- | --- |"]
        for e, v in row.items():
            o, a = v["official"], v["audited"]
            lines.append(f"| {e} | {o['hits']}/{v['n']} · {o['mrr10']:.2f} | {a['hits']}/{v['n']} · {a['mrr10']:.2f} |")
        lines.append("")
    (out_dir / "audit.md").write_text("\n".join(lines), encoding="utf-8")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--depth", type=int, default=5)
    b.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("score")
    s.add_argument("--pool", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--judges", default="xiao,cao", help="参与判定的人，逗号分隔；AI 预筛池由一人复核时写 xiao")
    a = ap.parse_args()
    if a.cmd == "build":
        if a.out.exists():
            print(f"错误：{a.out} 已存在（判定按它进行，不覆盖）", file=sys.stderr)
            return 1
        pool = build(a.depth)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"池 {pool['n_pairs']} 对 → {a.out}")
        return 0
    rep = score(a.pool, a.out, tuple(x.strip() for x in a.judges.split(",") if x.strip()))
    print(json.dumps({k: rep[k] for k in ("pool", "agreement", "complete")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
