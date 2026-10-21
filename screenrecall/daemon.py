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

from . import control, db, envcheck, lifecycle, storage, winapi
from .capture import Recorder
from .config import Config, load_config
from .hotkey import HotkeyListener, parse_hotkey
from .embed import EmbedWorker
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


def _start_hotkey(cfg: Config, recorder: Recorder, conn, log: logging.Logger) -> HotkeyListener | None:
    """按配置准备快捷键补拍监听；未配置或写法有误时只提示，不阻止后台启动。"""
    spec = cfg.capture.snapshot_hotkey
    if not spec:
        control.set_hotkey_status(conn, "未配置快捷键，可在页面点击补拍")
        return None
    try:
        hk = parse_hotkey(spec)
    except ValueError as exc:
        log.warning("快捷键配置无效：%s", exc)
        control.set_hotkey_status(conn, f"快捷键配置无效（{spec}），可在页面点击补拍")
        return None

    status_conn_path = cfg.db_path

    def on_status(_ok: bool, text: str) -> None:
        c = db.connect(status_conn_path)  # 回调在热键线程里，使用自己的连接
        try:
            control.set_hotkey_status(c, text)
        finally:
            c.close()

    return HotkeyListener(hk, lambda: recorder.request_snapshot(None), on_status, log)


def _maintenance_loop(cfg: Config, recorder: Recorder, stop: threading.Event, log: logging.Logger) -> None:
    """到期与配额维护（方案 5.2）：启动时一次，之后按间隔执行。空间仍不足时暂停采集并提示。

    预留余量（默认 256 MiB）覆盖两次维护之间的最大写入量，使配额判断发生在磁盘耗尽之前。
    """
    conn = db.connect(cfg.db_path)
    try:
        swept = lifecycle.sweep_dead_files(conn, cfg.data_dir)
        if swept:
            log.info("清除了 %d 个已删除 / 已过期画面的残留图片", swept)
        while not stop.is_set():
            try:
                rep = lifecycle.run_maintenance(conn, cfg.data_dir, cfg.retention)
                if rep["paused"]:
                    recorder.storage_pause(conn, rep["reason"])
                else:
                    recorder.storage_clear()
                if rep["images_expired"] or rep["occurrences_removed"] or rep["quota_steps"]:
                    log.info("维护：%s", rep)
            except Exception:
                log.exception("维护失败，稍后重试")
            stop.wait(cfg.retention.maintenance_interval_seconds)
    finally:
        conn.close()


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
        threading.Thread(target=_maintenance_loop, args=(cfg, recorder, stop, log), name="maintenance", daemon=True),
    ]
    if report.models_ready:
        embedder = EmbedWorker(cfg, threading.Event(), stop, log)
        threads.append(threading.Thread(target=embedder.run, name="embed", daemon=True))
    else:
        log.warning("向量模型未就绪或已关闭：只建立文字索引，向量待办保留到模型就绪后补齐")
    listener = _start_hotkey(cfg, recorder, conn, log)
    if listener is not None:
        threads.append(threading.Thread(target=listener.run, name="hotkey", daemon=True))
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
            if control.snapshot_pending(ctrl):
                recorder.request_snapshot(int(ctrl["snapshot_seq"]))
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
        if listener is not None:
            listener.stop()
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
