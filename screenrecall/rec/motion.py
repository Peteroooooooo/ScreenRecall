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
- （v2 motion2）整屏复核：几个候选偏移各按所有竖带一起叠起来，只看在变的行，数"独特格子"和"有内容的格子"
  对上的比例。另有一个偏移差不多一样说得通 → 接不上；最好的站得住 → 用它（一条竖带看得到独特内容也接得上）；
  "没动"只认在变的行投的票（静止的标题栏、工具栏、页边、空白行说"没动"不算）。详见 _verify。

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


@lru_cache(maxsize=32)
def _const_fps(width: int) -> np.ndarray:
    """每条竖带上"整行都是同一个值 v"的指纹表，形状 (STRIPS, 16)。"""
    _, lens = _layout(width, STRIPS)
    out = np.zeros((STRIPS, 16), dtype=np.uint64)
    for s, n in enumerate(lens):
        rows = np.repeat(np.arange(16, dtype=np.uint8)[:, None], max(int(n), 1), axis=1)
        if n == 0:
            rows[:] = 0
        out[s] = row_fingerprints(rows)
    return out


def content_cells(fp: Fp) -> np.ndarray:
    """哪些格子（竖带 × 行）有内容（不是纯色），形状 (STRIPS, 高)。"""
    return fp.content


@dataclass
class _Prepared:
    """一条带作为"旧图"时要用到的预处理，缓存起来，同一张图当旧图时不重算。"""

    uniq_vals: np.ndarray  # 取样里只出现一次的指纹（已排序）
    uniq_pos: np.ndarray   # 对应的行号
    once: np.ndarray       # 每一行在整条里是否只出现一次


def _prepare(prev: np.ndarray, prev_mask: np.ndarray | None = None, once: np.ndarray | None = None) -> _Prepared:
    """once 可以传入已经算好的"每一行在整条里是否只出现一次"（Fp.vsorted 一次排序算出全部竖带的）。"""
    samp_pos = np.arange(0, len(prev), K)
    samp = prev[::K]
    if prev_mask is not None:
        keep = ~prev_mask[::K]
        samp_pos, samp = samp_pos[keep], samp[keep]
    vals, first, counts = np.unique(samp, return_index=True, return_counts=True)
    single = counts == 1
    if once is None:
        _, inv, cnt_all = np.unique(prev, return_inverse=True, return_counts=True)
        once = cnt_all[inv.reshape(-1)] == 1
    return _Prepared(vals[single], samp_pos[first[single]], once)


# 把各条竖带的指纹加上不同的"盐"后放在一起排序：一次排序就知道每条带里哪些行只出现一次、相同的行在哪
_SALT = _weights(STRIPS)[:, None] * np.uint64(0x2545F4914F6CDD1D)


@dataclass(frozen=True)
class _Sorted:
    keys: np.ndarray    # 排好序的 (指纹 + 盐)，长 STRIPS × 高
    rows: np.ndarray    # 对应的行号
    once: np.ndarray    # (STRIPS, 高) 这一行在这条带里只出现一次


def _sort_strips(v: np.ndarray) -> _Sorted:
    n_s, h = v.shape
    keys = (v + _SALT[:n_s]).ravel()
    order = np.argsort(keys, kind="stable")
    sk = keys[order]
    n = sk.size
    if n == 0:
        return _Sorted(sk, order, np.zeros(v.shape, dtype=bool))
    new = np.empty(n, dtype=bool)
    new[0] = True
    np.not_equal(sk[1:], sk[:-1], out=new[1:])
    starts = np.flatnonzero(new)
    run_len = np.diff(np.append(starts, n))
    once_sorted = np.repeat(run_len == 1, run_len)
    once = np.empty(n, dtype=bool)
    once[order] = once_sorted
    return _Sorted(sk, (order % h).astype(np.int64), once.reshape(v.shape))


def shift_1d(prev: np.ndarray, cur: np.ndarray, *, prev_mask: np.ndarray | None = None,
             cur_mask: np.ndarray | None = None, prepared: _Prepared | None = None) -> int | None:
    """一条带上的偏移。正数 = 内容往上（左）移。给不出可靠的数时返回 None。

    遮罩（True = 忽略这一行）：旧图里被遮的行不参与投票和复核，新图里被遮的行不投票、不参与复核。
    传了 prepared 时它必须来自同一个 prev，并且是按同一个 prev_mask 算的。
    """
    p = prepared if prepared is not None else _prepare(prev, prev_mask)
    return _shift_from_votes(prev, cur, p, _votes(p, cur, cur_mask), prev_mask, cur_mask)


def _votes(p: _Prepared, cur: np.ndarray, cur_mask: np.ndarray | None) -> np.ndarray | None:
    """新图每一行在旧图（只出现一次的取样行）里找到的位置差，没有可比的行时 None。"""
    pp = _vote_rows(p, cur, cur_mask)
    return None if pp is None else pp[0] - pp[1]


def _vote_rows(p: _Prepared, cur: np.ndarray, cur_mask: np.ndarray | None) -> tuple[np.ndarray, np.ndarray] | None:
    """(旧图行号, 新图行号)：新图的行在旧图只出现一次的取样行里找到的位置。"""
    if len(p.uniq_vals) == 0 or len(cur) == 0:
        return None
    loc = np.searchsorted(p.uniq_vals, cur)
    loc[loc >= len(p.uniq_vals)] = 0
    hit = p.uniq_vals[loc] == cur
    if cur_mask is not None:
        hit &= ~cur_mask
    j = np.flatnonzero(hit)
    return p.uniq_pos[loc[j]], j


def _shift_from_votes(prev: np.ndarray, cur: np.ndarray, p: _Prepared, offs: np.ndarray | None,
                      prev_mask: np.ndarray | None, cur_mask: np.ndarray | None) -> int | None:
    if len(p.uniq_vals) < MIN_UNIQUE or offs is None or offs.size == 0:
        return None
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
# "原地变了"至少要这么多条竖带说没动，或者至少这么多行完全没变
INPLACE_MIN_STRIPS = 3
INPLACE_SAME_ROWS = 0.5
INPLACE_MIN_VOTES = 2  # 一条竖带说"没动"，至少要有这么多张在变的行（不是整行没变的行）投"没动"的票


def _moving_zero_votes(r: tuple[np.ndarray, np.ndarray] | None, static: np.ndarray) -> int:
    """这条竖带投"没动"（偏移 0）的票里，来自在变的行（新图这一行不是整行没变）的有几张。"""
    if r is None:
        return 0
    pp, j = r
    return int(((pp == j) & ~static[j]).sum())


def _contradicting(prev: Fp, cur: Fp, mask: MotionMask | None, dys: list[int | None], d: int,
                   static: np.ndarray, preps: list[_Prepared]) -> int:
    """没给出数的竖带里，在偏移 d 下明显对不上的条数。

    例子：网页每 12 段重复同样的首行、只有第二行的数字不同，一次翻页（PgDn）滚了将近一屏、真正的重叠很少。
    只看到首行的右半边几条竖带会"对上"一个错的偏移（差 12 段），左边几条看得到第二行，在这个偏移下大半对不上。
    真正滚动时没给出数的条带只是字太少（空白多），叠起来照样一样。
    static（整行完全没变的行：固定的标题栏、工具栏）不参与：它们在任何非零偏移下都对不上，说明不了什么。"""
    bad = 0
    for s, v in enumerate(dys):
        if v is not None:
            continue
        once = preps[s].once
        pm = static if mask is None else mask.v[s] | static
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


# 整屏复核（v2 motion2）：模板化的页面（日志、表格、头像一样的聊天、重复行很多的代码）大多数行在别处还有一模一样的，
# 一条竖带说的数常常只是"差几段"的错数；静止的标题栏、工具栏、页边又会让几条竖带说"没动"。
# 做法：选出几个候选偏移，每个候选按所有竖带一起叠起来，只看"在动的行"（整行没变的行、一直变的小块都不算），
# 数两种格子对上的比例：
# - 独特格子：在旧图这条带里只出现一次的行（模板化内容里只有它们分得清"差几段"）；
# - 有内容的格子：不是纯色的（模板行也算，真正的偏移下它们也得对上）。
# 候选：各条带的票（只出现一次的行）合在一起的前几名，加上"所有相同的行"给出的前几名（重复版式的错位候选
# 只从这里来：它们的重叠区里没有独特的行，不投票）。
# 最好的候选 = 说得通（独特格子够数并且过半对上）里对不上比例最低的；站得住 = 独特格子和有内容的格子都几乎全对上。
# 另有一个候选和它差不多一样解释得了画面（有内容的格子对上的比例差不多、重叠不算太少、独特格子不明显更差）
# = 重复的版式，没法知道真正滚了多少 → 接不上。
# 最好的候选站得住、没有对手 → 就是它（只有一两条竖带看得到独特内容时也接得上）；
# 有候选说得通却站不住 → 不说"原地变了"（说"没动"的条带多半只看到静止的标题栏、工具栏、页边）。
VERIFY_TOP = 4             # 合并的票里取前几名（非零）偏移
VERIFY_MIN_VOTES = 2       # 候选至少几票
ALIAS_TOP = 4              # "所有相同的行"给出的偏移取前几名
ALIAS_PERIOD_TOP = 8       # 前几名都拿来试"旧图自己是不是每隔 (候选之差) 行就重复"（只比一次，很便宜）
ALIAS_MAX_DUP = 8         # 旧图里同样的行超过这么多处（一屏都是的模板行）的不用来找候选
ALIAS_MIN_VOTES = 4
PLAUSIBLE_MIN_ONCE = 8     # 说得通：至少这么多独特格子参与比较
PLAUSIBLE_RATIO = 0.5      # 说得通：独特格子至少这么多对上（实测错的偏移最多 0.36）
STRONG_MIN_ONCE = 16       # 站得住：至少这么多独特格子
STRONG_RATIO = 0.85        # 站得住：独特格子对上的比例（实测真偏移 0.86 以上，大多 0.95 以上）
STRONG_MIN_CELLS = 64      # 站得住：至少这么多有内容的格子
STRONG_CELL_RATIO = 0.85   # 站得住：有内容的格子对上的比例（滚动条、标题栏变的字会占掉一点）
RIVAL_CELL_MARGIN = 0.05   # 对手：有内容的格子对上的比例最多比最好的低这么多
# 对手被独特的行否定：它对不上的独特格子比最好的那个多出 max(8, 它的独特格子数 × 0.15) 个以上，
# 并且多出来的分在至少 2 条竖带里（每条至少多 2 个）。只多在一条里不算：滚动条滑块不跟着内容走，
# 和旁边的内容拼成"独特"的行，哪个偏移下对不上得多只看滑块落在哪，说明不了什么
RIVAL_EXTRA_MISS = 8
RIVAL_MISS_SHARE = 0.15
# 旧图自己错开 p 行（p = 最好的候选和别的候选之差）后，在动的行里有内容的格子至少这么多、对不上的不到这个比例：
# 版式完全周期（实测头像一样的聊天 0.09，主要是滚动条滑块；表格、日志、代码在各种 p 下都在 0.4 以上）
SELF_MIN_PERIOD = 8
SELF_MIN_CELLS = 256
SELF_MAX_MISS = 0.15
# 而且有对不上的格子的行不到有内容的行的这么多：聊天页对不上的只是滚动条滑块两端那几行（实测 0.06–0.18），
# 表格里窄窄一列各不相同的序号让大多数行都对不上（实测 0.44 以上）
SELF_MAX_ROWS = 0.3
# 一条竖带里对不上的行只连成一两整块、不到比较的行的一半：滚动条滑块（每屏只有一个，错开 p 行比自己时总对不上），
# 先去掉再算上面两个比例
SELF_UI_RUNS = 2
SELF_UI_ROWS = 0.5



def _self_periodic(prev: Fp, keep_rows: np.ndarray, p: int) -> bool:
    h = prev.shape[0]
    if p >= h:
        return False
    keep = keep_rows[: h - p] & keep_rows[p:]
    c = (prev.content[:, : h - p] | prev.content[:, p:]) & keep
    n = int(np.count_nonzero(c))
    if n < SELF_MIN_CELLS:
        return False
    bad = c & (prev.v[:, : h - p] != prev.v[:, p:])
    n_rows = int(np.count_nonzero(c.any(axis=0)))
    for s in np.flatnonzero(bad.any(axis=1)):
        rows = np.flatnonzero(bad[s])
        if rows.size <= SELF_UI_ROWS * n_rows and int(1 + (np.diff(rows) > _RUN_GAP).sum()) <= SELF_UI_RUNS:
            bad[s] = False  # 只是一两整块（滚动条滑块）：跟着窗口走、不跟着内容走，不算内容对不上
    if int(np.count_nonzero(bad)) >= SELF_MAX_MISS * n:
        return False
    return int(np.count_nonzero(bad.any(axis=0))) < SELF_MAX_ROWS * n_rows
RIVAL_STRIP_MISS = 2
RIVAL_STRIPS = 2


def _alias_candidates(prev: Fp, cur: Fp, static: np.ndarray) -> list[int]:
    """所有相同的有内容的行（旧图里出现不超过 ALIAS_MAX_DUP 处）给出的偏移，票数前几名（非零）。
    完全重复的版式里，错开整段的偏移在这里和真偏移一样多票；新图的行隔一行取一行。"""
    h = prev.shape[0]
    ps = prev.vsorted
    sel = cur.content & ~static
    sel[:, 1::2] = False  # 新图隔一行取一行
    s_idx, cj = np.nonzero(sel)
    if cj.size == 0:
        return []
    ck = cur.v[s_idx, cj] + _SALT[s_idx, 0]
    lo = np.searchsorted(ps.keys, ck, "left")
    n = np.searchsorted(ps.keys, ck, "right") - lo
    ok = (n >= 1) & (n <= ALIAS_MAX_DUP)  # 只和有内容的行比：纯色行不在 sel 里，指纹相同就是内容相同
    lo, n, cj = lo[ok], n[ok], cj[ok]
    if n.size == 0:
        return []
    rep = np.repeat(np.arange(n.size), n)
    idx = lo[rep] + np.arange(rep.size) - np.repeat(np.cumsum(n) - n, n)
    hist = np.bincount(ps.rows[idx] - cj[rep] + h, minlength=2 * h + 1)
    hist[h] = 0
    top = np.argpartition(-hist, ALIAS_PERIOD_TOP)[:ALIAS_PERIOD_TOP] if hist.size > ALIAS_PERIOD_TOP \
        else np.arange(hist.size)
    top = top[np.argsort(-hist[top], kind="stable")]
    return [int(t) - h for t in top if hist[t] >= ALIAS_MIN_VOTES]


@dataclass(frozen=True)
class _Fit:
    """一个候选偏移下所有竖带叠起来的结果。"""

    oh: int                # 独特格子对上数
    ot: int                # 独特格子数
    ch: int                # 有内容格子对上数
    ct: int                # 有内容格子数
    miss: np.ndarray       # 每条竖带对不上的独特格子数 (STRIPS,)


_NO_FIT = _Fit(0, 0, 0, 0, np.zeros(STRIPS, dtype=np.int64))


def _explain(prev: Fp, cur: Fp, d: int, once_ok: np.ndarray, cont_ok: np.ndarray, cur_ok: np.ndarray) -> _Fit:
    """按偏移 d 把所有竖带叠起来数格子。形状都是 (STRIPS, 高)：once_ok / cont_ok 是旧图里参与比较的
    独特格子 / 有内容格子，cur_ok 是新图里参与比较的格子（整行没变的行、一直变的小块都已去掉）。"""
    h = prev.shape[0]
    j0, j1 = max(0, -d), min(h, h - d)
    if j1 <= j0:
        return _NO_FIT
    ok = cur_ok[:, j0:j1]
    ne = prev.v[:, j0 + d: j1 + d] != cur.v[:, j0:j1]
    o = once_ok[:, j0 + d: j1 + d] & ok
    c = cont_ok[:, j0 + d: j1 + d] & ok
    ot, ct = int(np.count_nonzero(o)), int(np.count_nonzero(c))
    o &= ne
    c &= ne
    miss = np.count_nonzero(o, axis=1)
    return _Fit(ot - int(miss.sum()), ot, ct - int(np.count_nonzero(c)), ct, miss)


@dataclass(frozen=True)
class _Verdict:
    best: int | None      # 说得通的候选里最好的（没有时 None）
    strong: bool          # best 站得住
    rivals: int           # 和 anchor（best，没有 best 时是条带多数说的数）差不多一样解释得了画面的其他偏移个数


def _verify(prev: Fp, cur: Fp, mask: MotionMask | None, static: np.ndarray,
            vrows: list[tuple[np.ndarray, np.ndarray] | None], dys: list[int | None], my: int | None) -> _Verdict:
    once = prev.vsorted.once
    # 合并的票只要独特的行投的（取样里只出现一次、整条里也只出现一次）、新旧两边都不是整行没变的行：
    # 模板行（取样里碰巧只有一次）投的票大多是差几行的错数，会把真偏移挤出前几名
    parts = []
    for s, r in enumerate(vrows):
        if r is None or r[0].size == 0:
            continue
        pp, j = r
        ok = once[s][pp] & ~static[pp] & ~static[j]
        parts.append(pp[ok] - j[ok])
    pooled = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int64)
    pooled = pooled[pooled != 0]
    cands: list[int] = []
    if pooled.size:
        vals, counts = np.unique(pooled, return_counts=True)
        order = np.argsort(-counts, kind="stable")[:VERIFY_TOP]
        cands = [int(vals[i]) for i in order if counts[i] >= VERIFY_MIN_VOTES]
    if not cands and my in (None, 0):
        return _Verdict(None, False, 0)  # 没有任何"在动"的迹象（原地变了 / 接不上），不必再找重复版式的错位
    aliases = _alias_candidates(prev, cur, static)
    for d in [d for d in dys if d not in (None, 0)] + aliases[:ALIAS_TOP]:
        if int(d) not in cands:
            cands.append(int(d))
    # 原地变了一部分的行（网页标题、标签页、状态栏）：在变，但同一位置还有独特的格子没变 —— 它们属于不动的版面，
    # 不能拿来判断内容滚了多少（混进来时，重叠少的候选会被它们拖低，重复版式的错位就挤不进"对手"）
    part = np.logical_not(static) & ((prev.v == cur.v) & once & cur.vsorted.once).any(axis=0)
    still = static | part
    keep = np.logical_not(still if mask is None else mask.v | still)  # (高,) 或 (STRIPS, 高)
    once_ok, cont_ok = once & keep, prev.content & keep
    cur_ok = np.broadcast_to(keep, once.shape)
    sc = {d: _explain(prev, cur, d, once_ok, cont_ok, cur_ok) for d in cands}
    plaus = [(1 - f.oh / f.ot, -f.ot, d) for d, f in sc.items()
             if f.ot >= PLAUSIBLE_MIN_ONCE and f.oh >= PLAUSIBLE_RATIO * f.ot]
    best = min(plaus)[2] if plaus else None
    strong = False
    if best is not None:
        f = sc[best]
        strong = f.ot >= STRONG_MIN_ONCE and f.oh >= STRONG_RATIO * f.ot and f.ct >= STRONG_MIN_CELLS \
            and f.ch >= STRONG_CELL_RATIO * f.ct
    anchor = best if best is not None else (my if my not in (None, 0) else None)
    if anchor is None:
        return _Verdict(None, False, 0)
    # 旧图在动的部分本身每隔 p 行就重复（完全周期的版式）：滚了 d 和 d ± p 看起来一模一样，没法知道是哪个
    keep_rows = np.logical_not(still)
    for p in sorted({abs(anchor - d) for d in [*sc, *aliases] if abs(anchor - d) >= SELF_MIN_PERIOD}):
        if _self_periodic(prev, keep_rows, p):
            return _Verdict(best, strong, 1)
    if anchor not in sc:
        sc[anchor] = _explain(prev, cur, anchor, once_ok, cont_ok, cur_ok)
    a = sc[anchor]
    if a.ct == 0:
        return _Verdict(best, strong, 0)
    a_cell = a.ch / a.ct
    rivals = 0
    for d, f in sc.items():
        # 重叠少的也算：完全重复的版式里，真偏移常常正是重叠少的那个（翻了将近一屏），重叠多的是错开整段的
        if d == anchor or f.ct < STRONG_MIN_CELLS:
            continue
        if f.ch < (a_cell - RIVAL_CELL_MARGIN) * f.ct:
            continue
        extra = np.maximum(f.miss - a.miss, 0)
        if int(extra.sum()) > max(RIVAL_EXTRA_MISS, RIVAL_MISS_SHARE * f.ot) and \
                int((extra >= RIVAL_STRIP_MISS).sum()) >= RIVAL_STRIPS:
            continue  # 被独特的行否定了：好几条竖带里比最好的那个多出不少对不上的独特格子
        rivals += 1
    return _Verdict(best, strong, rivals)


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

    __slots__ = ("gp", "g", "_v", "_h", "_prep_v", "_prep_h", "_u64", "_content", "_vs", "_hs")

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
        self._content: np.ndarray | None = None
        self._vs: _Sorted | None = None
        self._hs: _Sorted | None = None

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

    @property
    def content(self) -> np.ndarray:
        """哪些格子（竖带 × 行）有内容（不是纯色），形状 (STRIPS, 高)。"""
        if self._content is None:
            w = self.shape[1]
            starts = strip_edges(w)[:-1]
            first = self.g[:, np.minimum(starts, w - 1)].T  # (STRIPS, 高)，每格第一个取样像素的值
            const = _const_fps(w)[np.arange(STRIPS)[:, None], first]
            self._content = self.v != const
        return self._content

    @property
    def vsorted(self) -> _Sorted:
        """所有竖带指纹一起排序的结果（找相同的行、算"只出现一次"用）。"""
        if self._vs is None:
            self._vs = _sort_strips(self.v)
        return self._vs

    def prep_v(self, s: int) -> _Prepared:
        if self._prep_v is None:
            self._prep_v = [None] * STRIPS
        p = self._prep_v[s]
        if p is None:
            p = self._prep_v[s] = _prepare(self.v[s], once=self.vsorted.once[s])
        return p

    def prep_h(self, b: int) -> _Prepared:
        if self._prep_h is None:
            self._prep_h = [None] * BANDS
        p = self._prep_h[b]
        if p is None:
            if self._hs is None:
                self._hs = _sort_strips(self.h)
            p = self._prep_h[b] = _prepare(self.h[b], once=self._hs.once[b])
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
        once_all = prev.vsorted.once
        preps = [_prepare(prev.v[s], mask.v[s], once=once_all[s]) for s in range(STRIPS)]
        vmask: list[np.ndarray | None] = list(mask.v)
    else:
        preps = [prev.prep_v(s) for s in range(STRIPS)]
        vmask = [None] * STRIPS
    vrows = [_vote_rows(preps[s], cur.v[s], vmask[s]) for s in range(STRIPS)]
    offs = [None if r is None else r[0] - r[1] for r in vrows]
    dys = [_shift_from_votes(prev.v[s], cur.v[s], preps[s], offs[s], vmask[s], vmask[s]) for s in range(STRIPS)]
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
    # 整屏复核（只管上下）：宁可接不上也不给错的数
    vd = _verify(prev, cur, mask, rows_eq, vrows, dys, my)
    if vd.rivals:  # 重复的版式：几个偏移都说得通，不知道真正滚了多少
        return Motion("break", None, None, f"{agree}，{vd.rivals + 1} 个偏移都说得通", same_rows, dys, dxs)
    if vd.best is not None and my not in (None, 0) and vd.best != my:  # 条带多数说的数解释不了独特的行
        return Motion("break", None, None, f"{agree}，独特的行在别的偏移下对上", same_rows, dys, dxs)
    if vd.strong and mx in (None, 0):
        d = vd.best
        bad = _contradicting(prev, cur, mask, dys, d, rows_eq, preps)
        if bad >= CONTRA_STRIPS:
            return Motion("break", None, None, f"{agree}，{bad} 条在这个偏移下对不上", same_rows, dys, dxs)
        return Motion("shift", d, 0, agree + ("" if d == my else "，整屏复核站得住"), same_rows, dys, dxs)
    if vd.best is not None and my in (None, 0):
        # 在动的行在某个偏移下大半对得上、但站不住（或者左右也说在动）：说不准滚了多少，也不能说"原地变了"
        # （说"没动"的条带多半只看到静止的标题栏、工具栏、页边）
        return Motion("break", None, None, f"{agree}，像是滚了但站不住", same_rows, dys, dxs)
    if my not in (None, 0):
        bad = _contradicting(prev, cur, mask, dys, my, rows_eq, preps)
        if bad >= CONTRA_STRIPS:  # 宁可接不上也不给错的数：错的偏移会把两屏的行交错着拼进同一块
            return Motion("break", None, None, f"{agree}，{bad} 条在这个偏移下对不上", same_rows, dys, dxs)
        return Motion("shift", my, 0, agree, same_rows, dys, dxs)
    if mx not in (None, 0):
        return Motion("shift", 0, mx, agree, same_rows, dys, dxs)
    if my == 0:
        # "没动"要有在变的行撑腰：只靠整行没变的行（静止的标题栏、工具栏、页边、空白行）说"没动"的条带不算，
        # 整行没变的比例也只在有内容的行里算 —— 否则翻了一大截（重叠太少认不出偏移）也会被当成原地变了
        n_real = sum(1 for s, d in enumerate(dys) if d == 0 and _moving_zero_votes(vrows[s], rows_eq) >= INPLACE_MIN_VOTES)
        rows_c = prev.content.any(axis=0)
        same_c = float(rows_eq[rows_c].mean()) if rows_c.any() else same_rows
        if n_real >= INPLACE_MIN_STRIPS or same_c >= INPLACE_SAME_ROWS:
            return Motion("inplace", 0, 0, agree, same_rows, dys, dxs)
        # 大半画面都变了，说"没动"的只有一两条（多半是页边、滚动条）：说不准是原地变了还是滚了一大截
        # （翻页时真正的偏移认不出来），当成接不上（新块），不把新一屏的行按旧位置写进同一块
        return Motion("break", None, None, f"{agree}，只有 {n_real} 条说没动", same_rows, dys, dxs)
    return Motion("break", None, None, agree, same_rows, dys, dxs)
