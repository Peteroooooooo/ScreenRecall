"""只靠像素判断相邻两帧之间"画面怎么动了"（v2 录制快路径）。

算法和阈值与实验版 experiments/scroll/motion.py 完全一致，只是全部改成 numpy 批量计算：
- 绿色通道右移 4 位后，竖着切 12 条（估上下）、横着切 8 条（估左右），每条隔一列（行）取样；
- 每一行算一个 64 位指纹。行字节按 8 字节一组看成 uint64，先做 v ^= v >> 29（一一映射，
  把高字节的差别散到低位），再乘一组固定的奇数权重求和。不做这一步时，只在某个字高字节不同的
  两行很容易撞成同一个指纹；
- 旧图每 K=4 行取一行，只用在取样里只出现一次的行投票；票数 ≥ 8 且是第二名的 3 倍以上才算数；
- 再按这个偏移把两张图叠起来复核：重叠部分里"有内容的行"（在旧图里只出现一次）至少六成要完全一样；
- 至少 2 条给出同一个数、并且占在动的条的多数，才认这个数，否则宁可说"接不上"；
- （v2 新增）交叉复核：给不出数的条带按这个数叠起来，若有 2 条以上明显对不上、对不上的行分散在好几处，
  这个数是重复版式凑出来的（每隔几段同样的首行），也算接不上。

上下已经算出平移时不再算左右。每帧的指纹只算一次，缓存在 Fp 里。
灰度图存在宽高都补到 32 倍数的缓冲里（补的部分为 0），整行比较、32 像素小块比较都按 uint64 批量做。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

K = 4          # 旧图每 K 行（列）取一个做比对
STRIPS = 12    # 竖着切几条（估上下）
BANDS = 8      # 横着切几条（估左右）
MIN_UNIQUE = 6     # 取样里只出现一次的行少于这个数：没法比
MIN_VOTES = 8      # 第一名至少几票
RUNNER_UP_RATIO = 3  # 第一名至少是第二名的几倍
OVERLAP_MIN = 0.6    # 复核：重叠部分有内容的行至少几成完全一样
TILE = 32            # 变化小块的边长（像素）

_M64 = (1 << 64) - 1
_SHIFT = np.uint64(29)


def green(bgr_or_bgra: np.ndarray) -> np.ndarray:
    """取绿色通道并右移 4 位（0..15），得到连续的 uint8 灰度图。"""
    return np.right_shift(bgr_or_bgra[:, :, 1], 4)


def _pad_to(n: int, m: int = TILE) -> int:
    return n + (-n) % m


@lru_cache(maxsize=64)
def _weights(n_words: int) -> np.ndarray:
    """固定的奇数权重（splitmix64 序列），同一宽度每次都一样。"""
    out, x = [], 0x5EC0_4EC0_1120_2026
    for _ in range(n_words):
        x = (x + 0x9E3779B97F4A7C15) & _M64
        z = x
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
        z ^= z >> 31
        out.append(z | 1)
    return np.array(out, dtype=np.uint64)


def row_fingerprints(rows: np.ndarray) -> np.ndarray:
    """最后一维是"一行的字节"，返回每行一个 uint64 指纹（形状去掉最后一维）。

    行长不是 8 的倍数时补 0；比较只在同一宽度的行之间进行，补 0 不影响"相同/不同"。
    """
    n = rows.shape[-1]
    if n % 8 or not rows.flags.c_contiguous:
        buf = np.zeros(rows.shape[:-1] + (_pad_to(n, 8),), dtype=np.uint8)
        buf[..., :n] = rows
        rows = buf
    words = rows.view(np.uint64)
    mixed = words >> _SHIFT
    mixed ^= words
    return mixed @ _weights(words.shape[-1])  # 整数矩阵乘法，按 2^64 取模回绕


def _edges(n: int, parts: int) -> np.ndarray:
    return np.linspace(0, n, parts + 1).astype(int)


@lru_cache(maxsize=32)
def _layout(n: int, parts: int) -> tuple[np.ndarray, np.ndarray]:
    """分段边界和每段隔一个取样后的长度。"""
    e = _edges(n, parts)
    lens = np.array([len(range(e[i], e[i + 1], 2)) for i in range(parts)])
    return e, lens


def strip_edges(w: int, n: int = STRIPS) -> np.ndarray:
    """竖带在列方向的边界（长度 n+1）。"""
    return _layout(w, n)[0]


def band_edges(h: int, n: int = BANDS) -> np.ndarray:
    """横带在行方向的边界（长度 n+1）。"""
    return _layout(h, n)[0]


def vstrips(g: np.ndarray, n: int = STRIPS) -> np.ndarray:
    """竖带指纹：形状 (n, 高)，第 s 行是第 s 条竖带每一行的指纹。"""
    e, lens = _layout(g.shape[1], n)
    buf = np.zeros((g.shape[0], n, _pad_to(max(int(lens.max()), 1), 8)), dtype=np.uint8)
    for s in range(n):
        if lens[s]:
            buf[:, s, : lens[s]] = g[:, e[s]: e[s + 1]: 2]
    return np.ascontiguousarray(row_fingerprints(buf).T)


def hbands(g: np.ndarray, n: int = BANDS) -> np.ndarray:
    """横带指纹：形状 (n, 宽)，第 b 行是第 b 条横带每一列（看成一行）的指纹。"""
    e, lens = _layout(g.shape[0], n)
    buf = np.zeros((n, g.shape[1], _pad_to(max(int(lens.max()), 1), 8)), dtype=np.uint8)
    for b in range(n):
        if lens[b]:
            buf[b, :, : lens[b]] = g[e[b]: e[b + 1]: 2, :].T
    return row_fingerprints(buf)


@dataclass
class _Prepared:
    """一条带作为"旧图"时要用到的预处理，缓存起来，同一张图当旧图时不重算。"""

    uniq_vals: np.ndarray  # 取样里只出现一次的指纹（已排序）
    uniq_pos: np.ndarray   # 对应的行号
    once: np.ndarray       # 每一行在整条里是否只出现一次


def _prepare(prev: np.ndarray, prev_mask: np.ndarray | None = None) -> _Prepared:
    samp_pos = np.arange(0, len(prev), K)
    samp = prev[::K]
    if prev_mask is not None:
        keep = ~prev_mask[::K]
        samp_pos, samp = samp_pos[keep], samp[keep]
    vals, first, counts = np.unique(samp, return_index=True, return_counts=True)
    single = counts == 1
    _, inv, cnt_all = np.unique(prev, return_inverse=True, return_counts=True)
    once = cnt_all[inv.reshape(-1)] == 1
    return _Prepared(vals[single], samp_pos[first[single]], once)


def shift_1d(prev: np.ndarray, cur: np.ndarray, *, prev_mask: np.ndarray | None = None,
             cur_mask: np.ndarray | None = None, prepared: _Prepared | None = None) -> int | None:
    """一条带上的偏移。正数 = 内容往上（左）移。给不出可靠的数时返回 None。

    遮罩（True = 忽略这一行）：旧图里被遮的行不参与投票和复核，新图里被遮的行不投票、不参与复核。
    传了 prepared 时它必须来自同一个 prev 且没有遮罩。
    """
    p = prepared if prepared is not None else _prepare(prev, prev_mask)
    if len(p.uniq_vals) < MIN_UNIQUE or len(cur) == 0:
        return None
    loc = np.searchsorted(p.uniq_vals, cur)
    loc[loc >= len(p.uniq_vals)] = 0
    hit = p.uniq_vals[loc] == cur
    if cur_mask is not None:
        hit &= ~cur_mask
    j = np.flatnonzero(hit)
    if j.size == 0:
        return None
    offs = p.uniq_pos[loc[j]] - j
    vals, counts = np.unique(offs, return_counts=True)
    top = int(counts.argmax())
    v = int(counts[top])
    second = int(np.partition(counts, -2)[-2]) if len(counts) > 1 else 0
    if not (v >= MIN_VOTES and v >= RUNNER_UP_RATIO * second):
        return None
    d = int(vals[top])
    # 复核：按偏移 d 叠起来，旧图里只出现一次的行有多少完全一样
    hits, tot = _overlap(prev, cur, d, p.once, prev_mask, cur_mask)
    if not tot:
        return None
    return d if hits / tot >= OVERLAP_MIN else None


def _overlap(prev: np.ndarray, cur: np.ndarray, d: int, once_all: np.ndarray,
             prev_mask: np.ndarray | None, cur_mask: np.ndarray | None) -> tuple[int, int]:
    """按偏移 d 叠起来：(完全一样的行数, 参与比较的行数)。参与比较的是旧图里只出现一次、没被遮的行。"""
    j0, j1 = max(0, -d), min(len(cur), len(prev) - d)
    if j1 <= j0:
        return 0, 0
    once = once_all[j0 + d: j1 + d].copy()
    if prev_mask is not None:
        once &= ~prev_mask[j0 + d: j1 + d]
    if cur_mask is not None:
        once &= ~cur_mask[j0:j1]
    return int((once & (prev[j0 + d: j1 + d] == cur[j0:j1])).sum()), int(once.sum())


# 交叉复核：给不出数的条带，按选出来的偏移叠起来明显对不上（参与比较的行够多、一样的不到这个比例），
# （不算整行没变的行），至少有这么多条时，这个偏移是被重复的版式"凑"出来的（每隔几段就有一行完全一样的字），不认
CONTRA_MIN_ROWS = 64
CONTRA_MAX_RATIO = 0.8  # 真正的偏移下这些行几乎全一样（实测 0.99 以上）
CONTRA_STRIPS = 2


def _contradicting(prev: Fp, cur: Fp, mask: MotionMask | None, dys: list[int | None], d: int,
                   static: np.ndarray) -> int:
    """没给出数的竖带里，在偏移 d 下明显对不上的条数。

    例子：网页每 12 段重复同样的首行、只有第二行的数字不同，一次翻页（PgDn）滚了将近一屏、真正的重叠很少。
    只看到首行的右半边几条竖带会"对上"一个错的偏移（差 12 段），左边几条看得到第二行，在这个偏移下大半对不上。
    真正滚动时没给出数的条带只是字太少（空白多），叠起来照样一样。
    static（整行完全没变的行：固定的标题栏、工具栏）不参与：它们在任何非零偏移下都对不上，说明不了什么。"""
    bad = 0
    for s, v in enumerate(dys):
        if v is not None:
            continue
        if mask is None:
            once, pm = prev.prep_v(s).once, static
        else:
            once, pm = _prepare(prev.v[s], mask.v[s]).once, mask.v[s] | static
        cm = pm
        hits, tot = _overlap(prev.v[s], cur.v[s], d, once, pm, cm)
        if tot >= CONTRA_MIN_ROWS and hits < CONTRA_MAX_RATIO * tot and \
                _mismatch_runs(prev.v[s], cur.v[s], d, once, pm, cm) >= CONTRA_RUNS:
            bad += 1
    return bad


# 对不上的行要分散在好几处（每段的第二行）才算矛盾；集中在一处是滚动中的视频、动画（还没认出是一直在变的小块）
CONTRA_RUNS = 4
_RUN_GAP = 8


def _mismatch_runs(prev: np.ndarray, cur: np.ndarray, d: int, once_all: np.ndarray,
                   prev_mask: np.ndarray | None, cur_mask: np.ndarray | None) -> int:
    """偏移 d 下对不上的行（只看参与比较的行）分成几段（相隔不到 _RUN_GAP 行的算一段）。"""
    j0, j1 = max(0, -d), min(len(cur), len(prev) - d)
    once = once_all[j0 + d: j1 + d].copy()
    if prev_mask is not None:
        once &= ~prev_mask[j0 + d: j1 + d]
    if cur_mask is not None:
        once &= ~cur_mask[j0:j1]
    rows = np.flatnonzero(once & (prev[j0 + d: j1 + d] != cur[j0:j1]))
    if rows.size == 0:
        return 0
    return int(1 + (np.diff(rows) > _RUN_GAP).sum())


def _major(xs: list[int | None]) -> tuple[int | None, int, int]:
    """多条带的结论：(结论, 同意的条数, 给出数的条数)。"""
    ok = [x for x in xs if x is not None]
    if not ok:
        return None, 0, 0
    moving = [x for x in ok if x != 0]
    if moving:
        vals, counts = np.unique(np.array(moving), return_counts=True)
        i = int(counts.argmax())
        val, n = int(vals[i]), int(counts[i])
        return (val, n, len(ok)) if n >= 2 and n * 2 > len(moving) else (None, n, len(ok))
    return 0, len(ok), len(ok)


class Fp:
    """一帧的灰度图和指纹缓存：竖带、横带指纹和"当旧图用"的预处理都只算一次。

    gp 是宽高补到 32 倍数的灰度缓冲（补的部分为 0），g 是其中真正的画面 (高, 宽)。
    """

    __slots__ = ("gp", "g", "_v", "_h", "_prep_v", "_prep_h", "_u64")

    def __init__(self, g: np.ndarray, *, padded: np.ndarray | None = None):
        h, w = g.shape[:2]
        if padded is None:
            padded = np.zeros((_pad_to(h), _pad_to(w)), dtype=np.uint8)
            padded[:h, :w] = g
        self.gp = padded
        self.g = padded[:h, :w]
        self._v: np.ndarray | None = None
        self._h: np.ndarray | None = None
        self._prep_v: list[_Prepared | None] | None = None
        self._prep_h: list[_Prepared | None] | None = None
        self._u64: np.ndarray | None = None

    @classmethod
    def from_image(cls, img: np.ndarray) -> Fp:
        """从 BGR 或 BGRA 图直接写进补齐的缓冲，省一次拷贝。"""
        h, w = img.shape[:2]
        padded = np.zeros((_pad_to(h), _pad_to(w)), dtype=np.uint8)
        np.right_shift(img[:, :, 1], 4, out=padded[:h, :w])
        return cls(padded[:h, :w], padded=padded)

    @property
    def shape(self) -> tuple[int, int]:
        return self.g.shape[0], self.g.shape[1]

    @property
    def words(self) -> np.ndarray:
        """补齐缓冲按 uint64 看：(补齐高, 补齐宽 / 8)。"""
        if self._u64 is None:
            self._u64 = self.gp.view(np.uint64)
        return self._u64

    @property
    def v(self) -> np.ndarray:
        """竖带指纹 (STRIPS, 高)。"""
        if self._v is None:
            self._v = vstrips(self.g)
        return self._v

    @property
    def h(self) -> np.ndarray:
        """横带指纹 (BANDS, 宽)。"""
        if self._h is None:
            self._h = hbands(self.g)
        return self._h

    def prep_v(self, s: int) -> _Prepared:
        if self._prep_v is None:
            self._prep_v = [None] * STRIPS
        p = self._prep_v[s]
        if p is None:
            p = self._prep_v[s] = _prepare(self.v[s])
        return p

    def prep_h(self, b: int) -> _Prepared:
        if self._prep_h is None:
            self._prep_h = [None] * BANDS
        p = self._prep_h[b]
        if p is None:
            p = self._prep_h[b] = _prepare(self.h[b])
        return p


def rows_equal(a: Fp, b: Fp) -> np.ndarray:
    """每一行是否完全相同，形状 (高,)。两帧尺寸必须相同。"""
    h = a.shape[0]
    return (a.words[:h] == b.words[:h]).all(axis=1)


def tile_changes(a: Fp, b: Fp) -> np.ndarray:
    """哪些 32×32 小块有像素变了，形状 (补齐高/32, 补齐宽/32)。"""
    diff = a.words != b.words
    th, tw = diff.shape[0] // TILE, diff.shape[1] // (TILE // 8)
    return diff.reshape(th, TILE, tw, TILE // 8).any(axis=(1, 3))


@dataclass(frozen=True)
class MotionMask:
    """不参与比对的小块和换算好的行（列）遮罩。True = 忽略。

    tiles: (补齐高/32, 补齐宽/32)；v: (STRIPS, 高)；h: (BANDS, 宽)。
    """

    tiles: np.ndarray
    v: np.ndarray
    h: np.ndarray

    @classmethod
    def from_tiles(cls, tiles: np.ndarray, height: int, width: int) -> MotionMask:
        se = strip_edges(width)
        be = band_edges(height)
        tile_rows = np.repeat(tiles, TILE, axis=0)[:height]   # (高, 小块列)
        tile_cols = np.repeat(tiles, TILE, axis=1)[:, :width]  # (小块行, 宽)
        v = np.zeros((STRIPS, height), dtype=bool)
        for s in range(STRIPS):
            a, b = se[s], se[s + 1]
            if b > a:
                v[s] = tile_rows[:, a // TILE: (b - 1) // TILE + 1].any(axis=1)
        h = np.zeros((BANDS, width), dtype=bool)
        for i in range(BANDS):
            a, b = be[i], be[i + 1]
            if b > a:
                h[i] = tile_cols[a // TILE: (b - 1) // TILE + 1].any(axis=0)
        return cls(tiles, v, h)


@dataclass(frozen=True)
class Motion:
    """相邻两帧的结论。

    kind: 'same' 没变（或只在被遮的块里变）；'shift' 平移了 (dy, dx)；
          'inplace' 位置没动、内容原地变了；'break' 接不上（宁可接不上也不给错的数）。
    dy/dx: 正数 = 内容往上/往左移（相当于往下/往右滚）。break 时为 None。
    """

    kind: str
    dy: int | None
    dx: int | None
    agree: str
    same_rows: float
    dys: list[int | None] = field(default_factory=list)
    dxs: list[int | None] | None = None


def analyze(prev: Fp, cur: Fp, mask: MotionMask | None = None, *,
            rows_eq: np.ndarray | None = None, tiles: np.ndarray | None = None) -> Motion:
    """比较上一帧和这一帧。尺寸不同（窗口改了大小）直接算接不上。

    rows_eq / tiles 可以传入已经算好的 rows_equal / tile_changes 结果，省得重算。
    """
    if prev.shape != cur.shape:
        return Motion("break", None, None, "尺寸变了", 0.0)
    if rows_eq is None:
        rows_eq = rows_equal(prev, cur)
    same_rows = float(rows_eq.mean()) if len(rows_eq) else 1.0
    if rows_eq.all():
        return Motion("same", 0, 0, "完全相同", 1.0)
    if mask is not None:
        if tiles is None:
            tiles = tile_changes(prev, cur)
        if not (tiles & ~mask.tiles).any():
            return Motion("same", 0, 0, "只在一直变的小块里变", same_rows)
        dys = [shift_1d(prev.v[s], cur.v[s], prev_mask=mask.v[s], cur_mask=mask.v[s]) for s in range(STRIPS)]
    else:
        dys = [shift_1d(prev.v[s], cur.v[s], prepared=prev.prep_v(s)) for s in range(STRIPS)]
    my, ny, cy = _major(dys)
    dxs = None
    mx, nx, cx = None, 0, 0
    if my in (None, 0):
        if mask is not None:
            dxs = [shift_1d(prev.h[b], cur.h[b], prev_mask=mask.h[b], cur_mask=mask.h[b]) for b in range(BANDS)]
        else:
            dxs = [shift_1d(prev.h[b], cur.h[b], prepared=prev.prep_h(b)) for b in range(BANDS)]
        mx, nx, cx = _major(dxs)
    agree = f"上下 {ny}/{cy}/{STRIPS}" + (f"，左右 {nx}/{cx}/{BANDS}" if dxs is not None else "")
    if my not in (None, 0):
        bad = _contradicting(prev, cur, mask, dys, my, rows_eq)
        if bad >= CONTRA_STRIPS:  # 宁可接不上也不给错的数：错的偏移会把两屏的行交错着拼进同一块
            return Motion("break", None, None, f"{agree}，{bad} 条在这个偏移下对不上", same_rows, dys, dxs)
        return Motion("shift", my, 0, agree, same_rows, dys, dxs)
    if mx not in (None, 0):
        return Motion("shift", 0, mx, agree, same_rows, dys, dxs)
    if my == 0:
        return Motion("inplace", 0, 0, agree, same_rows, dys, dxs)
    return Motion("break", None, None, agree, same_rows, dys, dxs)
