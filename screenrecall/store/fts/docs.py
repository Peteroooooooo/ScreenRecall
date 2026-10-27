"""文字规范化与全文索引文档（规则版本 schema.FTS_VERSION）。

- norm(text)：NFKC + casefold + 去掉全部空白。行匹配（同一行是否又出现）和检索都用它，
  所以 "微 信"/"微信"、全角/半角、大小写都视为相同；显示一律用原文 text；
- 跨行：每行的检索文档 = norm + tail，tail 是同一块里下一行 norm 的前 TAIL_CHARS 个字。
  这样从行尾折到下一行开头的短语也能搜到（最多延伸到下一行 TAIL_CHARS 个字）；
  只落在 tail 里的命中不算本行（由检索端核验排除，避免重复计数）；
- lines_tri：文档 = norm + tail，三元组、detail=none，只用来出候选；
- lines_bi：程序生成的词元，空格分隔交给 ascii 分词器：
  中日韩连续段的相邻两字（"微信号" → 微信 信号）+ 段尾单字（号），使 1 个字可用前缀查询 "号"*；
  其余字母数字按原文空白和标点切成词（小写）。
- 两张表都是普通 contentless 表，删除必须提供与写入时一模一样的文档（delete 命令），
  因此文档只能由行里存着的 text / norm / tail 确定性地算出来。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from typing import Iterable, Sequence

TAIL_CHARS = 16


def norm(text: str) -> str:
    """NFKC + casefold + 去掉全部空白。"""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return "".join(folded.split())


def is_cjk(ch: str) -> bool:
    """中日韩表意文字、假名、谚文、注音（按码位范围判断，不依赖分类表）。"""
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0x3040 <= o <= 0x30FF
        or 0x3100 <= o <= 0x312F
        or 0x31F0 <= o <= 0x31FF
        or 0xAC00 <= o <= 0xD7AF
        or 0x1100 <= o <= 0x11FF
        or 0xF900 <= o <= 0xFAFF
        or 0x20000 <= o <= 0x3FFFF
    )


def cjk_runs(s: str) -> list[str]:
    runs: list[str] = []
    cur: list[str] = []
    for ch in s:
        if is_cjk(ch):
            cur.append(ch)
        elif cur:
            runs.append("".join(cur))
            cur = []
    if cur:
        runs.append("".join(cur))
    return runs


def words(s: str) -> list[str]:
    """非中日韩的字母数字连续段（s 应已 NFKC + casefold）。"""
    out: list[str] = []
    cur: list[str] = []
    for ch in s:
        if ch.isalnum() and not is_cjk(ch):
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def run_tokens(run: str) -> list[str]:
    """一个中日韩连续段的词元：相邻两字 + 段尾单字。"""
    toks = [run[i:i + 2] for i in range(len(run) - 1)]
    toks.append(run[-1])
    return toks


def tri_doc(norm_text: str, tail: str) -> str:
    return norm_text + tail


def bi_doc(text: str, norm_text: str, tail: str) -> str:
    toks: list[str] = []
    for run in cjk_runs(norm_text + tail):
        toks.extend(run_tokens(run))
    toks.extend(words(unicodedata.normalize("NFKC", text).casefold()))
    return " ".join(dict.fromkeys(toks))


def tail_of(next_norm: str | None) -> str:
    return (next_norm or "")[:TAIL_CHARS]


# ---------------- 索引维护（须在调用方事务内） ----------------

Row = tuple[int, str, str, str]  # (line_id, text, norm, tail)


def index_rows(conn: sqlite3.Connection, rows: Iterable[Row]) -> None:
    rows = list(rows)
    conn.executemany("INSERT INTO lines_tri (rowid, k) VALUES (?, ?)",
                     [(lid, tri_doc(n, t)) for lid, _, n, t in rows])
    conn.executemany("INSERT INTO lines_bi (rowid, k) VALUES (?, ?)",
                     [(lid, bi_doc(x, n, t)) for lid, x, n, t in rows])


def unindex_rows(conn: sqlite3.Connection, rows: Iterable[Row]) -> None:
    """按原文档删除索引项（secure-delete 已开，词元会从索引段里真正抹掉）。"""
    rows = list(rows)
    conn.executemany("INSERT INTO lines_tri (lines_tri, rowid, k) VALUES ('delete', ?, ?)",
                     [(lid, tri_doc(n, t)) for lid, _, n, t in rows])
    conn.executemany("INSERT INTO lines_bi (lines_bi, rowid, k) VALUES ('delete', ?, ?)",
                     [(lid, bi_doc(x, n, t)) for lid, x, n, t in rows])


def set_tails(conn: sqlite3.Connection, changes: Sequence[tuple[int, str, int | None]]) -> None:
    """更新若干行的 tail：先按旧文档删索引，再写新 tail 与新文档。changes = [(line_id, tail, tail_line_id)]。"""
    if not changes:
        return
    old: list[Row] = []
    new: list[Row] = []
    for line_id, tail, tail_line in changes:
        row = conn.execute("SELECT text, norm, tail FROM lines WHERE line_id = ?", (line_id,)).fetchone()
        if row is None:
            continue
        if row[2] == tail:
            conn.execute("UPDATE lines SET tail_line_id = ? WHERE line_id = ?", (tail_line, line_id))
            continue
        old.append((line_id, row[0], row[1], row[2]))
        new.append((line_id, row[0], row[1], tail))
        conn.execute("UPDATE lines SET tail = ?, tail_line_id = ? WHERE line_id = ?", (tail, tail_line, line_id))
    unindex_rows(conn, old)
    index_rows(conn, new)
