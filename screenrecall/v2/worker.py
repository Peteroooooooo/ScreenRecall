"""空闲整理进程：电脑空闲时由主进程启动（低优先级，stdin 接管道），把录下来的东西整理好。

    python -m screenrecall.v2.worker [--data-dir D] [--home H] [--once]

按优先级做（每一步都分小批、进度存在库里，随时被杀掉下次接着做）：
1. OCR：UIA 读不到字的待识别区域（work_ocr）；
2. 文字向量：新行按短段落算向量（work_text）；
3. 合并块：同一内容源里文字连续重叠的块并成一块（work_text）；
4. 片段：从窗口焦点记录切片段（work_pieces）；
5. 概括：大模型写片段概括 + 任务标签、天概括（work_llm）；
6. 图片向量：新证据帧的 CLIP 向量 + 内容分类（work_clip）；
7. 旧证据帧只统计不删（work_clip.frame_report）。

停止：stdin 读到 EOF（主进程发现用户回来了会关掉管道）后在当前这一项做完时停下（约 1 秒内），退出码 0；
没有活可干时自己退出（退出码 0）；--once 只做一轮（测试用）。
同一数据目录同时只允许一个整理进程（data_dir/worker.lock）。
日志只记数量和用时，不记屏幕内容。
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..timeutil import utc_ms
from . import stdin_eof
from .work_clip import ClipStep, frame_report
from .work_common import Ctx, FileLock, StopFlag, Stopped, log
from .work_llm import PIECE_JOB, LlmStep
from .work_ocr import OcrStep, default_ocr_factory
from .work_pieces import PieceStep
from .work_text import MergeStep, TextVectorStep

LOCK_NAME = "worker.lock"


# ---------------- 默认依赖（测试里换成假的） ----------------

def default_encoder_factory(cfg) -> Callable[[], object]:
    def make():
        from ..text.encoder import OnnxTextEncoder

        # [v2.encoder] 是主进程查询用的；空闲整理用自己的线程数（用户不在，进程又是低优先级）
        return OnnxTextEncoder(threads=cfg.idle.encoder_threads, batch_size=cfg.encoder.batch_size)
    return make


def default_llm_factory(cfg, stop: StopFlag) -> Callable[[], object]:
    def make():
        from ..app import llm

        return llm.LlmClient.from_env(env_files=[llm.REPO_ROOT / ".env", Path(cfg.home) / ".env"],
                                      sleep=lambda s: stop.wait(s))
    return make


def default_clip_factory(cfg) -> Callable[[], object]:
    def make():
        from ..text.clipbatch import ClipBatch

        return ClipBatch(cfg.clip)
    return make


# ---------------- 单实例与 stdin ----------------

class InstanceLock(FileLock):
    """data_dir/worker.lock：拿不到说明已有一个整理进程在跑。"""

    def __init__(self, data_dir: Path):
        super().__init__(Path(data_dir) / LOCK_NAME)


def watch_stdin(stop: StopFlag, stream=None, *, poll_s: float = stdin_eof.POLL_S) -> threading.Thread | None:
    """stdin 是管道时，读到 EOF（主进程关了管道或自己退出了）就置停止标志。控制台里手动运行不看 stdin。
    管道不挂同步读（见 stdin_eof：挂着读会让别的线程加载 numpy / torch 的 DLL 时卡死）。"""
    return stdin_eof.watch(stop.set, stream, poll_s=poll_s)


# ---------------- 整理 ----------------

def _recover(conn) -> None:
    """上一个整理进程中途被杀：它领了没做完的区域 / 待办现在就能再领（已用掉的尝试次数照算）。"""
    conn.execute("UPDATE ocr_regions SET status = 'pending', claimed_ms = NULL WHERE status = 'running'")
    conn.execute("UPDATE jobs SET status = 'pending', not_before_ms = 0 WHERE status = 'running' AND kind IN (?, ?)",
                 ("text_rechunk", PIECE_JOB))


class Worker:
    def __init__(self, cfg, *, stop: StopFlag | None = None, ocr_factory=None, encoder_factory=None,
                 llm_factory="default", clip_factory=None, classifier_loader=None,
                 clock: Callable[[], int] = utc_ms):
        self.cfg = cfg
        self.stop = stop or StopFlag()
        self.clock = clock
        self._ocr_factory = ocr_factory or default_ocr_factory(cfg)
        self._encoder_factory = encoder_factory or default_encoder_factory(cfg)
        self._llm_factory = default_llm_factory(cfg, self.stop) if llm_factory == "default" else llm_factory
        self._clip_factory = clip_factory or default_clip_factory(cfg)
        self._classifier_loader = classifier_loader

    def run(self, once: bool = False) -> dict:
        from ..store.writer import Writer

        t0 = time.perf_counter()
        report: dict = {"passes": 0, "stopped": False, "steps": {}}
        with Writer(self.cfg.data_dir, self.cfg.writer, clock=self.clock) as writer:
            ctx = Ctx(self.cfg, writer, self.stop, clock=self.clock)
            text = TextVectorStep(ctx, self._encoder_factory)
            try:
                writer.call(_recover).result(30)
                steps = [OcrStep(ctx, self._ocr_factory), text, MergeStep(ctx), PieceStep(ctx),
                         LlmStep(ctx, self._llm_factory)]
                if self.cfg.idle.clip:
                    steps.append(ClipStep(ctx, self._clip_factory, self._classifier_loader))
                broken: set[str] = set()
                while not self.stop.is_set():
                    report["passes"] += 1
                    did = 0
                    for step in steps:
                        if self.stop.is_set():
                            break
                        if step.name in broken:
                            continue
                        if isinstance(step, (LlmStep, ClipStep)) and step.backlog():
                            # 等大模型回答可能要很久，CLIP（torch）最占内存：先放掉 OCR 引擎和文本编码器
                            # （约 0.9 GB），峰值不叠加；下一轮要用再加载
                            steps[0].close()
                            text.close()
                            gc.collect()
                        try:
                            did += step.run()
                        except Stopped:
                            break
                        except Exception:  # noqa: BLE001 —— 一步出错不拖累别的步骤；这次整理不再做这一步
                            log.exception("整理步骤 %s 出错，本次跳过", step.name)
                            broken.add(step.name)
                    if once or did == 0 or self.stop.is_set():
                        break
                    self.stop.wait(1.0)  # 录制进程还在补新内容：歇一下再来一轮
                if not self.stop.is_set():
                    frame_report(ctx)
                llm = next((s for s in steps if isinstance(s, LlmStep)), None)
                if llm is not None and llm.model:
                    report["llm_model"] = llm.model
            finally:
                text.close()
                ctx.close()
            report["stopped"] = self.stop.is_set()
            report["steps"] = {k: {"items": v.items, "seconds": round(v.seconds, 3), **v.extra}
                               for k, v in ctx.stats.items()}
        report["seconds"] = round(time.perf_counter() - t0, 3)
        report["rss_mb"], report["peak_mb"] = _memory_mb()
        return report


def _memory_mb() -> tuple[float | None, float | None]:
    try:
        import psutil

        mi = psutil.Process().memory_info()
        peak = getattr(mi, "peak_wset", None)
        return round(mi.rss / 2 ** 20, 1), (round(peak / 2 ** 20, 1) if peak else None)
    except Exception:  # noqa: BLE001
        return None, None


LOG_NAME = "idle-worker.log"
LOG_MAX_BYTES = 2_000_000


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """日志写 <home>/logs/idle-worker.log（滚动，最多约 8 MB）；标准错误（主进程把它接到 idle-worker.out.log）
    只留警告和出错。httpx 每次请求都在 INFO 记完整地址（大模型接口地址里可能带密钥），压到 WARNING。"""
    import logging.handlers

    logs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(logs_dir / LOG_NAME, maxBytes=LOG_MAX_BYTES, backupCount=3,
                                              encoding="utf-8", delay=True)
    fh.setFormatter(fmt)
    fh.setLevel(level)
    root = logging.getLogger()
    root.addHandler(fh)
    if sys.stderr is not None:
        eh = logging.StreamHandler(sys.stderr)
        eh.setFormatter(fmt)
        eh.setLevel(max(level, logging.WARNING))
        root.addHandler(eh)
    root.setLevel(level)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.v2.worker", description="空闲整理进程")
    ap.add_argument("--data-dir", default=None, help="v2 数据目录（默认按配置）")
    ap.add_argument("--home", default=None, help="应用目录（默认 SCREENRECALL_HOME 或 %%LOCALAPPDATA%%\\ScreenRecall）")
    ap.add_argument("--once", action="store_true", help="只做一轮（测试用）")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    if args.home:
        os.environ["SCREENRECALL_HOME"] = str(Path(args.home).resolve())
    from .config import load_v2

    cfg = load_v2(args.data_dir, home=Path(args.home).resolve() if args.home else None)
    setup_logging(cfg.logs_dir, getattr(logging, str(args.log_level).upper(), logging.INFO))
    stop = StopFlag()
    watch_stdin(stop)
    lock = InstanceLock(cfg.data_dir)
    if not lock.acquire():
        log.info("已有一个空闲整理进程在处理这个数据目录，本进程退出")
        return 0
    try:
        report = Worker(cfg, stop=stop).run(once=args.once)
    except Exception:  # noqa: BLE001
        log.exception("空闲整理进程出错退出")
        return 1
    finally:
        lock.release()
    log.info("整理结束：%s", json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
