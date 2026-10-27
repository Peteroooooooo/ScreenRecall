"""一帧画面和"画面像素 ↔ 屏幕坐标"的换算。

WGC 送来的帧就是窗口可见边框（DWMWA_EXTENDED_FRAME_BOUNDS，含标题栏）里的内容，
单位是物理像素（录制进程是按显示器 DPI 感知 v2）。UIA 给的矩形也是屏幕物理像素，
用 FrameMap.rect_to_frame() 换成帧里的像素位置。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .motion import Fp

try:  # opencv 去掉 alpha 通道比 numpy 快很多；没装时退回 numpy
    import cv2 as _cv2
except ImportError:  # pragma: no cover
    _cv2 = None


@dataclass(frozen=True)
class FrameMap:
    """帧像素 → 屏幕物理像素：screen = (left + fx * scale_x, top + fy * scale_y)。"""

    left: int
    top: int
    width: int    # 帧宽（像素）
    height: int   # 帧高（像素）
    scale_x: float = 1.0
    scale_y: float = 1.0

    @classmethod
    def from_bounds(cls, bounds: tuple[int, int, int, int] | None, width: int, height: int) -> FrameMap:
        if not bounds:
            return cls(0, 0, width, height)
        left, top, right, bottom = bounds
        bw, bh = right - left, bottom - top
        sx = bw / width if width and bw > 0 else 1.0
        sy = bh / height if height and bh > 0 else 1.0
        # 边框和帧差一两个像素时按 1:1 算（窗口刚改完大小、圆角裁切）
        if abs(sx - 1.0) * width <= 2:
            sx = 1.0
        if abs(sy - 1.0) * height <= 2:
            sy = 1.0
        return cls(int(left), int(top), int(width), int(height), sx, sy)

    def to_screen(self, fx: float, fy: float) -> tuple[int, int]:
        return round(self.left + fx * self.scale_x), round(self.top + fy * self.scale_y)

    def to_frame(self, sx: float, sy: float) -> tuple[int, int]:
        return round((sx - self.left) / self.scale_x), round((sy - self.top) / self.scale_y)

    def rect_to_frame(self, left: float, top: float, right: float, bottom: float) -> tuple[int, int, int, int] | None:
        """屏幕矩形 → 帧内像素矩形（裁到帧内）；完全在帧外时返回 None。"""
        l, t = self.to_frame(left, top)
        r, b = self.to_frame(right, bottom)
        l, r = max(0, min(l, r)), min(self.width, max(l, r))
        t, b = max(0, min(t, b)), min(self.height, max(t, b))
        if r <= l or b <= t:
            return None
        return l, t, r, b

    def rect_to_screen(self, left: int, top: int, right: int, bottom: int) -> tuple[int, int, int, int]:
        l, t = self.to_screen(left, top)
        r, b = self.to_screen(right, bottom)
        return l, t, r, b


class Frame:
    """一帧：灰度指纹（Fp，算一次缓存）+ 原图。BGR 只在需要时（静止、存证据）才从 BGRA 转出来。"""

    __slots__ = ("hwnd", "t_ms", "seq", "fmap", "source", "fp", "_bgra", "_bgr")

    def __init__(self, hwnd: int, t_ms: int, fp: Fp, fmap: FrameMap, *, seq: int = 0, source: str = "wgc",
                 bgra: np.ndarray | None = None, bgr: np.ndarray | None = None):
        self.hwnd = hwnd
        self.t_ms = t_ms
        self.seq = seq
        self.fmap = fmap
        self.source = source
        self.fp = fp
        self._bgra = bgra
        self._bgr = bgr

    @classmethod
    def from_bgra(cls, hwnd: int, t_ms: int, bgra: np.ndarray, fmap: FrameMap | None = None, **kw) -> Frame:
        h, w = bgra.shape[:2]
        return cls(hwnd, t_ms, Fp.from_image(bgra), fmap or FrameMap(0, 0, w, h), bgra=bgra, **kw)

    @classmethod
    def from_bgr(cls, hwnd: int, t_ms: int, bgr: np.ndarray, fmap: FrameMap | None = None, **kw) -> Frame:
        h, w = bgr.shape[:2]
        return cls(hwnd, t_ms, Fp.from_image(bgr), fmap or FrameMap(0, 0, w, h), bgr=bgr, **kw)

    @property
    def gray(self) -> np.ndarray:
        """绿色通道右移 4 位的灰度图 (高, 宽)。"""
        return self.fp.g

    @property
    def shape(self) -> tuple[int, int]:
        return self.fp.shape

    @property
    def bgr(self) -> np.ndarray:
        """连续的 BGR 原图（第一次访问时从 BGRA 转出并缓存，之后放掉 BGRA）。"""
        if self._bgr is None:
            src = self._bgra
            if src is None:
                raise ValueError("这一帧没有原图")
            if _cv2 is not None:
                self._bgr = _cv2.cvtColor(src, _cv2.COLOR_BGRA2BGR)
            else:  # pragma: no cover
                self._bgr = np.ascontiguousarray(src[:, :, :3])
            self._bgra = None
        return self._bgr

    def drop_pixels(self) -> None:
        """只留指纹，放掉原图（不会再用到原图的帧，尽早还内存）。"""
        self._bgra = None
        self._bgr = None
