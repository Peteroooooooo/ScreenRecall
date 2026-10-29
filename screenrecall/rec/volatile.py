"""一直在变的小块（视频、时钟、光标闪烁）：最近几次比对里变过 ≥ 3 次的 32 像素小块。

用法：位置没动的两帧比完（'same' / 'inplace'）后调用 look() 记一次"看"；画面平移或接不上时
reset()，因为小块的位置已经对不上了。打字期间不记（不然正在打的那一行也会被当成"一直在变"）。
只在一直变的小块里变了的帧直接丢掉，它们也不会让"静止计时"重新开始。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

from .motion import MotionMask


@dataclass(frozen=True)
class VolatileConfig:
    min_changes: int = 3      # 最近几次里至少变过几次才算"一直在变"
    max_looks: int = 8        # 只看最近几次
    window_ms: int = 8000     # 只看这么久以内的几次（视频暂停后，过一会儿就不再遮）


class VolatileTiles:
    """一个窗口的小块变化历史。小块网格形状由第一次 look() 决定，形状变了就重来。"""

    def __init__(self, config: VolatileConfig | None = None):
        self.config = config or VolatileConfig()
        self._looks: deque[tuple[int, np.ndarray]] = deque(maxlen=self.config.max_looks)
        self._cache: tuple[int, np.ndarray | None] | None = None
        self._mask_cache: tuple[int, tuple[int, int], MotionMask | None] | None = None

    def reset(self) -> None:
        self._looks.clear()
        self._cache = None
        self._mask_cache = None

    def look(self, changed: np.ndarray, t_ms: int) -> None:
        """记一次比较结果：changed 是 tile_changes() 的结果。"""
        if self._looks and self._looks[-1][1].shape != changed.shape:
            self._looks.clear()
        self._looks.append((t_ms, changed))
        self._cache = None
        self._mask_cache = None

    def volatile(self, t_ms: int) -> np.ndarray | None:
        """当前算"一直在变"的小块；一个都没有时返回 None。"""
        if self._cache is not None and self._cache[0] == t_ms:
            return self._cache[1]
        recent = [c for t, c in self._looks if t_ms - t <= self.config.window_ms]
        out = None
        if len(recent) >= self.config.min_changes:
            count = np.sum(recent, axis=0)
            vol = count >= self.config.min_changes
            out = vol if vol.any() else None
        self._cache = (t_ms, out)
        return out

    def only_volatile(self, changed: np.ndarray, t_ms: int) -> bool:
        """这次的变化是不是全落在一直变的小块里（没有变化时也返回 False）。"""
        if not changed.any():
            return False
        vol = self.volatile(t_ms)
        if vol is None or vol.shape != changed.shape:
            return False
        return not bool((changed & ~vol).any())

    def mask(self, t_ms: int, height: int, width: int) -> MotionMask | None:
        """给 motion.analyze 用的遮罩；没有一直变的小块时返回 None（按原算法比）。"""
        key = (height, width)
        if self._mask_cache is not None and self._mask_cache[0] == t_ms and self._mask_cache[1] == key:
            return self._mask_cache[2]
        vol = self.volatile(t_ms)
        out = None
        if vol is not None and vol.shape == (-(-height // 32), -(-width // 32)):
            out = MotionMask.from_tiles(vol, height, width)
        self._mask_cache = (t_ms, key, out)
        return out
