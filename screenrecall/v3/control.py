"""录制进程的控制线程：每秒读一次 control 行，把主进程（网页、托盘）的意图交给捕获循环和处理线程，再写心跳。

- 暂停：paused_until_ms 在将来（PAUSE_FOREVER_MS = 到手动继续）→ loop.set_paused(True)；过了时间自动继续；
- 名单 / 不录规则：control.rev 变了才重读 → loop.set_tiers(store.app_tiers())、loop.set_excludes(标题关键词)、
  pipeline.set_excluded_hosts(网站)；
- 隐私删除：meta 'delete_rev' 变了（主进程 Store.delete_range 每删一次 +1）→ pipeline.forget_deleted()，
  处理线程不再往删掉的条目里写；
- 心跳：每 2 秒（或状态变了时）写 recorder_pid / recorder_state / current_app / current_title（没在录、
  或这个页面按网址跳过了时为 NULL）。
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

INTERVAL_S = 1.0
HEARTBEAT_MS = 2000


class ControlLoop:
    def __init__(self, cfg, store, loop, pipeline, *, pid: int, clock=None):
        from ..rec.clock import now_ms

        self.cfg = cfg
        self.store = store
        self.loop = loop
        self.pipeline = pipeline
        self.pid = int(pid)
        self.clock = clock or now_ms
        self.paused: bool | None = None
        self._rev: int | None = None
        self._delete_rev: str | None = None
        self._rev_seen = False       # 第一轮只记下 delete_rev（启动前的删除和这个进程无关）
        self._last_hb = -10**15
        self._last_key: tuple | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.errors = 0

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="sr-v3-control", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(3.0)

    def _run(self) -> None:
        while not self._stop.wait(INTERVAL_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 控制线程不能停
                self.errors += 1
                log.exception("控制线程这一轮出错")

    def tick(self, now: int | None = None) -> None:
        now = self.clock() if now is None else now
        row = self.store.control() or {}

        until = row.get("paused_until_ms")
        paused = until is not None and int(until) > now
        if paused != self.paused:
            if self.paused is not None or paused:
                log.info("录制%s", "暂停" if paused else "继续")
            self.paused = paused
        self.loop.set_paused(paused)

        rev = int(row.get("rev") or 0)
        if rev != self._rev:
            self._rev = rev
            tiers = self.store.app_tiers()
            excludes = self.store.excludes()
            self.loop.set_tiers(tiers)
            self.loop.set_excludes([e["value"] for e in excludes if e.get("kind") == "title"])
            self.pipeline.set_excluded_hosts([e["value"] for e in excludes if e.get("kind") == "host"])
            log.info("名单：%d 个进程、%d 条不录规则", len(tiers), len(excludes))

        delete_rev = self.store.meta_get("delete_rev")
        if self._rev_seen and delete_rev != self._delete_rev:
            self.pipeline.forget_deleted(now)
        self._delete_rev, self._rev_seen = delete_rev, True

        state = "paused" if paused else self.loop.state
        cur = self.loop.current if state == "recording" else None
        app, title = cur if cur is not None else (None, None)
        hwnd = getattr(self.loop, "current_hwnd", None)
        if cur is not None and hwnd and self.pipeline.is_skipped(hwnd, title):
            app = title = None  # 按网址跳过的页面（自己的网页、内部页、不录网站）：标题不进库
        key = (state, app, title)
        if key != self._last_key or now - self._last_hb >= HEARTBEAT_MS:
            self._last_key = key
            self._last_hb = now
            self.store.heartbeat(recorder_pid=self.pid, recorder_state=state, current_app=app, current_title=title)
