"""空闲整理进程：电脑空闲时由主进程启动（低优先级，stdin 接管道），做完一轮就退出。

    python -m screenrecall.v3.worker --data-dir D --home H [--once] [--no-llm]

按顺序做：
1. 删过期截图：Store.expire_shots(现在 - image_days 天)；字一直留着；
2. 可选：大模型起名字、每小时一句话（names）。cfg.llm_names 打开且 .env 配了大模型（LLM_BASE_URL / LLM_API_KEY /
   LLM_MODEL，读 <仓库>/.env 和 <home>/.env）才做，两步合计最多 cfg.llm_max_calls 次；没配就跳过（页面显示标题）；
3. 可选：文字向量（vectors，cfg.vectors 默认关）。
v3 每次就做这一轮（要做的事很少）：做完退出码 0，主进程下次空闲再启动；--once 和 run(once=True) 保留给命令行和测试，
效果相同。每一项之间看停止标记：stdin 读到 EOF（主进程发现用户回来了会关掉管道）或 Ctrl+Break / Ctrl+C，
在当前这一项做完时停下（等大模型回答时立即放弃）。同一数据目录同时只允许一个整理进程（data_dir/worker.lock）。
日志 <home>/logs/idle-worker.log 只记数量和用时，不记屏幕内容。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..timeutil import utc_ms
from . import names, stdin_eof
from .config import V3Config

log = logging.getLogger("screenrecall.v3.worker")

LOCK_NAME = "worker.lock"
LOG_NAME = "idle-worker.log"
LOG_MAX_BYTES = 2_000_000
DAY_MS = 86_400_000


class StopFlag:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def wait(self, seconds: float) -> bool:
        """睡 seconds 秒，期间收到停止就提前返回 True。"""
        return self._ev.wait(max(0.0, seconds))


class FileLock:
    """跨进程的非阻塞文件锁（锁文件第一个字节）：拿不到说明别的进程（或本进程的另一处）正拿着。
    进程被杀时系统自动放锁。"""

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def instance_lock(data_dir: Path) -> FileLock:
    """data_dir/worker.lock：拿不到说明已有一个整理进程在跑。"""
    return FileLock(Path(data_dir) / LOCK_NAME)


class _Counted:
    """包一层大模型客户端：数一共调了几次（起名字和每小时一句话合计不超过 llm_max_calls）。"""

    def __init__(self, client):
        self.client = client
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return self.client.chat(messages, **kw)


def default_llm_factory(cfg: V3Config, stop: StopFlag) -> Callable[[], object]:
    def make():
        from ..app import llm

        return llm.LlmClient.from_env(env_files=[llm.REPO_ROOT / ".env", Path(cfg.home) / ".env"],
                                      sleep=lambda s: stop.wait(s))
    return make


class Worker:
    """store / queries 不给时自己开 v3.store 的 Store / Queries（用完关掉）；llm_factory / encoder_factory 是零参函数，
    测试换成假的。use_llm=False 对应命令行 --no-llm。"""

    def __init__(self, cfg: V3Config, *, store=None, queries=None, llm_factory=None, encoder_factory=None,
                 stop: StopFlag | None = None, clock: Callable[[], int] | None = None, use_llm: bool = True):
        self.cfg = cfg
        self.store, self.queries = store, queries
        self.stop = stop or StopFlag()
        self.clock = clock or utc_ms
        self.use_llm = use_llm
        self._llm_factory = llm_factory or default_llm_factory(cfg, self.stop)
        self._encoder_factory = encoder_factory
        self._llm: _Counted | None = None
        self._llm_off = False

    # ---------------- 大模型 ----------------

    def _client(self) -> _Counted | None:
        """配了大模型就返回（计数包装过的）客户端；没配 / 关掉 / 建不起来返回 None（只记一次日志）。"""
        if self._llm is None and not self._llm_off:
            if not (self.use_llm and self.cfg.llm_names):
                self._llm_off = True
                return None
            try:
                self._llm = _Counted(self._llm_factory())
            except Exception as exc:  # noqa: BLE001 —— LlmNotConfigured 等：页面显示标题
                from ..app.llm import error_brief

                self._llm_off = True
                log.info("大模型没有配置或不可用，跳过起名字和每小时一句话：%s", error_brief(exc))
        return self._llm

    def _budget(self) -> int:
        return max(0, int(self.cfg.llm_max_calls) - (self._llm.calls if self._llm else 0))

    # ---------------- 各步 ----------------

    def _expire(self) -> int:
        return int(self.store.expire_shots(self.clock() - int(self.cfg.image_days) * DAY_MS) or 0)

    def _names(self) -> int:
        llm = self._client()
        if llm is None or self._budget() <= 0:
            return 0
        return names.name_sources(self.queries, self.store, llm, max_calls=self._budget(), stop=self.stop)

    def _hours(self) -> int:
        llm = self._client()
        if llm is None or self._budget() <= 0:
            return 0
        return names.hour_lines(self.queries, self.store, llm, before_ms=self.clock(), max_calls=self._budget(),
                                stop=self.stop)

    def _vectors(self) -> int:
        if not self.cfg.vectors:
            return 0
        from .vectors import LineVectors

        enc = self._encoder_factory() if self._encoder_factory is not None else None
        lv = LineVectors(self.cfg, encoder=enc)
        try:
            return lv.update(self.queries, self.store, self.stop)
        finally:
            lv.close()

    def run(self, once: bool = False) -> dict:
        """做一轮，返回 {"steps": {名字: 数量}, "llm_calls", "stopped", "seconds"}；出错的一步记日志后跳过（数量记 None）。"""
        t0 = time.perf_counter()
        lock = instance_lock(self.cfg.data_dir)
        if not lock.acquire():
            log.info("已有一个空闲整理进程在处理这个数据目录，这次不做")
            return {"steps": {}, "llm_calls": 0, "stopped": False, "locked": True, "seconds": 0.0}
        try:
            return self._run(t0)
        finally:
            lock.release()

    def _run(self, t0: float) -> dict:
        own = []
        if self.store is None:
            from .store.writer import Store

            self.store = Store(self.cfg, clock=self.clock)
            own.append(self.store)
        if self.queries is None:
            from .store.queries import Queries

            self.queries = Queries(self.cfg, clock=self.clock)
            own.append(self.queries)
        steps: dict[str, int | None] = {}
        try:
            for name, fn in (("expire_shots", self._expire), ("names", self._names), ("hour_lines", self._hours),
                             ("vectors", self._vectors)):
                if self.stop.is_set():
                    break
                try:
                    steps[name] = fn()
                except Exception:  # noqa: BLE001 —— 一步出错不拖累别的步骤
                    log.exception("整理步骤 %s 出错，本次跳过", name)
                    steps[name] = None
        finally:
            for x in own:
                x.close()
            if self.store in own:
                self.store = None
            if self.queries in own:
                self.queries = None
        return {"steps": steps, "llm_calls": self._llm.calls if self._llm else 0, "stopped": self.stop.is_set(),
                "locked": False, "seconds": round(time.perf_counter() - t0, 3)}


# ---------------- 进程入口 ----------------

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


def watch_stop(stop: StopFlag) -> None:
    """stdin EOF（主进程关了管道）和 Ctrl+Break / Ctrl+C 都置停止标志。"""
    stdin_eof.watch(stop.set)
    for name in ("SIGBREAK", "SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: stop.set())
            except (ValueError, OSError):  # 不在主线程（测试里）
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.v3.worker", description="空闲整理进程")
    ap.add_argument("--data-dir", default=None, help="v3 数据目录（默认按配置）")
    ap.add_argument("--home", default=None, help="应用目录（默认 SCREENRECALL_HOME 或 %%LOCALAPPDATA%%\\ScreenRecall）")
    ap.add_argument("--once", action="store_true", help="只做一轮（v3 本来就只做一轮，保留给测试）")
    ap.add_argument("--no-llm", action="store_true", help="不调大模型（不起名字、不写每小时一句话）")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    home = Path(args.home).resolve() if args.home else None
    if home:
        os.environ["SCREENRECALL_HOME"] = str(home)
    from ..app.paths import ensure_private_dir
    from .config import load_v3

    cfg = load_v3(args.data_dir, home=home)
    setup_logging(cfg.logs_dir, getattr(logging, str(args.log_level).upper(), logging.INFO))
    stop = StopFlag()
    watch_stop(stop)
    ensure_private_dir(cfg.data_dir)
    try:
        report = Worker(cfg, stop=stop, use_llm=not args.no_llm).run(once=args.once)
    except Exception:  # noqa: BLE001
        log.exception("空闲整理进程出错退出")
        return 1
    log.info("整理结束：%s", json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
