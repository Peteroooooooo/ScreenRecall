"""只建索引、不采集：python -m screenrecall.indexer --data-dir DIR

用途：数据集准备阶段，把批量导入的截图跑完 OCR 与向量待办，然后退出。复用后台同一套 OcrWorker /
EmbedWorker 和同一份模型配置，只是不启动窗口采集、快捷键和维护线程，因此也能在 macOS 开发机上运行
（采集后台 daemon 依赖 Windows API，在 Mac 上不能启动）。

- 先处理全部 OCR 待办，再处理向量待办；当前没有可领取的待办（全部完成，或只剩退避等待重试的）就结束，
  等待重试的待办留在 pending，再运行一次即可；
- 结束时打印各阶段剩余待办与失败数，失败原因看数据库 index_jobs.last_error；
- 不能和同一数据目录上正在运行的后台同时用（两边会抢同一批待办）。
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

from . import db
from .config import load_config
from .ocr import OcrEngine, OcrWorker, recover_interrupted


def _counts(conn) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT stage, status, COUNT(*) FROM index_jobs GROUP BY stage, status"):
        out.setdefault(r[0], {})[r[1]] = r[2]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.indexer", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", required=True, help="数据目录（不要指向正在被后台使用的目录）")
    ap.add_argument("--no-vectors", action="store_true", help="只跑 OCR，向量待办留到以后")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("indexer")
    cfg = load_config(args.data_dir)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    log.info("数据目录 %s；恢复中断任务 %d 个", cfg.data_dir, recover_interrupted(conn))

    stop, wake = threading.Event(), threading.Event()
    ocr = OcrWorker(cfg, OcrEngine(cfg.ocr), wake, stop, log)
    t0, n = time.monotonic(), 0
    while (job := ocr._claim(conn)) is not None:
        ocr._process(conn, job)
        n += 1
        if n % 50 == 0:
            log.info("OCR 已处理 %d 个", n)
    log.info("OCR 处理 %d 个，用时 %.0f 秒", n, time.monotonic() - t0)

    if not args.no_vectors and cfg.models.enabled:
        from . import models
        from .embed import EmbedWorker

        missing = models.available(cfg.models)
        if missing:
            log.warning("缺少模型 %s，向量待办保留；准备：python -m screenrecall.models --download", missing)
        else:
            emb = EmbedWorker(cfg, threading.Event(), stop, log)
            t0, n = time.monotonic(), 0
            while emb.run_once(conn):
                n += 1
                if n % 50 == 0:
                    log.info("向量已处理 %d 个", n)
            log.info("向量处理 %d 个，用时 %.0f 秒", n, time.monotonic() - t0)

    counts = _counts(conn)
    conn.close()
    print("各阶段待办状态：", counts)
    left = sum(v for st in counts.values() for k, v in st.items() if k in ("pending", "running"))
    failed = sum(v for st in counts.values() for k, v in st.items() if k == "failed")
    print(f"未完成 {left} 个，失败 {failed} 个" + ("（原因见 index_jobs.last_error）" if failed else ""))
    return 0 if not left and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
