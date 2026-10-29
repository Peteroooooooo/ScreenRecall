"""合成图上的 OCR 对照：旧版 rapidocr-onnxruntime 1.4.4 vs PP-OCRv6 tiny vs small。

指标：每张耗时（预热后取中位数）、字符错误率 CER（去空白后按阅读顺序拼接比较）、
英文 / 数字之间空格的保留率、行数。结果只写数字到 results/text/ocr_bench.json。

用法：python experiments/text/ocr_bench.py [--n 3] [--threads 2]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402

import metrics  # noqa: E402
import synth  # noqa: E402


def _order(items: list[tuple[str, float, float]], width: int, two_cols: bool) -> str:
    """按阅读顺序拼接：两栏文档先左栏后右栏，栏内从上到下。"""
    def key(it):
        _t, x, y = it
        return ((1 if two_cols and x >= width / 2 else 0), y, x)

    return "\n".join(t for t, _x, _y in sorted(items, key=key))


class Legacy:
    name = "rapidocr-onnxruntime 1.4.4 (PP-OCRv4)"

    def __init__(self, threads: int):
        from rapidocr_onnxruntime import RapidOCR

        self._e = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1, max_side_len=2000,
                           print_verbose=False)

    def __call__(self, bgr: np.ndarray, crop: bool) -> list[tuple[str, float, float]]:
        res, _ = self._e(bgr)
        out = []
        for quad, text, _score in res or []:
            out.append((str(text), min(p[0] for p in quad), min(p[1] for p in quad)))
        return out


class V6:
    def __init__(self, model: str, threads: int):
        from screenrecall.text.ocr2 import OcrEngine

        self._e = OcrEngine(model, threads=threads)
        self.name = self._e.version()

    def __call__(self, bgr: np.ndarray, crop: bool) -> list[tuple[str, float, float]]:
        return [(ln.text, ln.x0, ln.y0) for ln in self._e.recognize(bgr, crop=crop)]


def run(n: int, threads: int, repeats: int) -> dict:
    engines = {}
    t = time.perf_counter()
    engines["legacy_v4"] = Legacy(threads)
    load = {"legacy_v4": time.perf_counter() - t}
    for m in ("tiny", "small"):
        t = time.perf_counter()
        engines[f"v6_{m}"] = V6(m, threads)
        load[f"v6_{m}"] = time.perf_counter() - t
    images = {kind: [fn(1000 + i) for i in range(n)] for kind, fn in synth.KINDS.items()}
    out: dict = {"threads": threads, "images_per_kind": n, "repeats": repeats,
                 "load_s": {k: round(v, 2) for k, v in load.items()},
                 "engines": {k: getattr(e, "name", k) for k, e in engines.items()}, "kinds": {}}
    for kind, imgs in images.items():
        crop = kind.startswith("crop")
        two_cols = kind.startswith("dense")
        per = {}
        for ek, eng in engines.items():
            eng(imgs[0][0], crop)  # 预热
            times, cers, gaps_kept, gaps_truth, lines = [], [], 0, 0, []
            for bgr, truth in imgs:
                ts = []
                for _ in range(repeats):
                    t0 = time.perf_counter()
                    got = eng(bgr, crop)
                    ts.append(time.perf_counter() - t0)
                times.append(statistics.median(ts))
                w = bgr.shape[1]
                ref = _order([(t, x, y) for t, x, y in truth], w, two_cols)
                hyp = _order(got, w, two_cols)
                cers.append(metrics.cer(ref, hyp))
                gaps_truth += metrics.ascii_gaps(ref)
                gaps_kept += min(metrics.ascii_gaps(hyp), metrics.ascii_gaps(ref))
                lines.append((len(got), len(truth)))
            per[ek] = {
                "s_per_image": round(statistics.median(times), 3),
                "cer": round(statistics.mean(cers), 4),
                "english_spaces_kept": round(gaps_kept / max(1, gaps_truth), 3),
                "lines_found_vs_truth": [sum(a for a, _ in lines), sum(b for _, b in lines)],
            }
            print(kind, ek, per[ek], flush=True)
        out["kinds"][kind] = per
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "results" / "text" / "ocr_bench.json"))
    args = ap.parse_args()
    import platform

    res = run(args.n, args.threads, args.repeats)
    res["machine"] = {"cpu": platform.processor(), "python": platform.python_version()}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
