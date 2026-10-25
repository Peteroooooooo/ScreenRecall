"""合成 datasets/v1/queries.json：曹贺乔 20 条 + 肖天奕 40 条（2026-10-22 两人数据合库后的 60 条主集）。

- id 加采集者前缀（cao- / xiao-），两人原稿的 slot 编号有重名。
- 曹的草稿里场景写作 table，统一成冻结类别表里的 table_doc。
- 其余字段原样保留（relevant 是合并库里的 frame_id，合库时保留了 frame_id，不需要换号）；review_status
  由各自原稿决定：肖的 40 条已由 xiaotianyi 逐条复核 → approved；曹的 20 条保持原稿状态，等复核后改。

    python datasets/build_queries_v1.py --cao data-dataset/review/queries-cao-draft.json \
        --xiao datasets/local-20261019/queries.json --out datasets/v1/queries.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SCENARIO_FIX = {"table": "table_doc"}


def load(p: Path) -> list[dict]:
    data = json.loads(p.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data["queries"]


def build(cao: list[dict], xiao: list[dict]) -> list[dict]:
    out = []
    for owner, items in (("cao", cao), ("xiao", xiao)):
        for q in items:
            q = dict(q)
            q["scenario"] = SCENARIO_FIX.get(q["scenario"], q["scenario"])
            q["source_id"] = q["id"]
            q["id"] = f"{owner}-{q['split']}-{q['scenario']}-{q['language']}-{q['id'].rsplit('-', 1)[-1]}"
            q["owner"] = owner
            if owner == "xiao":
                q.setdefault("author", "kiro-ai（代 xiao 起草）")
                q.setdefault("reviewer", "xiao")
                if q.get("reviewed_by") and not q.get("review_status"):
                    q["review_status"] = "approved"
            out.append(q)
    ids = [q["id"] for q in out]
    if len(ids) != len(set(ids)):
        raise SystemExit("合成后 id 仍有重复")
    return sorted(out, key=lambda q: (q["split"] != "dev", q["scenario"], q["language"], q["id"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cao", required=True, type=Path)
    ap.add_argument("--xiao", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    qs = build(load(a.cao), load(a.xiao))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(qs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(qs)} 条 → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
