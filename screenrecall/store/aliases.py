"""合并过的块里重复的行：标成"别名"（lines.alias_of = 留下显示的那一行），不删历史。

两个块合并（writer.block_merge）时，两边都看到过的那几行在库里各有一份。合并后按根块坐标比：
规范化文字相同、纵向相差不超过 match_dy_px（同一块里本来就按这个认同一行）、来自不同的块 → 后来的那份
（并入的块的）记 alias_of = 先有的那份（目标块的）。检索和阅读视图只显示 alias_of 为空的行；
被指向的那一行的存在区间扩到两份的并集（它代表两次看到）。

- 隐私删除照常按时间删行，两份各自判断；被指向的行删掉时，指向它的行的 alias_of 清空（store.delete），
  那份重新显示（它是在删除范围之外看到的）；
- 合并之后录制进程还会往并入的块里写新行：空闲整理进程每轮对有新行的合并树补一次（alias_new_lines）；
- 结构版本 3 的迁移对已有的合并树补一次（backfill）。
在写事务里调用（conn 已在事务内）。
"""

from __future__ import annotations

from typing import Callable, Iterable


def tree_offsets(conn, root: int) -> dict[int, int]:
    """根块 root 的整棵合并树：块 → 本块坐标到根块坐标的偏移。"""
    out = {int(root): 0}
    stack = [int(root)]
    while stack:
        b = stack.pop()
        for cid, off in conn.execute("SELECT block_id, offset_px FROM blocks WHERE merged_into = ?", (b,)):
            cid = int(cid)
            if cid not in out:
                out[cid] = out[b] + int(off)
                stack.append(cid)
    return out


def alias_tree(conn, offs: dict[int, int], tol: float, *, order: Callable[[tuple], tuple],
               may_alias: Callable[[tuple], bool]) -> int:
    """在一棵合并树（offs：块 → 到根块的偏移）里标别名。

    行按 order 排好依次看：may_alias 的行在不同块里找"已留下"的同一行（文字相同、根块坐标 y 差 <= tol，
    最近的那个，同一块的两行不会指向同一行），找到就记成它的别名；找不到（或不许当别名）的行留下，
    供后面的行匹配。返回新标的别名数。"""
    if len(offs) < 2:
        return 0
    ids = list(offs)
    rows = []
    for s in range(0, len(ids), 500):
        part = ids[s:s + 500]
        rows += [tuple(r) for r in conn.execute(
            "SELECT line_id, block_id, norm, y0, x0, first_seen_ms, last_seen_ms FROM lines "
            f"WHERE alias_of IS NULL AND block_id IN ({', '.join('?' * len(part))})", part)]
    canon: dict[str, list[tuple[float, float, int, int]]] = {}
    used: set[tuple[int, int]] = set()  # (留下的行, 别名所在的块)
    aliases: list[tuple[int, int]] = []
    spans: dict[int, list[int]] = {}
    for r in sorted(rows, key=order):
        lid, bid, norm, y0, x0, f, l = r
        y = float(y0) + offs[int(bid)]
        if may_alias(r):
            best = None
            for cy, cx, cid, cb in canon.get(norm, ()):
                if cb == bid or (cid, bid) in used or abs(cy - y) > tol:
                    continue
                key = (abs(cy - y), abs(cx - float(x0)))
                if best is None or key < best[0]:
                    best = (key, cid)
            if best is not None:
                cid = best[1]
                used.add((cid, bid))
                aliases.append((cid, lid))
                s = spans.setdefault(cid, [f, l])
                s[0], s[1] = min(s[0], f), max(s[1], l)
                continue
        canon.setdefault(norm, []).append((y, float(x0), lid, bid))
    if aliases:
        conn.executemany("UPDATE lines SET alias_of = ? WHERE line_id = ?", aliases)
        conn.executemany("UPDATE lines SET first_seen_ms = min(first_seen_ms, ?), last_seen_ms = max(last_seen_ms, ?) "
                         "WHERE line_id = ?", [(f, l, cid) for cid, (f, l) in spans.items()])
    return len(aliases)


def alias_merge(conn, src: int, tol: float) -> int:
    """src 已经并入别的块（合并已写入）之后：src 那棵树里与目标那棵树重复的行，记成目标那份的别名。"""
    src_blocks = set(tree_offsets(conn, src))
    offs = tree_offsets(conn, _root(conn, src))
    return alias_tree(conn, offs, tol, order=lambda r: (int(r[1]) in src_blocks, r[0]),
                      may_alias=lambda r: int(r[1]) in src_blocks)


def alias_new_lines(conn, segment_id: int, since_line_id: int, tol: float) -> int:
    """内容源里 line_id > since_line_id 的新行：所在的合并树（至少两个块）里与别的块重复的，记成别名。"""
    roots: set[int] = set()
    for (bid,) in conn.execute("SELECT DISTINCT block_id FROM lines WHERE segment_id = ? AND line_id > ? "
                               "AND alias_of IS NULL", (int(segment_id), int(since_line_id))):
        roots.add(_root(conn, int(bid)))
    n = 0
    for root in roots:
        offs = tree_offsets(conn, root)
        n += alias_tree(conn, offs, tol, order=lambda r: (r[0],), may_alias=lambda r: r[0] > since_line_id)
    return n


def backfill(conn, tol: float) -> int:
    """（迁移用）所有已有的合并树：先建的块的行先留下，后建的块里重复的记成别名。"""
    roots = [int(r[0]) for r in conn.execute(
        "SELECT DISTINCT b.block_id FROM blocks b WHERE b.merged_into IS NULL "
        "AND EXISTS (SELECT 1 FROM blocks c WHERE c.merged_into = b.block_id)")]
    n = 0
    for root in roots:
        offs = tree_offsets(conn, root)
        created = _created(conn, offs)
        n += alias_tree(conn, offs, tol, order=lambda r: (created.get(int(r[1]), 0), int(r[1]), r[0]),
                        may_alias=lambda r: True)
    return n


def _created(conn, blocks: Iterable[int]) -> dict[int, int]:
    ids = list(blocks)
    out: dict[int, int] = {}
    for s in range(0, len(ids), 500):
        part = ids[s:s + 500]
        out.update({int(b): int(c) for b, c in conn.execute(
            f"SELECT block_id, created_ms FROM blocks WHERE block_id IN ({', '.join('?' * len(part))})", part)})
    return out


def _root(conn, block_id: int) -> int:
    cur, seen = int(block_id), set()
    while cur not in seen:
        seen.add(cur)
        r = conn.execute("SELECT merged_into FROM blocks WHERE block_id = ?", (cur,)).fetchone()
        if r is None or r[0] is None:
            return cur
        cur = int(r[0])
    return cur
