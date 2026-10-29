"""motion.py 的同一套算法和阈值，只省掉重复劳动：
1. 每张图的指纹只算一次，存在 Fp 里，下一次比对直接拿来用（原版每次比对都把上一张重算一遍）；
2. 上下已经算出"平移了 N"，就不再算左右（横带指纹也是用到才算）；
3. "整行没变的比例"直接逐字节比，不再算整行指纹。
"""
from __future__ import annotations

import numpy as np

from motion import BANDS, STRIPS, green, hbands, shift_1d, vstrips  # noqa: F401
from motion import analyze as _analyze_ref  # noqa: F401


class Fp:
    def __init__(self, g: np.ndarray):
        self.g = g
        self._v = self._h = None

    @property
    def v(self):
        if self._v is None:
            self._v = vstrips(self.g)
        return self._v

    @property
    def h(self):
        if self._h is None:
            self._h = hbands(self.g)
        return self._h


def _major(xs):
    from collections import Counter
    ok = [x for x in xs if x is not None]
    if not ok:
        return None, 0, 0
    moving = [x for x in ok if x != 0]
    if moving:
        val, n = Counter(moving).most_common(1)[0]
        return (val, n, len(ok)) if n >= 2 and n * 2 > len(moving) else (None, n, len(ok))
    return 0, len(ok), len(ok)


def analyze(prev: Fp, cur: Fp) -> dict:
    dy = [shift_1d(a, b) for a, b in zip(prev.v, cur.v)]
    my, ny, cy = _major(dy)
    if my not in (None, 0):
        dx, (mx, nx, cx) = None, (None, 0, 0)
    else:
        dx = [shift_1d(a, b) for a, b in zip(prev.h, cur.h)]
        mx, nx, cx = _major(dx)
    same_rows = float((prev.g == cur.g).all(axis=1).mean())
    return {"dy": dy, "dx": dx, "上下结论": my, "上下同意": f"{ny}/{cy}/{len(dy)}",
            "左右结论": mx, "左右同意": f"{nx}/{cx}/{BANDS}", "整行没变的比例": round(same_rows, 2)}
