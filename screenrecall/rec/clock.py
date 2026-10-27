"""录制核心统一用的时钟：数值是 UTC 毫秒（可以直接存档），但走的是单调时钟（不会因为对时而倒退）。

进程启动时记一次 UTC 和单调时钟的差，之后都用单调时钟加这个差。各部分（截图回调、输入监测、
计时）用同一个时钟，事件里的 t_ms 互相可比。测试里注入自己的时钟。
"""

from __future__ import annotations

import time
from typing import Callable

Clock = Callable[[], int]

_OFFSET_MS = time.time_ns() // 1_000_000 - time.monotonic_ns() // 1_000_000


def now_ms() -> int:
    return time.monotonic_ns() // 1_000_000 + _OFFSET_MS
