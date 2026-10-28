"""文字规范化与全文索引文档（规则版本 schema.FTS_VERSION）。

- norm(text)：NFKC + casefold + 去掉全部空白。行匹配（同一行是否又出现）和检索都用它，
  所以 "微 信"/"微信"、全角/半角、大小写都视为相同；显示一律用原文 text；
- 文档里的 tail 是 v2 跨行检索留下的参数，v3 一律传 ""（每行只按自己的字建索引）；
- lines_tri：文档 = norm + tail，三元组、detail=none，只用来出候选；
- lines_bi：程序生成的词元，空格分隔交给 ascii 分词器：
  中日韩连续段的相邻两字（"微信号" → 微信 信号）+ 段尾单字（号），使 1 个字可用前缀查询 "号"*；
  其余字母数字按原文空白和标点切成词（小写）。
- 两张表都是普通 contentless 表，删除必须提供与写入时一模一样的文档（delete 命令），
  因此文档只能由行里存着的 text / norm 确定性地算出来。
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Iterable


def fold(text: str) -> str:
    """NFKC + casefold（保留空白）：应用名、标题等按"大小写/全半角不敏感"比较时用。"""
    return unicodedata.normalize("NFKC", text).casefold()


def norm(text: str) -> str:
    """NFKC + casefold + 去掉全部空白。"""
    return "".join(fold(text).split())


# 中日韩表意文字、假名、谚文、注音的码位范围（含两端）
_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x3040, 0x30FF), (0x3100, 0x312F), (0x31F0, 0x31FF),
    (0xAC00, 0xD7AF), (0x1100, 0x11FF), (0xF900, 0xFAFF), (0x20000, 0x3FFFF),
)
_CJK_RUN = re.compile("[" + "".join(f"{re.escape(chr(lo))}-{re.escape(chr(hi))}" for lo, hi in _CJK_RANGES) + "]+")
_WORD = re.compile(r"[^\W_]+")


def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _CJK_RANGES)


def cjk_runs(s: str) -> list[str]:
    """中日韩连续段（与 is_cjk 的码位范围一致）。"""
    return _CJK_RUN.findall(s)


def words(s: str) -> list[str]:
    """非中日韩的字母数字连续段（s 应已 NFKC + casefold）。"""
    return _WORD.findall(_CJK_RUN.sub(" ", s))


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
