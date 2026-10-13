"""采集与索引后台：python -m screenrecall.daemon [--check] [--data-dir PATH]

- 启动时：DPI 感知 → 环境检查 → 单实例锁 → 建表 → 恢复中断任务 → 文件核查；录制默认关闭；
- 运行时：主线程轮询控制记录并写心跳，采集线程按间隔采样，OCR 线程处理待办；
- 关闭页面不影响后台；Ctrl+C / Ctrl+Break 时结束录制段并标记后台已退出。
"""

from __future__ import annotations

import argparse
import logging
import msvcrt
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from . import control, db, envcheck, storage, winapi
from .capture import Recorder
from .config import Config, load_config
from .ocr import OcrWorker, recover_interrupted

CONTROL_POLL_S = 0.3
HEARTBEAT_S = 1.0


def _setup_logging(cfg: Config) -> logging.Logger:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("screenrecall")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    fh = RotatingFileHandler(cfg.logs_dir / "daemon.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.propagate = False
    return log


def _acquire_single_instance(cfg: Config):
    """在数据目录上加独占锁，避免两个后台同时录制。进程退出时系统自动释放。"""
    fh = open(cfg.data_dir / "daemon.lock", "a+b")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="python -m screenrecall.daemon", description="ScreenRecall 采集与索引后台")
    ap.add_argument("--check", action="store_true", help="只运行环境检查")
    ap.add_argument("--data-dir", help="运行数据目录（覆盖配置与环境变量）")
    args = ap.parse_args(argv)

    cfg = load_config(args.data_dir)
    dpi_mode = winapi.enable_dpi_awareness()  # 必须早于任何窗口 API 调用
    report = envcheck.run_checks(cfg, dpi_mode)
    print(report.render(), flush=True)
    if not report.ok:
        print("环境检查未通过，后台未启动。", flush=True)
        return 1
    if args.check:
        return 0

    lock = _acquire_single_instance(cfg)
    if lock is None:
        print(f"另一个后台实例正在使用 {cfg.data_dir}，本实例退出。", flush=True)
        return 2

    log = _setup_logging(cfg)
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    recovered = recover_interrupted(conn)
    files = storage.reconcile(conn, cfg.data_dir, log)
    control.on_daemon_start(conn, os.getpid())
    log.info("数据目录 %s；恢复中断任务 %d 个；文件核查 %s", cfg.data_dir, recovered, files)

    stop = threading.Event()
    wake_ocr = threading.Event()
    recorder = Recorder(cfg, wake_ocr, stop, log)
    worker = OcrWorker(cfg, report.engine, wake_ocr, stop, log)
    threads = [
        threading.Thread(target=recorder.run, name="capture", daemon=True),
        threading.Thread(target=worker.run, name="ocr", daemon=True),
    ]
    for t in threads:
        t.start()

    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    log.info("后台已启动（pid %d），录制默认关闭。现在可以运行：streamlit run app.py", os.getpid())

    last_beat = 0.0
    try:
        while not stop.is_set():
            ctrl = control.read(conn)
            if control.is_pending(ctrl):
                try:
                    recorder.apply_request(conn, ctrl)
                except Exception:
                    log.exception("应用控制请求失败，稍后重试")
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_S:
                control.heartbeat(conn)
                last_beat = now
            stop.wait(CONTROL_POLL_S)
    except KeyboardInterrupt:
        log.info("收到中断信号")
    finally:
        stop.set()
        wake_ocr.set()
        try:
            recorder.shutdown(conn)
        except Exception:
            log.exception("结束录制段失败")
        for t in threads:
            t.join(timeout=15)
        control.on_daemon_stop(conn)
        conn.close()
        lock.close()
        log.info("后台已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
