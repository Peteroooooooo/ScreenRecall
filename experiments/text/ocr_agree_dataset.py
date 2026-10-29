"""真实截图上的只读一致性核对：旧版 1.4.4 vs PP-OCRv6 tiny vs small。

- 数据库用 sqlite3 只读模式（mode=ro）打开，只取帧图片路径；图片只读不写；
- 识别出的文字只在内存里比较，**不打印、不保存、不写日志**，只输出汇总数字：
  两两字符一致率、行数、每张耗时、不一致的字里非 GB2312 汉字（繁体 / 生僻字）所占比例；
- 不跑任何搜索，不碰评测查询。

用法：python experiments/text/ocr_agree_dataset.py [--n 40] [--data PATH]
结果写到 results/text/ocr_agree_dataset.json。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import metrics  # noqa: E402
from ocr_bench import V6, Legacy  # noqa: E402

DEFAULT_DATA = ROOT.parent / "ScreenRecall" / "data-dataset"


def _frames(data: Path, n: int) -> list[Path]:
    db = data / "screenrecall.sqlite3"
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT image_path FROM frames WHERE image_status = 'ok' ORDER BY captured_at_ms, frame_id"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return []
    idx = np.linspace(0, len(rows) - 1, num=min(n, len(rows))).round().astype(int)
    return [data / rows[i][0] for i in sorted(set(idx.tolist()))]


def _stats(xs: list[float], nd: int = 4) -> dict:
    return {"mean": round(statistics.mean(xs), nd), "median": round(statistics.median(xs), nd),
            "min": round(min(xs), nd)} if xs else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--data", default=str(DEFAULT_DATA))
    args = ap.parse_args()
    paths = _frames(Path(args.data), args.n)
    engines = {"legacy_v4": Legacy(args.threads), "v6_tiny": V6("tiny", args.threads), "v6_small": V6("small", args.threads)}
    times = {k: [] for k in engines}
    lines = {k: [] for k in engines}
    chars = {k: 0 for k in engines}
    rare = {k: 0 for k in engines}
    cjk = {k: 0 for k in engines}
    pairs = [("legacy_v4", "v6_tiny"), ("legacy_v4", "v6_small"), ("v6_tiny", "v6_small")]
    agree = {f"{a}~{b}": [] for a, b in pairs}
    diff_total = {f"{a}~{b}": 0 for a, b in pairs}
    diff_rare = {f"{a}~{b}": 0 for a, b in pairs}
    sizes = []
    skipped = 0
    for p in paths:
        try:
            with Image.open(p) as im:
                im.load()
                bgr = np.ascontiguousarray(np.asarray(im.convert("RGB"))[:, :, ::-1])
        except OSError:
            skipped += 1
            continue
        sizes.append(bgr.shape[1] * bgr.shape[0])
        texts: dict[str, str] = {}
        for k, eng in engines.items():
            t0 = time.perf_counter()
            got = eng(bgr, False)
            times[k].append(time.perf_counter() - t0)
            lines[k].append(len(got))
            texts[k] = "\n".join(t for t, _x, _y in got)
            s = metrics.squash(texts[k])
            chars[k] += len(s)
            cjk[k] += sum(1 for ch in s if metrics.is_cjk(ch))
            rare[k] += sum(1 for ch in s if metrics.is_rare_cjk(ch))
        for a, b in pairs:
            key = f"{a}~{b}"
            agree[key].append(metrics.agreement(texts[a], texts[b]))
            d = metrics.diff_chars(texts[a], texts[b])
            diff_total[key] += len(d)
            diff_rare[key] += sum(1 for ch in d if metrics.is_rare_cjk(ch))
        del texts  # 识别结果只在这一轮里用于比较
        print(f"done {len(sizes)}/{len(paths)}", flush=True)
    out = {
        "frames_read": len(sizes), "frames_skipped": skipped, "threads": args.threads,
        "median_megapixels": round(statistics.median(sizes) / 1e6, 2) if sizes else None,
        "engines": {k: getattr(e, "name", k) for k, e in engines.items()},
        "s_per_image": {k: _stats(v, 3) for k, v in times.items()},
        "lines_per_image": {k: _stats([float(x) for x in v], 1) for k, v in lines.items()},
        "chars_total": chars,
        "non_gb2312_share_of_cjk": {k: round(rare[k] / max(1, cjk[k]), 5) for k in engines},
        "char_agreement": {k: _stats(v) for k, v in agree.items()},
        "disagreeing_chars": diff_total,
        "non_gb2312_share_of_disagreeing": {k: round(diff_rare[k] / max(1, diff_total[k]), 4) for k in diff_total},
        "note": "只读核对；识别文字未打印、未保存。非 GB2312 汉字 = 繁体或生僻字。",
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    dst = ROOT / "results" / "text" / "ocr_agree_dataset.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
