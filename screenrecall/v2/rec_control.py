"""录制进程的控制循环：每 ~500 ms 读一次库里的 control 行（单独的只读连接），把页面的意图交给录制核心，
再写回心跳。

- 暂停：desired_state = 'paused' 且（没有 pause_until_ms 或还没到）→ 暂停；pause_until_ms 过了自动继续。
  暂停时先丢掉内存里的候选帧再暂停（隐私卫生：暂停前那一屏不再存盘），并记一条全局 paused 缺口；
- 排除规则：excluded_processes / excluded_title_keywords → RulesConfig → core.set_rules；规则变了也丢候选帧；
- 隐私删除：purges 表出现新行 → 丢候选帧、丢掉内容源/块映射（被删的内容源可能已清空标题）；
- 钉住的窗口：pinned_windows 是 [{"hwnd": 可选, "process": "...", "title": "..."}]（也接受纯数字 hwnd）。
  有 hwnd 且进程对得上就用它；否则要求进程名和标题都完全一致才算（钉错窗口 = 在后台录了用户没选的窗口，
  所以不做模糊匹配）。找不到的每 2 秒再找一次（窗口重新打开后能接上）；最多 MAX_PINNED 个；
- 锁屏：核心自己判断；这里按核心的状态补齐全局 locked 缺口（没有窗口在跟踪时跟踪器不会报）；
- 心跳：状态 / pid / 当前前台窗口（被排除的窗口不写进程名和标题）/ 备注，每 2 秒或状态变化时写一次。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, Callable, Iterable

from ..rec.windows import make_rules
from ..store import schema
from ..store.writer import Writer
from .rec_sink import EXCLUDED_TITLE, GlobalGaps, StoreSink

log = logging.getLogger(__name__)

MAX_PINNED = 2
WindowList = Callable[[], Iterable[tuple[int, str, str]]]  # [(hwnd, 进程名, 标题)]


def real_window_list() -> list[tuple[int, str, str]]:
    from .. import winapi

    return [(w.hwnd, w.process, w.title) for w in winapi.list_windows()]


def effective_paused(row: dict, now: int) -> bool:
    if row.get("desired_state") != "paused":
        return False
    until = row.get("pause_until_ms")
    return until is None or now < int(until)


def _pins(raw: Any) -> list[tuple[int | None, str | None, str | None]]:
    out = []
    for p in raw or []:
        if isinstance(p, int):
            out.append((p, None, None))
        elif isinstance(p, dict):
            h = p.get("hwnd")
            out.append((int(h) if h else None, p.get("process") or None, p.get("title") or None))
    return out


def resolve_pins(pins, windows: list[tuple[int, str, str]]) -> list[int | None]:
    """每个钉住项对应的窗口句柄（找不到为 None）。"""
    out: list[int | None] = []
    used: set[int] = set()
    for hwnd, process, title in pins:
        hit = None
        if hwnd:
            for h, proc, _ in windows:
                if h == hwnd and (not process or proc.casefold() == process.casefold()):
                    hit = h
                    break
        if hit is None and process and title:
            for h, proc, t in windows:
                if h not in used and proc.casefold() == process.casefold() and t == title:
                    hit = h
                    break
        if hit is not None:
            used.add(hit)
        out.append(hit)
    return out


class ControlLoop:
    def __init__(self, writer: Writer, core, sink: StoreSink, gaps: GlobalGaps, db_path, *, pid: int,
                 clock: Callable[[], int], window_list: WindowList | None = None, interval_s: float = 0.5,
                 heartbeat_ms: int = 2000, resolve_ms: int = 2000, note_fn: Callable[[], str | None] | None = None):
        self.writer = writer
        self.core = core
        self.sink = sink
        self.gaps = gaps
        self.db_path = db_path
        self.pid = pid
        self.clock = clock
        self.window_list = window_list or real_window_list
        self.interval_s = interval_s
        self.heartbeat_ms = heartbeat_ms
        self.resolve_ms = resolve_ms
        self.note_fn = note_fn
        self.paused: bool | None = None
        self._rules_key: tuple | None = None
        self._purge_id: int | None = None
        self._pins_raw: Any = None
        self._pinned: dict[int, int | None] = {}  # 钉住项下标 → 解析到的窗口
        self._applied: set[int] = set()  # 已经交给核心钉住的窗口
        self._last_resolve = -10**15
        self._last_hb = -10**15
        self._last_hb_key: tuple | None = None
        self._conn: sqlite3.Connection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.errors = 0

    # ---------------- 线程 ----------------

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="sr-recorder-control", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(5.0)
        self._close_conn()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 控制循环不能停
                self.errors += 1
                log.exception("控制循环这一轮出错")
                self._close_conn()  # 连接可能坏了：关掉，下一轮重开（只丢引用会一直漏连接）

    def _close_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    # ---------------- 一轮 ----------------

    def _read(self) -> tuple[dict, int]:
        if self._conn is None:
            self._conn = schema.connect_ro(self.db_path)
        r = self._conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
        row = dict(r)
        for k in ("pinned_windows", "excluded_processes", "excluded_title_keywords"):
            try:
                row[k] = json.loads(row[k] or "[]")
            except (TypeError, ValueError):
                row[k] = []
        p = self._conn.execute("SELECT COALESCE(MAX(purge_id), 0) FROM purges").fetchone()[0]
        return row, int(p)

    def tick(self, now: int | None = None) -> None:
        now = self.clock() if now is None else now
        row, purge_id = self._read()
        first = self.paused is None

        # 隐私删除
        if self._purge_id is not None and purge_id != self._purge_id:
            log.info("检测到新的隐私删除：丢掉候选帧和内容源映射")
            self.core.drop_candidates()
            self.sink.reset_blocks()
        self._purge_id = purge_id

        # 排除规则
        procs = tuple(str(x) for x in row["excluded_processes"] or ())
        kws = tuple(str(x) for x in row["excluded_title_keywords"] or ())
        if (procs, kws) != self._rules_key:
            if self._rules_key is not None:
                self.core.drop_candidates()
            self._rules_key = (procs, kws)
            self.core.set_rules(make_rules(excluded_processes=procs, excluded_title_keywords=kws))

        # 暂停 / 继续
        paused = effective_paused(row, now)
        if paused != self.paused:
            if paused:
                self.core.drop_candidates()
                self.core.pause()
                self.gaps.close("locked", now)
                self.gaps.open("paused", now)
            elif not first:
                self.core.resume()
                self.gaps.close("paused", now)
            self.paused = paused
            log.info("录制%s", "暂停" if paused else "继续")

        stats = self.core.stats()  # 线程安全（等当前这一轮 step 做完）

        # 钉住的窗口
        self._update_pins(now, row["pinned_windows"], set(stats.get("pinned", ())))

        # 锁屏缺口（核心没在跟踪任何窗口时跟踪器不会报）
        if not paused:
            if stats.get("locked"):
                self.gaps.open("locked", now)
            else:
                self.gaps.close("locked", now)

        self._heartbeat(now, paused, bool(stats.get("locked")))

    def _update_pins(self, now: int, raw: Any, core_pinned: set[int]) -> None:
        pins = _pins(raw)[:MAX_PINNED]
        if pins != self._pins_raw:
            # 名单变了（加了 / 去掉一项）：没变的项留着已经找到的窗口。全部重找的话，标题已经变了的窗口
            # （浏览器、聊天）按标题再也找不到，钉住就悄悄停了
            old = {(p[1], p[2]): self._pinned.get(i) for i, p in enumerate(self._pins_raw or ())}
            self._pins_raw = pins
            self._pinned = {i: old[(p[1], p[2])] for i, p in enumerate(pins) if old.get((p[1], p[2])) is not None}
            self._last_resolve = -10**15
        if not pins and not self._applied:
            return
        # 核心发现窗口关了会自己去掉钉住：那一项重新找
        for i, h in list(self._pinned.items()):
            if h is not None and h in self._applied and h not in core_pinned:
                self._applied.discard(h)
                self._pinned[i] = None
        unresolved = any(self._pinned.get(i) is None for i in range(len(pins)))
        if unresolved and now - self._last_resolve >= self.resolve_ms:
            self._last_resolve = now
            try:
                wins = list(self.window_list())
            except Exception:  # noqa: BLE001
                log.exception("列出窗口失败")
                wins = []
            for i, h in enumerate(resolve_pins(pins, wins)):
                if self._pinned.get(i) is None:
                    self._pinned[i] = h
        want = {h for h in self._pinned.values() if h is not None}
        for h in self._applied - want:
            self.core.unpin(h)
        for h in want - self._applied:
            self.core.pin(h)
        self._applied = want

    def _heartbeat(self, now: int, paused: bool, locked: bool) -> None:
        state = "paused" if paused else "recording"
        cur = None if paused or locked else self.sink.current
        if cur is None:
            hwnd = process = title = since = None
        elif cur[3]:  # 被排除的窗口：不写进程名和标题
            hwnd, process, title, since = None, None, EXCLUDED_TITLE, cur[4]
        else:
            hwnd, process, title, since = cur[0], cur[1], cur[2], cur[4]
        note = "locked" if locked and not paused else None
        if self.note_fn is not None and note is None:
            try:
                note = self.note_fn()
            except Exception:  # noqa: BLE001
                note = None
        key = (state, hwnd, process, title, since, note)
        if key == self._last_hb_key and now - self._last_hb < self.heartbeat_ms:
            return
        self._last_hb_key = key
        self._last_hb = now
        self.writer.heartbeat(recorder_state=state, recorder_pid=self.pid, recorder_note=note, current_hwnd=hwnd,
                              current_process=process, current_title=title, current_since_ms=since)
