"""文本编码器开销：ONNX（onnxruntime + tokenizers）vs sentence-transformers（torch）。

每种各起一个新进程测：导入 + 加载耗时、加载后常驻内存（RSS / 私有字节）、吞吐（条 / 秒）、编码时的峰值 RSS。
文本是合成的中英混合短句（长度不一，部分超过 128 token）。结果写到 results/text/encoder_bench.json。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _texts(n: int) -> list[str]:
    import random

    rng = random.Random(3)
    words = ["今天下午三点开组会", "请把实验数据整理一下", "meeting at 3pm", "the build failed", "录屏软件", "磁盘快满了",
             "error 0x80070005", "搜索结果按时间排序", "pull request review", "图书馆三楼"]
    return [" ".join(rng.choice(words) for _ in range(rng.randint(1, 25))) for _ in range(n)]


def child(mode: str, threads: int) -> dict:
    import os

    import psutil

    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    p = psutil.Process()
    mem = lambda: (p.memory_info().rss / 2**20, p.memory_full_info().private / 2**20)  # noqa: E731
    base = mem()
    t0 = time.perf_counter()
    if mode.startswith("onnx"):
        from screenrecall.text.encoder import OnnxTextEncoder

        enc = OnnxTextEncoder(threads=threads, batch_size=16, mem_arena=(mode != "onnx_noarena"))
    else:
        from screenrecall import models

        cfg = models.ModelsConfig(models.DEFAULT_MODELS.text, models.DEFAULT_MODELS.image, models.DEFAULT_MODELS.clip_text,
                                  threads=threads, batch_size=16)
        enc = models.StText(cfg.text, cfg)
    load_s = time.perf_counter() - t0
    loaded = mem()
    texts = _texts(512)
    enc.encode(texts[:32])
    peak = [loaded[0]]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            peak[0] = max(peak[0], p.memory_info().rss / 2**20)
            time.sleep(0.02)

    th = threading.Thread(target=sample, daemon=True)
    th.start()
    t0 = time.perf_counter()
    enc.encode(texts)
    enc_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    for t in texts[:64]:
        enc.encode([t])
    single_ms = (time.perf_counter() - t1) / 64 * 1000
    stop.set()
    th.join()
    return {
        "mode": mode, "threads": threads, "load_s": round(load_s, 2),
        "rss_base_mib": round(base[0]), "rss_loaded_mib": round(loaded[0]), "private_loaded_mib": round(loaded[1]),
        "rss_peak_encoding_mib": round(peak[0]),
        "texts_per_s_batch16": round(len(texts) / enc_s, 1), "single_query_ms": round(single_ms, 1),
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        print(json.dumps(child(sys.argv[2], int(sys.argv[3]))))
        return 0
    out = {"note": "i5-12600KF (AVX2, no AVX-512); fresh process per mode; synthetic zh/en texts", "runs": []}
    for mode, threads in (("onnx", 2), ("onnx_noarena", 2), ("torch", 2), ("onnx", 4), ("torch", 4)):
        r = subprocess.run([sys.executable, __file__, "--child", mode, str(threads)], capture_output=True, text=True,
                           check=True)
        res = json.loads(r.stdout.strip().splitlines()[-1])
        print(res, flush=True)
        out["runs"].append(res)
    dst = ROOT / "results" / "text" / "encoder_bench.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
