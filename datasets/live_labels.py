"""把本地标注工具（tools/label_app.py）的标注表转成数据集标注格式（与 labels-cao.csv 同列），用于实时录制的帧。

实时录制的帧没有原始截图清单和任务编号，这里按固定规则补 task_id（来源组的起点，决定分类划分时哪些帧必须
在同一侧）：应用 + 规范化窗口标题 + 首次出现所在的 5 分钟段。Edge 标题里会变的"和另外 N 个页面""用户配置"
后缀先去掉，同一篇网页不因标签数变化被拆开。dataset.source_groups 还会再把像素相同或 OCR 全文相同的帧并组。

已知取舍：同一窗口标题跨过多个 5 分钟段时（如长时间看同一份 PDF、同一个微信窗口里切换多个群），不同段会
落到不同组，可能分到不同划分；全按标题分组则微信一个组就有 339 帧，划分无法平衡。这一点写进 PROGRESS。

    python datasets/live_labels.py --data-dir data-dataset --src datasets/local-20261019/labels.csv \
        --annotator xiao --out data-dataset/labels-xiao.csv

已有 --out 时保留其中的复核列（review_label / reviewer / final_label / change_reason），只刷新其余列。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenrecall import collect, dataset, db, timeutil  # noqa: E402
from screenrecall.config import load_config  # noqa: E402

COLS = dataset.ALL_COLS + collect.EXTRA_LABEL_COLS
REVIEW_COLS = ("review_label", "reviewer", "final_label", "change_reason")
BLOCK_MS = 5 * 60_000
WEB_APPS = {"msedge.exe", "chrome.exe", "firefox.exe"}


def norm_title(t: str) -> str:
    t = re.sub(r" - 用户配置 \d+ - Microsoft Edge$", "", t or "")
    return re.sub(r" 和另外 \d+ 个页面.*$", "", t).strip()


def task_id(app: str, title: str, first_ms: int) -> str:
    stem = re.sub(r"\.exe$", "", (app or "unknown").lower())
    h = hashlib.blake2b(norm_title(title).encode(), digest_size=4).hexdigest()
    return f"xiao-live-{stem}-{h}-{first_ms // BLOCK_MS}"


def build(conn, src: Path, annotator: str, out: Path) -> tuple[int, list[str]]:
    keep = {}
    if out.exists():
        for r in csv.DictReader(open(out, encoding="utf-8-sig", newline="")):
            keep[r["frame_id"]] = {c: r.get(c, "") for c in REVIEW_COLS}
    live = dataset.live_frames(conn)
    info = {r["frame_id"]: r for r in conn.execute(
        "SELECT f.frame_id, f.image_path, o.first_seen_ms, o.app, o.window_title, o.session_id, o.source_type, "
        "length(t.text) AS n FROM frames f JOIN occurrences o ON o.occurrence_id = (SELECT occurrence_id FROM "
        "occurrences WHERE frame_id = f.frame_id ORDER BY first_seen_ms, occurrence_id LIMIT 1) "
        "LEFT JOIN frame_text t ON t.frame_id = f.frame_id WHERE f.image_status = 'ok'")}
    rows, problems = [], []
    for s in csv.DictReader(open(src, encoding="utf-8-sig", newline="")):
        fid = s["frame_id"].strip()
        f = info.get(fid)
        if f is None:
            problems.append(f"{fid[:8]}：库里没有这一帧（或原图不可用）")
            continue
        if fid not in live:
            problems.append(f"{fid[:8]}：不是实时录制的帧，应走 collect labels")
            continue
        if not s.get("label"):
            problems.append(f"{fid[:8]}：没有初标 label")
            continue
        row = {c: "" for c in COLS}
        row.update(frame_id=fid, image_path=f["image_path"], captured_local=timeutil.fmt_local(f["first_seen_ms"]),
                   app=f["app"] or "", window_title=f["window_title"] or "", session_id=f["session_id"],
                   source_type=f["source_type"], task_id=task_id(f["app"], f["window_title"], f["first_seen_ms"]),
                   label=s["label"].strip(), annotator=annotator, collector=annotator, source_file=f["image_path"],
                   ocr_chars=str(f["n"] or 0), time_known="yes",
                   source_kind="real_web" if (f["app"] or "").lower() in WEB_APPS else "real_app")
        row.update(keep.get(fid, {}))
        rows.append(row)
    if not problems:
        collect.write_csv(out, COLS, rows)
    return len(rows), problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="实时录制帧的标注表 → 数据集标注格式")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--src", required=True, type=Path, help="tools/label_app.py 写出的 labels.csv")
    ap.add_argument("--annotator", required=True)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    conn = db.connect(load_config(a.data_dir).db_path)
    try:
        n, problems = build(conn, a.src, a.annotator, a.out)
    finally:
        conn.close()
    for p in problems[:30]:
        print("错误：" + p, file=sys.stderr)
    if problems:
        print(f"错误：共 {len(problems)} 个问题，未写出", file=sys.stderr)
        return 1
    print(f"写出 {n} 行到 {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
