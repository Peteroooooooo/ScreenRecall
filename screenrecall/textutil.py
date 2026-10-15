"""OCR 文本工具：等长大小写折叠、行偏移与命中行定位。"""

from __future__ import annotations


def fold(text: str) -> str:
    """等长小写：逐字符 lower()；小写后变长的字符（如 'İ'）保持原样，保证偏移一一对应。"""
    lowered = text.lower()
    if len(lowered) == len(text):
        return lowered
    out = []
    for ch in text:
        lo = ch.lower()
        out.append(lo if len(lo) == 1 else ch)
    return "".join(out)


def join_lines(texts: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """各 OCR 行按行序以换行连接，返回全文与每行的 [start, end) 字符区间。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    for t in texts:
        spans.append((pos, pos + len(t)))
        pos += len(t) + 1
    return "\n".join(texts), spans


def find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    """needle 在 haystack 中所有出现的 [start, end)，允许重叠。"""
    if not needle:
        return []
    out: list[tuple[int, int]] = []
    start = haystack.find(needle)
    while start != -1:
        out.append((start, start + len(needle)))
        start = haystack.find(needle, start + 1)
    return out


def lines_for_spans(spans: list[tuple[int, int]], line_spans: list[tuple[int, int]]) -> list[int]:
    """与任一命中区间重叠的行号（升序）。跨换行的命中会覆盖多行。"""
    hit: set[int] = set()
    for s, e in spans:
        for idx, (ls, le) in enumerate(line_spans):
            if ls < e and s < le:
                hit.add(idx)
    return sorted(hit)


def squeeze(text: str) -> tuple[str, list[int]]:
    """"放宽匹配"用的检索副本：去掉全部空白（含 OCR 行之间的换行）。

    返回副本与位置映射：副本第 i 个字符来自原文第 pos[i] 个字符，便于把命中换回原文区间、
    再关联 OCR 行；不能直接用副本偏移裁剪原文。
    """
    chars: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch)
            pos.append(i)
    return "".join(chars), pos


def find_all_squeezed(text: str, needle: str) -> list[tuple[int, int]]:
    """在去空白副本里查找（needle 同样去空白），返回原文中的 [start, end)。调用方负责大小写折叠。"""
    hay, pos = squeeze(text)
    key, _ = squeeze(needle)
    return [(pos[s], pos[e - 1] + 1) for s, e in find_all(hay, key)]
