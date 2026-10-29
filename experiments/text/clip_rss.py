"""CLIP 空闲批次的内存：进批次前 / 批次中 / 出批次后（新进程里测，合成图）。

结果写到 results/text/clip_rss.json。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")


def main() -> int:
    import numpy as np
    import psutil

    from screenrecall.text.clipbatch import ClipBatch, ClipBatchConfig

    p = psutil.Process()

    def mem():
        fi = p.memory_full_info()
        return {"rss_mib": round(fi.rss / 2**20), "private_mib": round(fi.private / 2**20)}

    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 255, size=(1000, 1600, 3), dtype=np.uint8) for _ in range(32)]
    out = {"images": len(imgs), "size": "1600x1000", "threads": 4, "before": mem()}
    t0 = time.perf_counter()
    with ClipBatch(ClipBatchConfig(threads=4, batch_size=16)) as clip:
        out["load_s"] = round(time.perf_counter() - t0, 2)
        out["loaded"] = mem()
        t1 = time.perf_counter()
        v = clip.encode(imgs)
        out["encode_s_per_image"] = round((time.perf_counter() - t1) / len(imgs), 3)
        out["during"] = mem()
        import torch

        out["torch_threads_in_batch"] = torch.get_num_threads()
    out["after"] = mem()
    out["torch_threads_after"] = torch.get_num_threads()
    out["vec_shape"] = list(v.shape)
    # 第二个批次：看模型内存是否回到"只导入了 torch"的水平、多次批次是否累积
    with ClipBatch(ClipBatchConfig(threads=4, batch_size=16)) as clip:
        clip.encode(imgs[:8])
        out["second_during"] = mem()
    out["second_after"] = mem()
    out["note"] = ("after 的 RSS 很小是因为退出批次时 EmptyWorkingSet 把页面换出；以私有字节为准。"
                   "torch / transformers 库本身的内存随空闲整理进程退出才归还。")
    print(json.dumps(out, ensure_ascii=False))
    dst = ROOT / "results" / "text" / "clip_rss.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
