"""⑤ 留截图（方案"留截图"一节）：JPEG 编码、全黑判断、"读不出字的新画面"有多大，和留图策略 ShotPolicy。

- 强标签（网址 / 本地文件）：每个只在第一次看时留 1 张（'first'）；
- 弱标签（软件 + 标题）：每一次（一条时间线条目：切进来到切走，5 分钟内回来算同一次；一直不走时每 weak_visit_ms
  算一次）留新字最多的那张（'visit'），当场存、之后有新字更多的就替换；一次里一行新字都没有就不留；
- 本地文件：只留第一次那 1 张（翻页、滚动都不再留）；
- 读不出字的新画面（照片、视频）：同一个出处每 fallback_shot_ms 最多 1 张（'fallback'），强弱标签都算。
  "新画面" = 和上次看这个出处时比变了的 32 像素小块（tile_signature），里面"像图"的（picture_tiles：没有一大片底色）、
  又没有字盖住的部分；文字页的行距、页边是底色，回到看过的页面、往回翻看过的字都不算；
- 截到全黑（有版权保护的视频）：不留。
编码和存在调用方（处理线程）做；ShotPolicy 只看元数据决定，状态只在内存里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..rec.motion import TILE, Fp
from ..store.fts.docs import norm
from .config import V3Config
from .types import KIND_FILE, SHOT_FALLBACK, SHOT_FIRST, SHOT_VISIT, Box, EncodedShot, SourceLabel, TextLine

try:  # opencv 编码 / 缩放比 Pillow 快；两个都在 requirements.txt 里
    import cv2 as _cv2
except ImportError:  # pragma: no cover
    _cv2 = None

# 判"全黑"：均值不超过 BLACK_MEAN、标准差不超过 BLACK_STD（0–255），并且比 BLACK_LEVEL 亮的像素不到 BLACK_INK。
# 只看标准差不够：黑底上一行细小的字（深色主题）标准差也很小，但那一行字要读、要存
BLACK_MEAN = 10.0
BLACK_STD = 6.0
BLACK_LEVEL = 32
BLACK_INK = 0.0005
# 判"像图"的小块：块里最多的那种颜色占不到这个比例（字、留白都有一大片底色）；隔 PICTURE_STEP 个像素取样
PICTURE_BG_SHARE = 0.5
PICTURE_STEP = 2

DECIDE_NONE = "none"


def _bgr3(bgr) -> np.ndarray:
    a = np.asarray(bgr)
    if a.ndim == 2:
        a = np.repeat(a[:, :, None], 3, axis=2)
    elif a.ndim == 3 and a.shape[2] == 4:
        a = a[:, :, :3]
    return np.ascontiguousarray(a)


def encode_jpeg(bgr, *, quality: int, max_width: int) -> EncodedShot:
    """BGR(A) 图编码成 JPEG；宽超过 max_width 时等比缩小。scale = JPEG 宽 / 原图宽；boxes 为空（调用方用 shot_boxes 填）。"""
    a = _bgr3(bgr)
    h, w = a.shape[:2]
    if w <= 0 or h <= 0:
        raise ValueError("空图不能编码")
    scale = 1.0
    if max_width and w > max_width:
        nw = int(max_width)
        nh = max(1, round(h * nw / w))
        scale = nw / w
        if _cv2 is not None:
            a = _cv2.resize(a, (nw, nh), interpolation=_cv2.INTER_AREA)
        else:  # pragma: no cover
            from PIL import Image

            a = np.asarray(Image.fromarray(a[:, :, ::-1]).resize((nw, nh), Image.LANCZOS))[:, :, ::-1]
    q = max(1, min(100, int(quality)))
    if _cv2 is not None:
        ok, buf = _cv2.imencode(".jpg", a, [_cv2.IMWRITE_JPEG_QUALITY, q])
        if not ok:
            raise ValueError("JPEG 编码失败")
        data = buf.tobytes()
    else:  # pragma: no cover
        import io

        from PIL import Image

        out = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(a[:, :, ::-1])).save(out, "JPEG", quality=q)
        data = out.getvalue()
    return EncodedShot(jpeg=data, width=int(a.shape[1]), height=int(a.shape[0]), scale=scale)


def shot_boxes(lines: Sequence[TextLine], scale: float) -> list[tuple[str, Box]]:
    """每行字的 (norm, JPEG 坐标框)。同一个 norm 只留第一个（库里一张图上每行一个框）；norm 为空的不要。"""
    out: list[tuple[str, Box]] = []
    seen: set[str] = set()
    for ln in lines:
        k = norm(ln.text)
        if not k or k in seen:
            continue
        seen.add(k)
        x, y, w, h = ln.box
        out.append((k, (round(x * scale), round(y * scale), max(1, round(w * scale)), max(1, round(h * scale)))))
    return out


def is_black(bgr) -> bool:
    """几乎全黑（有版权保护的视频截出来是黑的）：各通道的均值和标准差都很小，而且几乎没有亮一点的像素
    （黑底上的一行细字不算全黑）。"""
    a = np.asarray(bgr)
    if a.size == 0:
        return False
    if a.ndim == 3 and a.shape[2] == 4:
        a = a[:, :, :3]
    a = np.ascontiguousarray(a)
    if _cv2 is not None and a.dtype == np.uint8:
        mean, std = _cv2.meanStdDev(a)
        mean, std = float(mean.max()), float(std.max())
    else:  # pragma: no cover
        f = a.astype(np.float32)
        mean, std = float(f.mean()), float(f.std())
    if mean > BLACK_MEAN or std > BLACK_STD:
        return False
    lum = a.max(axis=2) if a.ndim == 3 else a
    return bool(np.count_nonzero(lum > BLACK_LEVEL) <= BLACK_INK * lum.size)


_SIG_WEIGHTS = np.random.default_rng(20261028).integers(1, 2**63, size=(TILE, TILE // 8), dtype=np.uint64) | np.uint64(1)


def tile_signature(bgr) -> np.ndarray:
    """每个 32 像素小块一个 64 位指纹（和 rec.motion 一样看绿色通道的高 4 位），形状 (补齐高/32, 补齐宽/32)。
    同一个出处两次拍之间比一比，就知道"上次看它之后哪些地方变了"。"""
    words = Fp.from_image(_bgr3(bgr)).words
    th, tw = words.shape[0] // TILE, words.shape[1] // (TILE // 8)
    return (words.reshape(th, TILE, tw, TILE // 8) * _SIG_WEIGHTS[None, :, None, :]).sum(axis=(1, 3), dtype=np.uint64)


def picture_tiles(bgr) -> np.ndarray:
    """哪些 32 像素小块"像图"（照片、视频画面），形状 (补齐高/32, 补齐宽/32)：块里最多的那种颜色（16 级灰度）
    占不到 PICTURE_BG_SHARE。字和留白都有一大片底色，不算图——所以文字页的行距、页边不会被当成"读不出字的画面"。"""
    a = _bgr3(bgr)
    q = a[::PICTURE_STEP, ::PICTURE_STEP, 1] >> 4
    t = TILE // PICTURE_STEP
    h, w = q.shape
    th, tw = -(-h // t), -(-w // t)
    pad = np.full((th * t, tw * t), 16, dtype=np.uint8)  # 补出来的格子取第 17 种"颜色"，不算进任何一种
    pad[:h, :w] = q
    blocks = pad.reshape(th, t, tw, t)
    valid = np.count_nonzero(blocks < 16, axis=(1, 3))
    mode = np.zeros((th, tw), dtype=np.int64)
    for level in range(16):
        np.maximum(mode, np.count_nonzero(blocks == level, axis=(1, 3)), out=mode)
    return (valid > 0) & (mode < PICTURE_BG_SHARE * np.maximum(valid, 1))


def _tiles_to_pixels(tiles: np.ndarray, fh: int, fw: int) -> np.ndarray:
    return np.repeat(np.repeat(tiles, TILE, axis=0), TILE, axis=1)[:fh, :fw]


def uncovered_change_ratio(changed: Box | None, frame_shape: tuple[int, int], lines: Sequence[TextLine], *,
                           tiles: np.ndarray | None = None, bgr=None) -> float:
    """上次拍之后变了、又没有字盖住的面积 / 整帧面积（0–1）。frame_shape 是 (高, 宽)（numpy 的 shape 前两项）。
    变了的地方：给了 tiles（变了的 32 像素小块，形状同 tile_signature）按小块算，否则按 changed 框；
    两个都没有（None）时按整帧都变了算。给了 bgr 时只算"像图"的小块（picture_tiles）：读不出字的新画面。"""
    fh, fw = int(frame_shape[0]), int(frame_shape[1])
    if fh <= 0 or fw <= 0:
        return 0.0
    if tiles is not None:
        mask = _tiles_to_pixels(np.asarray(tiles, dtype=bool), fh, fw).copy()
    else:
        mask = np.zeros((fh, fw), dtype=bool)
        if changed is None:
            mask[:] = True
        else:
            x, y, w, h = changed
            mask[max(0, y):max(0, y + h), max(0, x):max(0, x + w)] = True
    if bgr is not None:
        mask &= _tiles_to_pixels(picture_tiles(bgr), fh, fw)
    for ln in lines:
        lx, ly, lw, lh = ln.box
        if lw > 0 and lh > 0:
            mask[max(0, ly):max(0, ly + lh), max(0, lx):max(0, lx + lw)] = False
    return float(np.count_nonzero(mask)) / float(fh * fw)


@dataclass
class _Slot:
    """弱标签一条（visit_id）在这 weak_visit_ms 里已经存下的那张。"""
    shot_id: int
    new_lines: int
    since: int      # 这一张所在的 weak_visit_ms 从何时开始（这一段第一次存图的时间）


MAX_SLOTS = 512


class ShotPolicy:
    """⑤ 留截图。只看元数据决定；编码由调用方做。状态只在内存（重启就忘，没关系）。

    弱标签"这一次新字最多的那张"当场就存（搜索结果马上有图、进程被杀也不丢）：这一条（5 分钟内回来算同一条，
    visit_id 不变）在 weak_visit_ms 里第一次有新字 → 存一张；之后哪一拍新字比它多 → 换成那一拍（replaces 给出
    要替换掉的旧图）；过了 weak_visit_ms → 下一次有新字时另存一张。所以一条一张、一直不走每 10 分钟一张，
    前后台来回切、回来又看的都不会再多存几乎一样的图。"""

    def __init__(self, cfg: V3Config):
        self.cfg = cfg
        self._slots: dict[int, _Slot] = {}        # visit_id -> 这一段已存的那张
        self._fallback_at: dict[int, int] = {}    # source_id -> 上次留 fallback 图的时间

    def _live(self, visit_id: int, t_ms: int) -> _Slot | None:
        s = self._slots.get(visit_id)
        return s if s is not None and t_ms - s.since < self.cfg.weak_visit_ms else None

    def decide(self, *, label: SourceLabel, source_id: int, visit_id: int, source_has_shot: bool, t_ms: int,
               new_lines: int, has_text: bool, uncovered_change: float, black: bool) -> str:
        """返回 'first' / 'fallback' / 'visit' / 'none'（规则见模块和类说明）。'visit' 时调用方先问 replaces()。
        uncovered_change：上次看这个出处之后新出来的、字盖不住的"像图"的面积（uncovered_change_ratio 带 bgr 算的）；
        读得出字的页面里新出来的照片 / 视频照样按 fallback 算。has_text 只是给调用方记录用。"""
        if black:
            return DECIDE_NONE
        if label.kind == KIND_FILE:  # 本地文件：只记路径和时间，每个文件只留第一次那 1 张
            return DECIDE_NONE if source_has_shot else SHOT_FIRST
        if label.strong and not source_has_shot:
            return SHOT_FIRST
        if new_lines <= 0 and uncovered_change >= self.cfg.fallback_min_area:
            last = self._fallback_at.get(source_id)
            if last is None or t_ms - last >= self.cfg.fallback_shot_ms:
                return SHOT_FALLBACK
        if not label.strong and new_lines > 0:
            s = self._live(visit_id, t_ms)
            if s is None or new_lines > s.new_lines:  # 一样多留先来的
                return SHOT_VISIT
        return DECIDE_NONE

    def replaces(self, visit_id: int, t_ms: int) -> int | None:
        """decide 给了 'visit'：这一张要替换掉的旧图（同一条、同一个 weak_visit_ms 里的）；新开一段时 None。"""
        s = self._live(visit_id, t_ms)
        return s.shot_id if s is not None else None

    def saved_visit(self, visit_id: int, shot_id: int, new_lines: int, t_ms: int) -> None:
        """调用方真的存下了 'visit' 图（存失败时不调，下一拍还能再试）。"""
        s = self._live(visit_id, t_ms)
        self._slots[visit_id] = _Slot(shot_id, new_lines, s.since if s is not None else t_ms)
        if len(self._slots) > MAX_SLOTS:  # 过了 weak_visit_ms 的不会再被替换，不用记了
            self._slots = {k: v for k, v in self._slots.items() if t_ms - v.since < self.cfg.weak_visit_ms}

    def clear(self) -> None:
        """忘掉已存的图（隐私删除之后：那些图可能已经删了），之后有新字另存。"""
        self._slots.clear()

    def mark_fallback(self, source_id: int, t_ms: int) -> None:
        """调用方真的存下了一张 fallback 图：记下时间（存失败时不记，下一拍还能再试）。"""
        self._fallback_at[source_id] = t_ms
        if len(self._fallback_at) > 4096:  # 很久以前的出处不用再限速
            cut = t_ms - self.cfg.fallback_shot_ms
            self._fallback_at = {k: v for k, v in self._fallback_at.items() if v > cut}
