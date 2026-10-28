"""主进程的托盘控制（app.main.Controller：暂停 / 继续 / 删最近 / 状态）和 MCP 的 Agent 策略。

暂停 / 继续只写 control 行（Store.set_control），录制进程每秒读一次；删最近 N 分钟是真删（Store.delete_range）。
状态从 Queries.status() 现读（托盘每 5 秒问一次，读一行很便宜，不缓存）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from ..app import mcp_server as ms
from ..app.tray import TrayStatus
from ..timeutil import utc_ms
from .types import PAUSE_FOREVER_MS

log = logging.getLogger("screenrecall.v3.controller")


def _until_text(until_ms: int, now_ms: int) -> str:
    """暂停到几点：今天的只写 14:30，别的日子带日期。"""
    d, n = datetime.fromtimestamp(until_ms / 1000), datetime.fromtimestamp(now_ms / 1000)
    return f"{d:%H:%M}" if d.date() == n.date() else f"{d:%m-%d %H:%M}"


class TrayController:
    def __init__(self, store, queries, *, clock: Callable[[], int] | None = None):
        self.store, self.queries = store, queries
        self.clock = clock or utc_ms

    def pause_until(self, until: datetime | None) -> None:
        """until 为 None：一直暂停到手动继续。"""
        ms_ = PAUSE_FOREVER_MS if until is None else int(until.timestamp() * 1000)
        self.store.set_control(paused_until_ms=ms_)

    def resume(self) -> None:
        self.store.set_control(paused_until_ms=None)

    def delete_recent(self, minutes: int) -> None:
        now = self.clock()
        rep = self.store.delete_range(now - int(minutes) * 60_000, now)
        log.info("已删除最近 %d 分钟：%s", minutes, {k: v for k, v in (rep or {}).items() if isinstance(v, int)})

    def status(self) -> TrayStatus:
        try:
            st = self.queries.status(self.clock())
        except Exception:  # noqa: BLE001 —— 托盘不能因为读库失败而崩
            log.warning("读取录制状态失败", exc_info=True)
            return TrayStatus("读取状态失败")
        until = st.get("paused_until_ms")
        now = int(st.get("now_ms") or self.clock())
        # 录制进程没在运行时 state 是 stopped，暂停照样记着：托盘要显示"已暂停"和"继续"（和网页 isPaused 一样）
        if st.get("state") == "paused" or st.get("paused_forever") or (until is not None and int(until) > now):
            if st.get("paused_forever") or until is None or int(until) >= PAUSE_FOREVER_MS:
                return TrayStatus("已暂停（到手动继续）", paused=True)
            return TrayStatus(f"已暂停到 {_until_text(int(until), now)}", paused=True)
        if not st.get("recorder_alive") or st.get("state") == "stopped":
            return TrayStatus("录制进程没在运行")
        if st.get("state") == "locked":
            return TrayStatus("锁屏中（不录）")
        cur = st.get("current") or {}
        return TrayStatus(f"在录：{cur['app']}" if cur.get("app") else "在录（前台没有要录的窗口）")


def agent_policy(queries, *, ttl_s: float = 1.0, clock: Callable[[], float] = time.monotonic) -> ms.AgentPolicy:
    """MCP 的服务端策略：enabled = control.agent_enabled（每次调用工具时现读，1 秒缓存）；读失败时当作关闭。
    v3 没有"对 Agent 隐藏"的名单：exclusion 用默认的空规则。"""
    lock = threading.Lock()
    box: dict = {"at": None, "value": False}

    def enabled() -> bool:
        with lock:
            now = clock()
            if box["at"] is None or now - box["at"] >= ttl_s:
                try:
                    box["value"] = bool((queries.control() or {}).get("agent_enabled", 1))
                except Exception:  # noqa: BLE001 —— 读不到就不给 Agent 数据
                    log.warning("读取 Agent 开关失败，先当作关闭", exc_info=True)
                    box["value"] = False
                box["at"] = now
            return box["value"]

    return ms.AgentPolicy(enabled=enabled)
