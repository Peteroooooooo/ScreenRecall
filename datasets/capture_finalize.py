"""把 capture_mac.py 写的 capture_log.csv 合进 collect manifest 生成的 manifest.csv。

    .venv/bin/python datasets/capture_finalize.py data-dataset/raw/cao --device cao-mbp \
        --plan datasets/scenes/pilot_plan.py

- 先对每个任务目录跑 `collect manifest`（自动算 sha、尺寸、编号）；
- 再按文件名把截图那一刻记下的时间（time_source=manual）、应用、窗口标题、语言、事实编号和
  来源 / 画面变化说明填进去。人工已经改过的行（keep / drop_reason / facts 非空）不覆盖；
- 计划里的 FACTS 追加进 <采集者>/facts.csv（已有的 fact_id 不改）。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenrecall import collect  # noqa: E402

FACT_COLS = ["fact_id", "task_id", "kind", "content", "language", "note"]


def finalize(root: Path, device: str, plan_facts: list[tuple] | None) -> list[str]:
    msgs = []
    for task_dir in sorted(p for p in root.iterdir() if p.is_dir() and (p / "capture_log.csv").exists()):
        added, total, notes = collect.build_manifest(task_dir, device=device)
        msgs += [f"{task_dir.name}: {n}" for n in notes]
        log = {r["file"]: r for r in collect.read_csv(task_dir / "capture_log.csv")}
        rows = collect.read_csv(task_dir / collect.MANIFEST)
        filled = 0
        for r in rows:
            c = log.get(r["file"])
            if not c or (r.get("app") or "").strip():
                continue   # 不是本工具截的，或者已经填过 / 人工改过
            r.update(captured_local=c["captured_local"], time_source=c["time_source"], app=c["app"],
                     window_title=c["window_title"], language=c["language"],
                     note="；".join(x for x in (c["source"], c["variation"], c["note"]) if x))
            if not (r.get("facts") or "").strip():
                r["facts"] = c["facts"]
            filled += 1
        collect.write_csv(task_dir / collect.MANIFEST, collect.MANIFEST_COLS, rows)
        msgs.append(f"{task_dir.name}: 清单 {total} 行（新增 {added}，本次填入采集记录 {filled}）")
    if plan_facts:
        fp = root / "facts.csv"
        old = collect.read_csv(fp) if fp.exists() else []
        have = {r["fact_id"] for r in old}
        new = [dict(zip(FACT_COLS, f)) for f in plan_facts if f[0] not in have]
        collect.write_csv(fp, FACT_COLS, old + new)
        msgs.append(f"facts.csv：新增 {len(new)} 条，共 {len(old) + len(new)} 条")
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="采集者目录，如 data-dataset/raw/cao")
    ap.add_argument("--device", required=True)
    ap.add_argument("--plan", help="从计划文件里取 FACTS 写进 facts.csv")
    a = ap.parse_args()
    facts = None
    if a.plan:
        spec = importlib.util.spec_from_file_location("plan", a.plan)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        facts = getattr(mod, "FACTS", None)
    for m in finalize(Path(a.root), a.device, facts):
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
