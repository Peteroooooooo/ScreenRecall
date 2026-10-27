"""v2 录制子进程：python -m screenrecall.v2.recorder [--data-dir D] [--home H]

主进程把它作为受看护的子进程启动（stdin 是管道）。stdin 读到 EOF、收到 Ctrl+Break / Ctrl+C 时
在约 2 秒内收尾退出：停核心（候选帧存成证据帧）→ 读字线程处理完剩下的事件（不再读 UIA）→
记"录制停止"缺口、心跳写 stopped → 写线程写完、关库。

启动顺序（有讲究）：
1. winapi.enable_dpi_awareness()：之后的窗口矩形、UIA 矩形、WGC 帧都是物理像素；
2. 先导入 screenrecall.text.uia（在任何模块导入 comtypes.client 之前，comtypes 生成的代码才不会写进 site-packages）；
3. load_v2()；数据目录设成只有本人能读；schema.connect + Writer；sweep_frames() 收拾上次崩溃留下的"编码中"帧；
4. 收拾上次没正常退出留下的半截记录（没结束的缺口、活动），第一次建库时写入默认排除规则；
5. 先跑一次控制循环（暂停状态、规则、钉住在核心开始录之前就生效），再启动读字线程、核心、控制循环。

日志写到 <home>/logs/recorder.log（滚动），只记次数、耗时和状态，从不记屏幕文字和窗口标题。
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("screenrecall.v2.recorder")

STATS_EVERY_S = 60.0
SHUTDOWN_DEADLINE_S = 2.5


class RecorderProcess:
    """录制进程的各部分。测试时注入假的窗口系统 / 输入 / 截图来源 / UIA 读取器 / 窗口列表，直接调 step / tick。"""

    def __init__(self, cfg, *, core_kwargs: dict | None = None, reader=None,
                 window_list: Callable | None = None, clock: Callable[[], int] | None = None,
                 pid: int | None = None, private_dirs: bool = True):
        from ..rec.clock import now_ms

        self.cfg = cfg
        self.core_kwargs = dict(core_kwargs or {})
        self.reader = reader
        self.window_list = window_list
        self.clock = clock or now_ms
        self.pid = os.getpid() if pid is None else pid
        self.private_dirs = private_dirs
        self.writer = None
        self.core = None
        self.text = None
        self.sink = None
        self.gaps = None
        self.control = None
        self._started = False
        self._last_stats = time.monotonic()

    # ---------------- 启动 ----------------

    def open(self) -> None:
        """建库、收拾上次留下的记录、组装各部分（不启动线程）。"""
        from ..rec.core import RecorderCore
        from ..rec.windows import make_rules
        from ..store.writer import Writer
        from .rec_control import ControlLoop
        from .rec_sink import GlobalGaps, StoreSink
        from .rec_text import TextWorker

        cfg = self.cfg
        if self.private_dirs:
            from ..app.paths import ensure_private_dir

            ensure_private_dir(cfg.data_dir)
        else:
            Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        self.writer = Writer(cfg.data_dir, cfg.writer, clock=self.clock)
        n = self.writer.sweep_frames().result(60)
        if n:
            log.info("收拾了 %d 张上次没编码完的证据帧", n)
        row = self.writer.call(_recover, self.clock(), list(cfg.default_excluded_processes),
                               list(cfg.default_excluded_title_keywords)).result(60)
        if row.get("fresh"):
            log.info("新库：写入默认排除规则（%d 个进程、%d 个标题关键词）", len(cfg.default_excluded_processes),
                     len(cfg.default_excluded_title_keywords))
        if row.get("crashed"):
            log.info("上次录制进程没有正常退出：补了一段缺口")
        self.writer.heartbeat(recorder_state="starting", recorder_pid=self.pid, recorder_note=None,
                              current_hwnd=None, current_process=None, current_title=None, current_since_ms=None)

        if self.reader is None:
            from dataclasses import replace

            from ..text.uia import UiaReader

            ucfg = cfg.uia
            if not ucfg.comtypes_cache_dir:  # 跟着 home 走（SCREENRECALL_HOME / --home），不写到别处
                ucfg = replace(ucfg, comtypes_cache_dir=str(Path(cfg.home) / "comtypes_cache"))
            self.reader = UiaReader(ucfg)
        self.gaps = GlobalGaps(self.writer)
        self.text = TextWorker(self.writer, self.reader, min_gap_px=cfg.uia.min_gap_px)
        self.sink = StoreSink(self.writer, self.text, self.gaps)
        rules = make_rules(excluded_processes=row["excluded_processes"],
                           excluded_title_keywords=row["excluded_title_keywords"])
        self.core = RecorderCore(self.sink, cfg.recorder, rules=rules, clock=self.clock, **self.core_kwargs)
        self.sink.describe = self.core.windows.describe
        self.text.core = self.core
        self.text.sink = self.sink
        self.control = ControlLoop(self.writer, self.core, self.sink, self.gaps, cfg.db_path, pid=self.pid,
                                   clock=self.clock, window_list=self.window_list)
        self.control.tick()  # 暂停 / 规则 / 钉住先生效

    def start(self) -> None:
        if self.writer is None:
            self.open()
        self.text.start()
        self.core.start()
        self.control.start()
        self._started = True
        log.info("录制进程已启动 pid=%d", self.pid)

    # ---------------- 运行与收尾 ----------------

    def stats(self) -> dict:
        out = {"text": self.text.stats.summary() if self.text else {},
               "sink": dict(self.sink.counts) if self.sink else {}}
        if self.core is not None:
            st = self.core.stats()
            out["core"] = {"steps": st["steps"], "frames_fed": st["frames_fed"], "kinds": st["kinds"],
                           "sessions": len(st["sessions"]),
                           "trackers": len(st["trackers"]), "paused": st["paused"], "locked": st["locked"]}
        return out

    def log_stats(self, final: bool = False) -> None:
        log.info("recorder stats%s %s", " final" if final else "", json.dumps(self.stats(), ensure_ascii=False))

    def wait(self, stop: threading.Event) -> None:
        while not stop.wait(0.2):
            if time.monotonic() - self._last_stats >= STATS_EVERY_S:
                self._last_stats = time.monotonic()
                self.log_stats()

    def close(self) -> None:
        """收尾：停控制循环和核心（候选帧存成证据帧）→ 读字线程处理完 → 记录停止 → 关库。"""
        from .rec_sink import STOPPED_DETAIL

        t0 = time.perf_counter()
        if self.control is not None:
            self.control.stop()
        if self.core is not None:
            self.core.stop()
        if self.text is not None:
            self.text.stop(1.5)
        if self.writer is not None:
            now = self.clock()
            if self.sink is not None:
                self.sink.close_window_gaps(now)
            if self.gaps is not None:
                self.gaps.close_all(now)
                self.gaps.open("closed", now, STOPPED_DETAIL)
            self.writer.activity_end(now)
            self.writer.heartbeat(recorder_state="stopped", recorder_note=None, current_hwnd=None,
                                  current_process=None, current_title=None, current_since_ms=None)
            if self.text is not None:
                self.log_stats(final=True)
            try:
                self.writer.flush(10.0)
            except Exception:  # noqa: BLE001
                log.exception("收尾时写库没写完")
            self.writer.close()
            self.writer = None
        if self.reader is not None and hasattr(self.reader, "close"):
            try:
                self.reader.close()
            except Exception:  # noqa: BLE001
                pass
        log.info("录制进程已停止，收尾 %.0f ms", (time.perf_counter() - t0) * 1000)


def _recover(conn, now: int, default_procs: list, default_kws: list) -> dict:
    """（写线程里）上次没正常退出：没结束的缺口和活动在最后一次心跳时结束，补一段"录制进程没在运行"；
    上次正常退出留下的"录制停止"缺口在现在结束。第一次建库时写默认排除规则。返回 control 行（规则已解析）。"""
    from .rec_sink import STOPPED_DETAIL

    r = dict(conn.execute("SELECT * FROM control WHERE id = 1").fetchone())
    hb = int(r["recorder_heartbeat_ms"] or 0)
    crashed = hb > 0 and r["recorder_state"] not in ("stopped",)
    end = max(0, min(hb, now)) if hb else now
    conn.execute("UPDATE gaps SET end_ms = max(start_ms, ?) WHERE end_ms IS NULL AND reason = 'closed' "
                 "AND detail = ?", (now, STOPPED_DETAIL))
    conn.execute("UPDATE gaps SET end_ms = max(start_ms, ?) WHERE end_ms IS NULL", (end,))
    conn.execute("UPDATE activity SET end_ms = max(start_ms, ?) WHERE end_ms IS NULL", (end,))
    if crashed and now > hb:
        conn.execute("INSERT INTO gaps (start_ms, end_ms, reason, detail) VALUES (?, ?, 'error', ?)",
                     (hb, now, "recorder not running"))
    # 第一次在这个库上启动：默认排除规则写进 control（页面已经改过的列表不覆盖）
    fresh = conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('recorder_defaults_ms', ?)",
                         (str(now),)).rowcount == 1
    if fresh:
        procs = json.loads(r["excluded_processes"] or "[]") or list(default_procs)
        kws = json.loads(r["excluded_title_keywords"] or "[]") or list(default_kws)
        r["excluded_processes"] = json.dumps(procs, ensure_ascii=False)
        r["excluded_title_keywords"] = json.dumps(kws, ensure_ascii=False)
        conn.execute("UPDATE control SET excluded_processes = ?, excluded_title_keywords = ?, updated_ms = ?, "
                     "request_seq = request_seq + 1 WHERE id = 1",
                     (r["excluded_processes"], r["excluded_title_keywords"], now))
    out = dict(r)
    out["excluded_processes"] = json.loads(r["excluded_processes"] or "[]")
    out["excluded_title_keywords"] = json.loads(r["excluded_title_keywords"] or "[]")
    out["fresh"], out["crashed"] = fresh, crashed
    return out


class OnlyWindows:
    """测试隔离（--only-hwnds）：只有这些窗口能当前台 / 被钉住，别的窗口在前台时当成"没有可录的前台"，
    既不截图也不进活动日志。真实窗口测试用它保证只录自己开的窗口。"""

    def __init__(self, inner, allowed: set[int]):
        self._inner = inner
        self.allowed = set(allowed)

    def foreground(self) -> int:
        h = self._inner.foreground()
        return h if h in self.allowed else 0

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ---------------- 进程入口 ----------------

def _setup_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(logs_dir / "recorder.log", maxBytes=2_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


def _watch_stdin(stop: threading.Event) -> bool:
    """stdin 是管道 / 文件时，读到 EOF 就叫停（主进程关掉管道 = 让我们退出）。控制台交互运行时不看。"""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        return False
    try:
        if sys.stdin.isatty():
            return False
    except (ValueError, OSError):
        return False

    def run():
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass
        stop.set()

    threading.Thread(target=run, name="sr-recorder-stdin", daemon=True).start()
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.v2.recorder")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--home", default=None)
    ap.add_argument("--only-hwnds", default="", help=argparse.SUPPRESS)  # 测试隔离：逗号分隔的窗口句柄
    args = ap.parse_args(argv)
    only = {int(x, 0) for x in args.only_hwnds.split(",") if x.strip()}

    from .. import winapi

    winapi.enable_dpi_awareness()
    import screenrecall.text.uia  # noqa: F401 - 必须在任何模块导入 comtypes.client 之前

    from .config import load_v2

    cfg = load_v2(args.data_dir, home=Path(args.home) if args.home else None)
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
    _watch_stdin(stop)

    core_kwargs: dict = {}
    window_list = None
    if only:
        from ..rec.windows import RealWindows
        from .rec_control import real_window_list

        core_kwargs["windows"] = OnlyWindows(RealWindows(), only)
        window_list = lambda: [w for w in real_window_list() if w[0] in only]  # noqa: E731
        log.info("测试隔离：只录 %d 个指定窗口", len(only))
    proc = RecorderProcess(cfg, core_kwargs=core_kwargs, window_list=window_list)
    code = 0
    try:
        proc.start()
        proc.wait(stop)
    except Exception:  # noqa: BLE001
        log.exception("录制进程出错退出")
        code = 1
    finally:
        # 收尾超时（某个系统调用卡住）就强行退出：下次启动时 sweep_frames / 恢复逻辑会收拾
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
