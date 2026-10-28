"""① 拍：一个窗口"什么时候拍"（纯逻辑，只看像素，不碰窗口和数据库）。

- 画面停下 cfg.still_ms 就拍（'still'）；画面一直在动满 cfg.forced_ms，强制拍一次（'forced'，之后重新计）；
- 一直在变的小块（视频角落、时钟、光标闪烁：rec.volatile）不算在动；但它们占了画面一大片时（整屏视频、
  一直在刷的日志）照样算在动，不然这类画面只拍得到一次；
- 这个页面刚打过字（调用方给 typing=True）先不拍，待拍的变化留着，等不打字了再按上面的规则出；
- 切进来（reset）：下一次 tick 就开始计"停下"，画面一直不变的窗口也拍一次；这一次的 changed 记成 None（整幅都算新的）。
changed 框：上次拍之后变过的 32 像素小块（去掉一直在变的）的外接框，整帧坐标 (x, y, w, h)。
"""

from __future__ import annotations

import numpy as np

from ..rec.motion import tile_changes
from ..rec.volatile import VolatileTiles
from .config import V3Config
from .types import REASON_FORCED, REASON_STILL, Box

TILE = 32
# 一直在变的小块合起来不超过画面的这个比例才忽略（"小块"）；更大就是整屏在动（视频、日志），照常算变化
VOLATILE_MAX_SHARE = 0.25


def _tiles_box(tiles: np.ndarray, height: int, width: int) -> Box | None:
    """小块布尔图 → 像素外接框（裁到帧内）；一块都没有时 None。"""
    rows = np.flatnonzero(tiles.any(axis=1))
    cols = np.flatnonzero(tiles.any(axis=0))
    if not len(rows):
        return None
    x0, y0 = int(cols[0]) * TILE, int(rows[0]) * TILE
    x1, y1 = min(width, (int(cols[-1]) + 1) * TILE), min(height, (int(rows[-1]) + 1) * TILE)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _union(a: Box | None, b: Box | None) -> Box | None:
    if a is None:
        return b
    if b is None:
        return a
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return x0, y0, x1 - x0, y1 - y0


class SnapTrigger:
    """一个窗口"什么时候拍"。只看像素（rec.motion.Fp 的 tile_changes，减去 rec.volatile 的一直在变的小块）。"""

    def __init__(self, cfg: V3Config):
        self.cfg = cfg
        self.latest = None                    # 最新一帧（rec.frames.Frame）；拍的时候用它
        self._vol = VolatileTiles()
        self._pending = False                 # 有没拍的变化（或刚切进来）
        self._whole = True                    # 这次的变化按整幅算（第一次 / 切进来 / 尺寸变了）
        self._box: Box | None = None          # 累积的变化外接框
        self._change_start: int | None = None # 这一段连续变化从何时开始
        self._last_change: int | None = None  # 最后一次变化（或切进来）的时间
        self.frames = 0
        self.changes = 0

    def reset(self, t_ms: int) -> None:
        """切进来：从现在开始计"停下"；changed 记成 None（整幅都算新的）。"""
        self._pending = True
        self._whole = True
        self._box = None
        self._change_start = None
        self._last_change = t_ms

    def feed(self, frame, t_ms: int) -> None:
        """新的一帧。有变化（非一直在变的小块）→ 记变化起点 / 最后变化时间，累积 changed 框。"""
        prev, self.latest = self.latest, frame
        self.frames += 1
        if prev is None or prev.shape != frame.shape:
            self._vol.reset()
            self._whole = True
            self._changed(t_ms)
            return
        tiles = tile_changes(prev.fp, frame.fp)
        if not tiles.any():
            return
        eff = tiles
        vol = self._vol.volatile(t_ms)
        if vol is not None and vol.shape == tiles.shape and vol.mean() <= VOLATILE_MAX_SHARE:
            eff = tiles & ~vol
        self._vol.look(tiles, t_ms)
        if not eff.any():
            return
        h, w = frame.shape
        self._box = _union(self._box, _tiles_box(eff, h, w))
        self._changed(t_ms)

    def _changed(self, t_ms: int) -> None:
        self.changes += 1
        self._pending = True
        if self._change_start is None:
            self._change_start = t_ms
        self._last_change = t_ms if self._last_change is None else max(self._last_change, t_ms)

    def tick(self, t_ms: int, *, typing: bool) -> str | None:
        """该拍了返回原因（'still' / 'forced'），否则 None。typing=True 时先不拍（待拍的留着）。"""
        if self.latest is None or not self._pending or typing:
            return None
        if self._last_change is not None and t_ms - self._last_change >= self.cfg.still_ms:
            return REASON_STILL
        if self._change_start is not None and t_ms - self._change_start >= self.cfg.forced_ms:
            return REASON_FORCED
        return None

    def snapped(self, t_ms: int) -> Box | None:
        """调用方拍了：返回这次的 changed 框（整幅算新的时 None）并清零，下一段变化重新计。"""
        box = None if self._whole else self._box
        self._pending = False
        self._whole = False
        self._box = None
        self._change_start = None
        return box
