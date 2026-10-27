"""OCR 对照用的文字指标：编辑距离（位并行，长文本也快）、字符错误率、英文空格保留、生僻 / 繁体字判断。"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_ASCII_GAP = re.compile(r"(?<=[A-Za-z0-9]) (?=[A-Za-z0-9])")


def levenshtein(a: str, b: str) -> int:
    """Hyyrö 位并行编辑距离（插入 / 删除 / 替换各算 1）。"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    m = len(a)
    peq: dict[str, int] = {}
    for i, ch in enumerate(a):
        peq[ch] = peq.get(ch, 0) | (1 << i)
    mask = (1 << m) - 1
    high = 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for ch in b:
        eq = peq.get(ch, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = mv | (~(xh | pv) & mask)
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = mh | (~(xv | ph) & mask)
        mv = ph & xv
    return score


def _dp(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def squash(text: str) -> str:
    """比较用：NFKC（全角半角统一）后去掉全部空白。"""
    return _WS.sub("", unicodedata.normalize("NFKC", text))


def cer(truth: str, hyp: str) -> float:
    t, h = squash(truth), squash(hyp)
    return levenshtein(t, h) / max(1, len(t))


def agreement(a: str, b: str) -> float:
    """两段识别结果的字符一致率：1 - 编辑距离 / 较长者长度。"""
    x, y = squash(a), squash(b)
    if not x and not y:
        return 1.0
    return 1.0 - levenshtein(x, y) / max(len(x), len(y))


def ascii_gaps(text: str) -> int:
    """英文 / 数字之间的单个空格数（"Meeting at 3pm" 有 2 个）。"""
    return len(_ASCII_GAP.findall(text))


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x3134F


def is_rare_cjk(ch: str) -> bool:
    """不在 GB2312 里的汉字：繁体、生僻字（含扩展区）。"""
    if not is_cjk(ch):
        return False
    try:
        ch.encode("gb2312")
        return False
    except UnicodeEncodeError:
        return True


def diff_chars(a: str, b: str) -> list[str]:
    """两段文字对齐后不一致的字符（两边各自的），用于统计"不一致里有多少是生僻 / 繁体字"。"""
    import difflib

    x, y = squash(a), squash(b)
    out: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        if op != "equal":
            out.extend(x[i1:i2])
            out.extend(y[j1:j2])
    return out


def _self_check() -> None:
    import random

    rng = random.Random(7)
    alphabet = "abc中文字 "
    for _ in range(300):
        a = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        b = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30)))
        assert levenshtein(a, b) == _dp(a, b), (a, b)


if __name__ == "__main__":
    _self_check()
    print("levenshtein ok")
