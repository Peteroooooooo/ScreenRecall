"""录制核心统一用的时钟：数值是 UTC 毫秒（可以直接存档），而且只往前走（不会因为对时而倒退）。

做法：单调时钟加一个"和 UTC 的差"。录制进程一开就是好几天，单调时钟和系统时间之间会慢慢漂
（几秒 / 天），睡眠唤醒后有的单调时钟（例如 Python 3.13 起 Windows 上的 QueryPerformanceCounter）
可能没把睡眠的时间算进去。所以每次取时间都和系统时间比一下：
- 比系统时间慢了 1 秒以上（睡眠、往前漂）→ 直接追上；
- 快了 1 秒以上（往后漂、有人把系统时间往回调）→ 不倒退，只是走慢一点（每过 1 秒少走 1 毫秒）慢慢追平，
  这样计时（静止判断这类"过了多久"）不会停住。
各部分（截图回调、输入监测、计时）用同一个时钟，事件里的 t_ms 互相可比。测试里注入自己的时钟。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

Clock = Callable[[], int]

JUMP_MS = 1000   # 和系统时间差多少才校正
SLEW_DIV = 1000  # 往回校正时，每过 SLEW_DIV 毫秒最多少走 1 毫秒


class _UtcClock:
    def __init__(self, mono: Callable[[], int] | None = None, wall: Callable[[], int] | None = None):
        self._mono = mono or (lambda: time.monotonic_ns() // 1_000_000)
        self._wall = wall or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.Lock()
        m = self._mono()
        self._offset = self._wall() - m
        self._slew_from = m  # 从这个单调时刻起累计"可以少走的毫秒数"
        self._last = m + self._offset

    def __call__(self) -> int:
        m, w = self._mono(), self._wall()
        with self._lock:
            err = w - (m + self._offset)
            if err > JUMP_MS:
                self._offset += err
                self._slew_from = m
            elif err < -JUMP_MS:
                n = max(0, m - self._slew_from) // SLEW_DIV
                if n:
                    self._offset -= min(-err, n)
                    self._slew_from += n * SLEW_DIV
            else:
                self._slew_from = m
            t = max(self._last, m + self._offset)
            self._last = t
            return t


now_ms: Clock = _UtcClock()
