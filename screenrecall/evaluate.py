"""五组检索实验（方案 6.2、详细方案 7.2）：python -m screenrecall.evaluate --queries Q.json --split dev|blind|all --out DIR

  Exp 1 词法（含短串回退）   Exp 2 文本语义   Exp 3 全图（画面）   Exp 4 词法 + 语义 RRF（产品文字模式）
  Exp 5 词法 + 语义 + 全图三路 RRF（**仅离线实验**，不进产品）

所有实验共用同一份底库、显式筛选与引号条件，候选先合并到帧，每帧只占一个名次。只有引号原串的查询
按产品统一规则走同一条路径（硬过滤后按时间），不为调用模型补造描述。指标：Hit@5、MRR@10，按整体、
四场景、中英文分别给出命中数 / 查询数。各方法不移除任何查询；无结果、失败都计 0 并保留在逐条结果里。

查询文件（JSON 列表，盲测前冻结；文件指纹写进结果）每项：
  id, split ("dev"/"blind"), scenario, language ("zh"/"en"), mode ("text"/"image"，产品演示时用的模式),
  query, start / end（可选，本地时间 "YYYY-MM-DD HH:MM"）, apps（可选）, case_sensitive（可选）,
  relevant（相关 frame_id 列表）, rationale（判定依据）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db, retrieval, search

EXPERIMENTS = {
    "exp1_lexical": "词法（含短串回退）",
    "exp2_semantic": "文本语义",
    "exp3_image": "全图（画面）",
    "exp4_rrf_text": "词法 + 语义 RRF（产品文字模式）",
    "exp5_rrf_three": "三路 RRF（仅离线实验）",
}
TOP = 10


@dataclass
class Query:
    id: str
    split: str
    scenario: str
    language: str
    mode: str
    query: str
    relevant: list[str]
    start: str | None = None
    end: str | None = None
    apps: tuple[str, ...] = ()
    case_sensitive: bool = False
    rationale: str = ""

    def filters(self) -> search.Filters:
        def ms(s):
            return int(datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp() * 1000) if s else None
        return search.Filters(ms(self.start), ms(self.end), tuple(self.apps))


def load_queries(path: Path) -> list[Query]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for i, q in enumerate(raw):
        try:
            out.append(Query(
                id=str(q["id"]), split=q["split"], scenario=q["scenario"], language=q["language"],
                mode=q.get("mode", "text"), query=q["query"], relevant=list(q["relevant"]),
                start=q.get("start"), end=q.get("end"), apps=tuple(q.get("apps", ())),
                case_sensitive=bool(q.get("case_sensitive", False)), rationale=q.get("rationale", ""),
            ))
        except KeyError as exc:
            raise ValueError(f"第 {i + 1} 条查询缺少字段 {exc}") from None
    ids = [q.id for q in out]
    if len(ids) != len(set(ids)):
        raise ValueError("查询 id 重复")
    return out


def protocol_check(queries: list[Query]) -> list[str]:
    """对照方案的查询设计（40 条：开发 / 盲测各 20，每侧每场景 5 条，整体中英各半）。只提示，不改查询。"""
    notes = []
    by = defaultdict(int)
    for q in queries:
        by[(q.split, q.scenario)] += 1
        if not q.relevant:
            notes.append(f"{q.id} 没有相关帧：主集只放有答案的查询，无答案样例要单独编号报告")
        if q.split not in ("dev", "blind"):
            notes.append(f"{q.id} 的 split 应为 dev 或 blind")
        if q.mode not in (retrieval.TEXT, retrieval.IMAGE):
            notes.append(f"{q.id} 的 mode 应为 text 或 image")
    if len(queries) != 40:
        notes.append(f"共 {len(queries)} 条查询，方案主集为 40 条")
    for (sp, sc), n in sorted(by.items()):
        if n != 5:
            notes.append(f"{sp} / {sc} 有 {n} 条，方案为每侧每场景 5 条")
    langs = defaultdict(int)
    for q in queries:
        langs[q.language] += 1
    if len(queries) == 40 and (langs.get("zh"), langs.get("en")) != (20, 20):
        notes.append(f"语言分布 {dict(langs)}，方案为中英各 20 条")
    return notes


# ---------------- 各实验的排名 ----------------

def rankings(conn: sqlite3.Connection, q: Query, engines: retrieval.Engines, *, rrf_k: int = 60,
             branch_top: int = 50) -> dict[str, list[str]]:
    """返回每个实验的前 TOP 个帧（各分支同一候选集合）。失败的分支返回空列表并在 errors 里记原因。"""
    parsed = search.parse_query(q.query)
    filters = q.filters()
    out: dict[str, list[str]] = {}
    if parsed.error or parsed.is_empty:
        return {e: [] for e in EXPERIMENTS} | {"_error": [parsed.error or "空查询"]}
    if not parsed.terms:  # 只有引号原串：所有实验同一条产品路径（硬过滤后按时间）
        base = search.search(conn, parsed, filters, case_sensitive=q.case_sensitive, limit=TOP)
        ids = [r.frame_id for r in base.results]
        img_ok = {r[0] for r in conn.execute("SELECT frame_id FROM frames WHERE image_status = 'ok'")}
        return {e: ([f for f in ids if f in img_ok] if e == "exp3_image" else ids) for e in EXPERIMENTS}

    allowed, occs, req = retrieval._allowed_frames(conn, parsed, filters, q.case_sensitive)
    if (req is not None and not req) or not allowed:
        return {e: [] for e in EXPERIMENTS}
    desc = " ".join(parsed.terms)
    errors = []

    lexical = search.search(conn, parsed, filters, case_sensitive=q.case_sensitive, limit=branch_top)
    lex = [r.frame_id for r in lexical.results if r.matched_terms and r.frame_id in allowed]

    sem: list[str] = []
    if engines.text_encoder is not None and engines.text_index is not None:
        engines.text_index.refresh(conn)
        sem = [f for f, _, _ in engines.text_index.rank(engines.text_encoder([desc])[0], allowed, branch_top)]
    else:
        errors.append("文本语义模型不可用")

    img: list[str] = []
    if engines.clip_text_encoder is not None and engines.image_index is not None:
        ok = {r[0] for r in conn.execute("SELECT frame_id FROM frames WHERE image_status = 'ok'") if r[0] in allowed}
        engines.image_index.refresh(conn)
        img = [f for f, _ in engines.image_index.rank(engines.clip_text_encoder([desc])[0], ok, branch_top)]
    else:
        errors.append("画面模型不可用")

    text_resp = retrieval.run(conn, parsed, filters, mode=retrieval.TEXT, engines=engines,
                              case_sensitive=q.case_sensitive, limit=TOP, rrf_k=rrf_k, branch_top=branch_top)
    fused3 = retrieval.rrf([lex, sem, img], rrf_k)
    order3 = sorted(fused3, key=lambda f: (-fused3[f], -occs[f][0].last_seen_ms, f))  # 同分规则与产品一致
    out = {
        "exp1_lexical": lex[:TOP],
        "exp2_semantic": sem[:TOP],
        "exp3_image": img[:TOP],
        "exp4_rrf_text": [r.frame_id for r in text_resp.results][:TOP],
        "exp5_rrf_three": order3[:TOP],
    }
    if errors:
        out["_error"] = errors
    return out


def first_relevant_rank(ranked: list[str], relevant: set[str]) -> int | None:
    for i, f in enumerate(ranked, start=1):
        if f in relevant:
            return i
    return None


def score(rank: int | None) -> tuple[int, float]:
    """Hit@5（前 5 个不同帧里有相关帧）与 MRR@10 的单条贡献。"""
    return int(rank is not None and rank <= 5), (1.0 / rank if rank is not None and rank <= TOP else 0.0)


def aggregate(per_query: list[dict]) -> dict:
    """按 split → 分组（overall / scenario:* / language:*）→ 实验给出 hits/n、Hit@5、MRR@10。"""
    out: dict = {}
    for split in sorted({r["split"] for r in per_query}):
        rows = [r for r in per_query if r["split"] == split]
        groups = {"overall": rows}
        for key in ("scenario", "language"):
            for v in sorted({r[key] for r in rows}):
                groups[f"{key}:{v}"] = [r for r in rows if r[key] == v]
        out[split] = {}
        for g, rs in groups.items():
            out[split][g] = {}
            for e in EXPERIMENTS:
                hits = sum(r["results"][e]["hit5"] for r in rs)
                mrr = sum(r["results"][e]["rr10"] for r in rs)
                out[split][g][e] = {"hits": hits, "n": len(rs), "hit5": hits / len(rs) if rs else 0.0,
                                    "mrr10": mrr / len(rs) if rs else 0.0}
    return out


def evaluate(conn: sqlite3.Connection, queries: list[Query], engines: retrieval.Engines, *, split: str = "all",
             **kw) -> dict:
    chosen = [q for q in queries if split == "all" or q.split == split]
    per = []
    for q in chosen:
        try:
            ranks = rankings(conn, q, engines, **kw)
        except Exception as exc:  # noqa: BLE001 - 执行失败也要保留并计 0
            ranks = {e: [] for e in EXPERIMENTS} | {"_error": [f"{type(exc).__name__}: {exc}"]}
        rel = set(q.relevant)
        res = {}
        for e in EXPERIMENTS:
            r = first_relevant_rank(ranks.get(e, []), rel)
            h, rr = score(r)
            res[e] = {"first_relevant_rank": r, "hit5": h, "rr10": rr, "top10": ranks.get(e, [])}
        per.append({"id": q.id, "split": q.split, "scenario": q.scenario, "language": q.language, "mode": q.mode,
                    "query": q.query, "n_relevant": len(rel), "errors": ranks.get("_error", []), "results": res})
    frames = conn.execute("SELECT COUNT(*) FROM frames WHERE image_status != 'deleted'").fetchone()[0]
    return {"split": split, "frames_in_library": frames, "experiments": EXPERIMENTS,
            "summary": aggregate(per), "per_query": per}


def to_markdown(report: dict) -> str:
    lines = [f"# 检索实验结果（split = {report['split']}，底库 {report['frames_in_library']} 帧）", "",
             "命中数 / 查询数，Hit@5 与 MRR@10。Exp 5 仅为离线实验。逐条结果见同目录 JSON。", ""]
    for split, groups in report["summary"].items():
        lines += [f"## {split}", "", "| 分组 | " + " | ".join(EXPERIMENTS) + " |",
                  "| --- |" + " --- |" * len(EXPERIMENTS)]
        for g, exps in groups.items():
            cells = [f"{v['hits']}/{v['n']} · {v['hit5']:.2f} · {v['mrr10']:.2f}" for v in exps.values()]
            lines.append(f"| {g} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from . import bench
    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m screenrecall.evaluate", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir")
    ap.add_argument("--queries", required=True)
    ap.add_argument("--split", choices=["dev", "blind", "all"], required=True)
    ap.add_argument("--out", required=True, help="结果目录：results.json + results.md")
    ap.add_argument("--check-only", action="store_true", help="只检查查询文件是否符合方案的查询设计")
    args = ap.parse_args(argv)
    qpath = Path(args.queries)
    queries = load_queries(qpath)
    notes = protocol_check(queries)
    for n in notes:
        print("注意：" + n)
    if args.check_only:
        return 0
    cfg = load_config(args.data_dir)
    t, i = bench.real_engines(cfg, retrieval.TEXT), bench.real_engines(cfg, retrieval.IMAGE)
    engines = retrieval.Engines(text_encoder=t.text_encoder, text_index=t.text_index,
                                clip_text_encoder=i.clip_text_encoder, image_index=i.image_index)
    conn = db.connect(cfg.db_path)
    try:
        report = evaluate(conn, queries, engines, split=args.split, rrf_k=cfg.search.rrf_k,
                          branch_top=cfg.search.branch_top)
    finally:
        conn.close()
    report["queries_file"] = str(qpath)
    report["queries_blake2b"] = hashlib.blake2b(qpath.read_bytes(), digest_size=16).hexdigest()
    report["protocol_notes"] = notes
    report["models"] = {"text": cfg.models.text.model_id, "image": cfg.models.image.model_id,
                        "clip_text": cfg.models.clip_text.model_id}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "results.md").write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
