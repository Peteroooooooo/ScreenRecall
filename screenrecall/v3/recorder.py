"""v3 录制子进程：python -m screenrecall.v3.recorder --data-dir D --home H
    [--only-hwnds 0x1A2B,1234] [--only-pids 4321] [--no-ocr] [--exit-after-s 30]   （后面几个是测试用的隐藏参数）

主进程把它作为受看护的子进程启动（stdin 是管道）。stdin 读到 EOF、收到 Ctrl+Break / Ctrl+C、或到了 --exit-after-s 时收尾退出：
捕获循环停（发 FocusEvent(0)、结束灰色时段）→ 处理线程处理完（当前条目离开、候选图存下）→ heartbeat(recorder_state="stopped")
→ 关库；收尾超过 3 秒强行退出（下次启动时 Store.recover 收拾半截的条目和灰色时段）。

启动顺序（有讲究）：
1. winapi.enable_dpi_awareness()：之后的窗口矩形、UIA 矩形、WGC 帧都是物理像素；
2. 先导入 screenrecall.text.uia（在任何模块导入 comtypes.client 之前，comtypes 生成的代码才不会写进 site-packages）；
3. load_v3 → 数据目录设成只有本人能读 → Store → recover → init_defaults(cfg.default_record_apps)；
4. control 先 tick 一次（暂停状态、名单、不录规则在捕获循环开始之前就生效）→ 处理线程 / 捕获循环 / 剪贴板 / control 线程。

测试隔离（--only-hwnds / --only-pids，和环境变量 SCREENRECALL_TEST_ONLY_HWNDS / _PIDS 合并）：只有允许的窗口能当前台 / 后台录，
别的窗口在前台时当成"没有要录的前台"——不截图、不记 unlisted 灰色时段、不记复制。两个都给时两个条件都要满足。
日志写到 <home>/logs/recorder.log（滚动），只记次数、耗时和状态，从不记屏幕文字、窗口标题和网址。
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("screenrecall.v3.recorder")

STATS_EVERY_S = 60.0
SHUTDOWN_DEADLINE_S = 3.0

# 只给测试用：逗号分隔的窗口句柄 / 进程号。主进程启动的录制子进程继承主进程的环境变量（supervisor.child_env），
# 所以端到端测试在启动主进程前设好它们，真实的录制进程就只录测试自己开的窗口。和隐藏参数合并（两边的都允许）。
TEST_ONLY_HWNDS_ENV = "SCREENRECALL_TEST_ONLY_HWNDS"
TEST_ONLY_PIDS_ENV = "SCREENRECALL_TEST_ONLY_PIDS"


def only_ids(*specs: str | None) -> set[int]:
    """'0x1A2B,1234' 这样的列表（十进制或 0x 十六进制）合并成集合；写错的项直接报错，不悄悄录全部窗口。"""
    out: set[int] = set()
    for spec in specs:
        for x in (spec or "").split(","):
            if x.strip():
                out.add(int(x.strip(), 0))
    return out


class OnlyWindows:
    """测试隔离：只有允许的窗口能当前台，别的窗口在前台时当成"没有要录的前台"。"""

    def __init__(self, inner, *, hwnds: set[int] | None = None, pids: set[int] | None = None):
        self._inner = inner
        self.hwnds = set(hwnds or ())
        self.pids = set(pids or ())

    def allowed(self, hwnd: int) -> bool:
        if not hwnd:
            return False
        if self.hwnds and hwnd not in self.hwnds:
            return False
        if self.pids:
            try:
                return self._inner.pid(hwnd) in self.pids
            except Exception:  # noqa: BLE001
                return False
        return True

    def foreground(self) -> int:
        h = self._inner.foreground()
        return h if self.allowed(h) else 0

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _NoOcr:
    """--no-ocr：不加载 OCR 模型，只用系统文字。"""

    def recognize(self, bgr) -> list:
        return []


class RecorderProcess:
    """录制进程的各部分。测试时注入假的窗口系统 / 输入 / 截图来源 / Store / 读字器，直接调各部分的 step / process / tick。"""

    def __init__(self, cfg, *, windows=None, inputs=None, source_factory=None, window_list: Callable | None = None,
                 store=None, reader=None, clock: Callable[[], int] | None = None, pid: int | None = None,
                 private_dirs: bool = True, no_ocr: bool = False, clipboard: bool = True):
        from ..rec.clock import now_ms

        self.cfg = cfg
        self.windows = windows
        self.inputs = inputs
        self.source_factory = source_factory
        self.window_list = window_list
        self.store = store
        self.reader = reader
        self.clock = clock or now_ms
        self.pid = os.getpid() if pid is None else pid
        self.private_dirs = private_dirs
        self.no_ocr = no_ocr
        self.use_clipboard = clipboard
        self.events: queue.Queue = queue.Queue()
        self.loop = None
        self.pipeline = None
        self.control = None
        self.clipboard = None
        self._last_stats = time.monotonic()

    def open(self) -> None:
        """建库、收拾上次留下的记录、组装各部分（不启动线程）。"""
        from .control import ControlLoop
        from .loop import CaptureLoop
        from .pipeline import Pipeline

        cfg = self.cfg
        if self.private_dirs:
            from ..app.paths import ensure_private_dir

            ensure_private_dir(cfg.data_dir)
        else:
            Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        if self.store is None:
            from .store.writer import Store

            self.store = Store(cfg, clock=self.clock)
        rec = self.store.recover(self.clock())
        if rec.get("crashed"):
            log.info("上次录制进程没有正常退出：收拾了半截的条目和灰色时段")
        if self.store.init_defaults(cfg.default_record_apps):
            log.info("新库：名单写入 %d 个默认软件", len(cfg.default_record_apps))
        self.store.heartbeat(recorder_state="starting", recorder_pid=self.pid, current_app=None, current_title=None)

        if self.reader is None and self.no_ocr:
            from .read import TextReader

            self.reader = TextReader(cfg, ocr=_NoOcr())
        self.loop = CaptureLoop(cfg, self.events, windows=self.windows, inputs=self.inputs,
                                source_factory=self.source_factory, clock=self.clock, window_list=self.window_list)
        self.pipeline = Pipeline(cfg, self.events, store=self.store, reader=self.reader, clock=self.clock)
        self.control = ControlLoop(cfg, self.store, self.loop, self.pipeline, pid=self.pid, clock=self.clock)
        self.control.tick()  # 暂停 / 名单 / 不录规则先生效
        if self.use_clipboard:
            from .clipboard import ClipboardWatcher

            self.clipboard = ClipboardWatcher(self.loop.on_copy, clock=self.clock)

    def start(self) -> None:
        if self.loop is None:
            self.open()
        self.pipeline.start()
        self.loop.start()
        if self.clipboard is not None:
            self.clipboard.start()
        self.control.start()
        log.info("录制进程已启动 pid=%d", self.pid)

    def stats(self) -> dict:
        out: dict = {}
        if self.loop is not None:
            out["loop"] = self.loop.stats()
        if self.pipeline is not None:
            p = self.pipeline
            out["pipeline"] = dict(p.counts)
            n = p.timing["snaps"]
            out["read_ms_avg"] = round(p.timing["read_ms"] / n, 1) if n else None
            rs = getattr(p._reader, "stats", None)
            if isinstance(rs, dict):  # 读字器的次数：OCR 几次、总共多少毫秒、跳过几次、UIA 超时几次
                out["read"] = dict(rs)
                k = rs.get("ocr") or 0
                out["ocr_ms_avg"] = round(rs.get("ocr_ms", 0.0) / k, 1) if k else None
            out["queue"] = self.events.qsize()
        if self.clipboard is not None:
            out["clipboard"] = {"updates": self.clipboard.updates, "copies": self.clipboard.copies}
        return out

    def log_stats(self, final: bool = False) -> None:
        log.info("recorder stats%s %s", " final" if final else "", json.dumps(self.stats(), ensure_ascii=False))

    def wait(self, stop: threading.Event) -> None:
        while not stop.wait(0.2):
            if time.monotonic() - self._last_stats >= STATS_EVERY_S:
                self._last_stats = time.monotonic()
                self.log_stats()

    def close(self, budget_s: float = SHUTDOWN_DEADLINE_S) -> None:
        """收尾：control 停 → 剪贴板停 → 捕获循环停 → 处理线程处理完 → 心跳 stopped → 关库。"""
        t0 = time.monotonic()
        if self.control is not None:
            self.control.stop()
        if self.clipboard is not None:
            self.clipboard.stop()
        if self.loop is not None:
            self.loop.stop()
        if self.pipeline is not None:
            left = budget_s - (time.monotonic() - t0) - 0.5
            self.pipeline.stop(max(0.5, left))
        if self.store is not None:
            try:
                self.store.heartbeat(recorder_state="stopped", current_app=None, current_title=None)
            except Exception:  # noqa: BLE001
                log.exception("写停止心跳失败")
            if self.loop is not None:
                self.log_stats(final=True)
            self.store.close()
            self.store = None
        log.info("录制进程已停止，收尾 %.0f ms", (time.monotonic() - t0) * 1000)


# ---------------- 进程入口 ----------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.v3.recorder")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--home", default=None)
    ap.add_argument("--only-hwnds", default="", help=argparse.SUPPRESS)   # 测试隔离：逗号分隔的窗口句柄
    ap.add_argument("--only-pids", default="", help=argparse.SUPPRESS)    # 测试隔离：逗号分隔的进程号
    ap.add_argument("--no-ocr", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--exit-after-s", type=float, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.hwnds = only_ids(args.only_hwnds, os.environ.get(TEST_ONLY_HWNDS_ENV, ""))
    args.pids = only_ids(args.only_pids, os.environ.get(TEST_ONLY_PIDS_ENV, ""))
    return args


def isolation(hwnds: set[int], pids: set[int]) -> dict:
    """测试隔离要注入的部件（windows / window_list）；不隔离时空 dict。"""
    if not hwnds and not pids:
        return {}
    from ..rec.windows import RealWindows
    from .loop import real_window_list

    only = OnlyWindows(RealWindows(), hwnds=hwnds, pids=pids)
    return {"windows": only, "window_list": lambda: [w for w in real_window_list() if only.allowed(w[0])]}


def _setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(logs_dir / "recorder.log", maxBytes=2_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from .. import winapi

    winapi.enable_dpi_awareness()
    import screenrecall.text.uia  # noqa: F401 - 必须在任何模块导入 comtypes.client 之前

    from .config import load_v3

    cfg = load_v3(args.data_dir, home=Path(args.home) if args.home else None)
    if args.home:
        from ..app.paths import ensure_private_dir

        ensure_private_dir(cfg.home)
    _setup_logging(cfg.logs_dir)

    stop = threading.Event()

    def on_signal(signum, _frame):
        stop.set()

    for name in ("SIGBREAK", "SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
    from . import stdin_eof

    stdin_eof.watch(stop.set, name="sr-recorder-stdin")
    if args.exit_after_s is not None:
        t = threading.Timer(max(0.0, args.exit_after_s), stop.set)
        t.daemon = True
        t.start()

    kwargs = isolation(args.hwnds, args.pids)
    if kwargs:
        log.info("测试隔离：只录 %d 个窗口句柄、%d 个进程的窗口", len(args.hwnds), len(args.pids))
    proc = RecorderProcess(cfg, no_ocr=args.no_ocr, **kwargs)
    code = 0
    try:
        proc.start()
        proc.wait(stop)
    except Exception:  # noqa: BLE001
        log.exception("录制进程出错退出")
        code = 1
    finally:
        # 收尾超时（某个系统调用卡住）就强行退出：下次启动时 Store.recover 会收拾
        def deadline():
            log.warning("收尾超过 %.1f 秒，强行退出", SHUTDOWN_DEADLINE_S)
            logging.shutdown()
            os._exit(3)

        timer = threading.Timer(SHUTDOWN_DEADLINE_S, deadline)
        timer.daemon = True
        timer.start()
        try:
            proc.close()
        except Exception:  # noqa: BLE001
            log.exception("收尾出错")
            code = code or 1
        timer.cancel()
    logging.shutdown()
    return code


if __name__ == "__main__":
    # 正常收尾后直接结束进程：别的库留下的后台线程（WGC、COM）不会拖住解释器退出
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
