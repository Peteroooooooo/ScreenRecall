"""只靠像素估"哪块往哪个方向移了多少"：竖带估上下，横带估左右。"""
from __future__ import annotations

from collections import Counter

import numpy as np

K = 4          # 旧图每 K 行（列）取一个做比对
STRIPS = 12    # 竖着切几条（估上下）
BANDS = 8      # 横着切几条（估左右）


def green(bgr_or_bgra: np.ndarray) -> np.ndarray:
    return bgr_or_bgra[:, :, 1] >> 4


def _row_hashes(a: np.ndarray) -> list[int]:
    return [hash(r.tobytes()) for r in a]


def vstrips(g: np.ndarray, n: int = STRIPS) -> list[list[int]]:
    e = np.linspace(0, g.shape[1], n + 1).astype(int)
    return [_row_hashes(g[:, e[i]:e[i + 1]:2]) for i in range(n)]


def hbands(g: np.ndarray, n: int = BANDS) -> list[list[int]]:
    e = np.linspace(0, g.shape[0], n + 1).astype(int)
    return [_row_hashes(np.ascontiguousarray(g[e[i]:e[i + 1]:2, :].T)) for i in range(n)]


def shift_1d(prev: list[int], cur: list[int]) -> int | None:
    """正数 = 内容往上（左）移。对上的行 ≥ 8 且是第二名的 3 倍以上才算数。"""
    pos: dict[int, list[int]] = {}
    for i in range(0, len(prev), K):
        pos.setdefault(prev[i], []).append(i)
    uniq = {r: v[0] for r, v in pos.items() if len(v) == 1}
    if len(uniq) < 6:
        return None
    votes: Counter[int] = Counter()
    for j, r in enumerate(cur):
        if r in uniq:
            votes[uniq[r] - j] += 1
    if not votes:
        return None
    top = votes.most_common(2)
    v, second = top[0][1], (top[1][1] if len(top) > 1 else 0)
    d = top[0][0]
    if not (v >= 8 and v >= 3 * second):
        return None
    # 复核：按这个偏移把两张图叠起来，重叠部分里"有内容的行"（在旧图里只出现一次的行）至少六成要完全一样
    seen = Counter(prev)
    hit = tot = 0
    for j, r in enumerate(cur):
        i = j + d
        if 0 <= i < len(prev) and seen[prev[i]] == 1:
            tot += 1
            hit += prev[i] == r
    return d if tot and hit / tot >= 0.6 else None


def analyze(prev_g: np.ndarray, cur_g: np.ndarray) -> dict:
    dy = [shift_1d(a, b) for a, b in zip(vstrips(prev_g), vstrips(cur_g))]
    dx = [shift_1d(a, b) for a, b in zip(hbands(prev_g), hbands(cur_g))]

    def major(xs):
        ok = [x for x in xs if x is not None]
        if not ok:
            return None, 0, 0
        moving = [x for x in ok if x != 0]
        if moving:
            # 有在动的竖带：至少 2 条给出同一个数，且占在动竖带的多数，才算数；否则宁可说"接不上"
            val, n = Counter(moving).most_common(1)[0]
            return (val, n, len(ok)) if n >= 2 and n * 2 > len(moving) else (None, n, len(ok))
        return 0, len(ok), len(ok)

    my, ny, cy = major(dy)
    mx, nx, cx = major(dx)
    same_rows = sum(a == b for a, b in zip(_row_hashes(prev_g), _row_hashes(cur_g))) / prev_g.shape[0]
    return {"dy": dy, "dx": dx, "上下结论": my, "上下同意": f"{ny}/{cy}/{len(dy)}",
            "左右结论": mx, "左右同意": f"{nx}/{cx}/{len(dx)}", "整行没变的比例": round(same_rows, 2)}
