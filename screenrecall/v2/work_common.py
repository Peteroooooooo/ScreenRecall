"""空闲整理进程的公共部分：上下文（配置、写线程、只读连接、停止标志）、进度游标、统计。

进度都存在库里（meta 的 worker.* 键、jobs 表、ocr_regions.status），进程随时被杀掉也能接着做。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..store import schema
from ..store.queries import Hidden
from ..store.writer import Writer
from ..timeutil import utc_ms

log = logging.getLogger("screenrecall.v2.worker")


class Stopped(Exception):
    """收到停止请求（stdin 关闭）：当前这一项不做了，进度已在库里。"""


class StopFlag:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise Stopped()

    def wait(self, seconds: float) -> bool:
        """睡 seconds 秒，期间收到停止就提前返回 True。"""
        return self._ev.wait(max(0.0, seconds))


@dataclass
class StepStats:
    items: int = 0           # 处理了多少项（区域、行、片段……）
    seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, n: int = 1) -> None:
        self.items += n


class Ctx:
    """一次空闲整理用到的东西。读走自己的只读连接，写全部经 Writer（单写线程，短事务）。"""

    def __init__(self, cfg, writer: Writer, stop: StopFlag, *, clock: Callable[[], int] = utc_ms):
        self.cfg = cfg
        self.writer = writer
        self.stop = stop
        self.clock = clock
        self.data_dir = writer.data_dir
        self._conn: sqlite3.Connection | None = None
        self.stats: dict[str, StepStats] = {}
        self._said: set[str] = set()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = schema.connect_ro(self.writer.db_path)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def stat(self, name: str) -> StepStats:
        return self.stats.setdefault(name, StepStats())

    def once(self, key: str, msg: str, *args, level: int = logging.WARNING) -> None:
        """同一条提示只记一次（缺模型之类）。"""
        if key in self._said:
            return
        self._said.add(key)
        log.log(level, msg, *args)

    # ---------------- 游标（meta 的 worker.* 键） ----------------

    def meta_get(self, key: str, default: int = 0) -> int:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        try:
            return int(r[0]) if r else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def meta_put_sql(conn, key: str, value: int) -> None:
        """在写线程的事务里设游标（和它对应的内容写在同一个操作里，崩溃时一起生效或一起不生效）。"""
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(int(value))))

    def meta_put(self, key: str, value: int) -> None:
        self.writer.call(self.meta_put_sql, key, value).result(30)

    # ---------------- 控制行：隐藏 / 排除名单 ----------------

    def control(self) -> dict:
        r = self.conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
        d = dict(r) if r else {}
        for k in ("excluded_processes", "excluded_title_keywords", "hidden_from_agents"):
            try:
                d[k] = json.loads(d.get(k) or "null")
            except ValueError:
                d[k] = None
        return d

    def private(self) -> Hidden:
        """不能发给外部大模型的应用 / 标题：对 Agent 隐藏的 + 录制排除规则（规则可能是录完之后才加的）。"""
        c = self.control()
        hidden = c.get("hidden_from_agents") or {}
        apps = list(hidden.get("apps") or []) + list(c.get("excluded_processes") or [])
        apps += list(self.cfg.default_excluded_processes)
        kws = list(hidden.get("title_keywords") or []) + list(c.get("excluded_title_keywords") or [])
        kws += list(self.cfg.default_excluded_title_keywords)
        return Hidden([a for a in apps if isinstance(a, str)], [k for k in kws if isinstance(k, str)])


def interruptible(stop: StopFlag, fn: Callable[..., Any], *args: Any, name: str = "work-item",
                  poll_s: float = 0.05) -> Any:
    """在守护线程里跑 fn(*args)，主线程每 poll_s 看一次停止标志：收到停止立即抛 Stopped，不等它算完
    （模型推理一次可能好几秒，比主进程给的 3 秒宽限还长）。线程是守护线程，进程退出时一起结束；
    它只做计算，不碰库，调用方负责把这一项的领取放回去。"""
    stop.check()
    box: dict[str, Any] = {}

    def call() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    th = threading.Thread(target=call, name=name, daemon=True)
    th.start()
    while th.is_alive():
        th.join(poll_s)
        if stop.is_set() and th.is_alive():
            raise Stopped()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def timed(ctx: Ctx, name: str):
    """with timed(ctx, 'ocr') as st: ... —— 累计这一步用时。"""
    return _Timed(ctx.stat(name))


class _Timed:
    def __init__(self, st: StepStats):
        self.st = st

    def __enter__(self) -> StepStats:
        self.t0 = time.perf_counter()
        return self.st

    def __exit__(self, *exc) -> None:
        self.st.seconds += time.perf_counter() - self.t0


def read_image(path: str):
    """读证据图（WebP）为 BGR。用 imdecode：cv2.imread 在 Windows 上读不了非 ASCII 路径。"""
    import cv2
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("证据图解码失败")
    return img
