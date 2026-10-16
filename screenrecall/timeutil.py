"""时间工具：存档统一用 UTC 毫秒，间隔与超时用单调时钟（方案 3.4）。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta


def utc_ms() -> int:
    """当前 UTC 时间（Unix 毫秒），用于存档。"""
    return time.time_ns() // 1_000_000


def local_tz() -> tuple[int, str]:
    """当前本地时区：(相对 UTC 的分钟偏移, 名称)。"""
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    return minutes, now.tzname() or ""


def tz_label() -> str:
    minutes, name = local_tz()
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{mins:02d}"


def local_dates_to_utc_range(start: date, end: date) -> tuple[int, int]:
    """本地日期闭区间 [start, end] 转为 UTC 毫秒半开区间 [a, b)。

    朴素 datetime 的 timestamp() 按本机时区规则换算，夏令时切换日也正确。
    """
    if end < start:
        start, end = end, start
    a = datetime(start.year, start.month, start.day)
    b = datetime(end.year, end.month, end.day) + timedelta(days=1)
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def fmt_local(ms: int | None) -> str:
    """UTC 毫秒转本地时间字符串。"""
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
