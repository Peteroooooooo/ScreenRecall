"""词法检索的查询规划：把用户输入切成词、决定每个词走哪张全文表，再给出 SQL 片段（v3 的 Queries.search 用）。

查询规则：
- 成对 "…" 或 “…” 内是一个整体短语，引号外按空白切成多个词；各词之间 AND（同一行内）；
- 每个词先 norm()（NFKC + casefold + 去空白），匹配语义是"行文字（去空白后）包含该词"；
- 不少于 3 个字的词：lines_tri 三元组出候选；
  1-2 个字的词：lines_bi 出候选（中日韩两字 = 二元组词元，单字 = 前缀 "字"*；
  纯字母数字 = 以它开头的词，例如 "ai" 能搜到 AI、AIGC，但搜不到 rain；
  中日韩字 + 字母数字混合（"1号"）= 普通子串，"21号" 也算）；
  有三字以上的词时只用三元组出候选，短词只做核验；
- 所有候选最后都用 SQL 里的 instr() 核验，结果是精确的。
v3 的索引文档 tail 一律为空（每行只索引自己），所以核验只看本行 norm。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import docs

MIN_TRIGRAM = 3
MAX_TRIGRAMS_PER_TERM = 24
_QUOTES = {'"': '"', "“": "”"}


@dataclass(frozen=True)
class Term:
    text: str  # norm 后
    kind: str  # 'tri' | 'short'
    fts: tuple[str, ...]  # FTS5 查询片段（已加引号）
    word_prefix: bool = False  # 短词里有字母数字：语义是"以它开头的词"，instr 核验不够，还要查 lines_bi


@dataclass(frozen=True)
class Plan:
    raw: str
    terms: tuple[Term, ...] = ()
    dropped: tuple[str, ...] = ()  # 没法检索的短词（只有标点）
    error: str | None = None


def _quote(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def split_query(raw: str) -> tuple[list[str], str | None]:
    """引号内为整体短语，引号外按空白切分。返回 (片段, 错误)。"""
    parts: list[str] = []
    outside: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch in _QUOTES:
            j = raw.find(_QUOTES[ch], i + 1)
            if j == -1:
                return [], "引号没有成对，请补全后再搜索。"
            parts.append(raw[i + 1:j])
            outside.append(" ")
            i = j + 1
            continue
        outside.append(ch)
        i += 1
    parts.extend("".join(outside).split())
    return parts, None


def _trigrams(t: str) -> list[str]:
    grams = list(dict.fromkeys(t[i:i + 3] for i in range(len(t) - 2)))
    if len(grams) <= MAX_TRIGRAMS_PER_TERM:
        return grams
    # 很长的词只取均匀分布的一部分（含首尾）出候选，核验仍用整个词
    step = (len(grams) - 1) / (MAX_TRIGRAMS_PER_TERM - 1)
    return list(dict.fromkeys(grams[round(i * step)] for i in range(MAX_TRIGRAMS_PER_TERM)))


def _short_tokens(t: str) -> list[str]:
    """1-2 个字的词在 lines_bi 里的查询词元。

    含中日韩字时只用中日韩词元出候选（instr 核验是精确的子串匹配）：
    混合词如 "1号" 里的 "1" 若按"以它开头的词"查，就搜不到 "21号"。
    """
    toks: list[str] = []
    for run in docs.cjk_runs(t):
        if len(run) >= 2:
            toks.extend(_quote(run[i:i + 2]) for i in range(len(run) - 1))
        else:
            toks.append(_quote(run) + "*")
    if not toks:
        toks.extend(_quote(w) + "*" for w in docs.words(t))
    return list(dict.fromkeys(toks))


def plan(raw: str) -> Plan:
    parts, err = split_query(raw)
    if err:
        return Plan(raw, error=err)
    terms: list[Term] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for p in parts:
        t = docs.norm(p)
        if not t or t in seen:
            continue
        seen.add(t)
        if len(t) >= MIN_TRIGRAM:
            terms.append(Term(t, "tri", tuple(_quote(g) for g in _trigrams(t))))
        else:
            toks = _short_tokens(t)
            if toks:
                word_prefix = not docs.cjk_runs(t) and bool(docs.words(t))
                terms.append(Term(t, "short", tuple(toks), word_prefix))
            else:
                dropped.append(p)
    return Plan(raw, tuple(terms), tuple(dropped))


# ---------------- SQL 片段（参数写进 params，名字带前缀免得和调用方撞） ----------------

def candidates_sql(terms: Sequence[Term], params: dict) -> str:
    """出候选的子查询（一列 id = line_id）：有三字以上的词只用 lines_tri，否则 lines_bi。"""
    tri = [t for t in terms if t.kind == "tri"]
    params["lx_m"] = " AND ".join(q for t in (tri or terms) for q in t.fts)
    table = "lines_tri" if tri else "lines_bi"
    return f"SELECT rowid AS id FROM {table} WHERE {table} MATCH :lx_m"


def verify_sql(terms: Sequence[Term], params: dict, col: str = "l.norm") -> str:
    """每个词都要是 col 的子串（精确核验）。"""
    clauses = []
    for i, term in enumerate(terms):
        params[f"lx_t{i}"] = term.text
        clauses.append(f"instr({col}, :lx_t{i}) > 0")
    return " AND ".join(clauses) if clauses else "1"


def word_prefix_sql(terms: Sequence[Term], params: dict, id_col: str = "l.line_id") -> str:
    """短的字母数字词（"以它开头的词"）不能只靠 instr 核验：候选来自 lines_tri 时额外要求在 lines_bi 里命中。
    候选本身来自 lines_bi（全是短词）时已经满足，返回空串。"""
    if not any(t.kind == "tri" for t in terms):
        return ""
    toks = [q for t in terms if t.word_prefix for q in t.fts]
    if not toks:
        return ""
    params["lx_mw"] = " AND ".join(toks)
    return f" AND {id_col} IN (SELECT rowid FROM lines_bi WHERE lines_bi MATCH :lx_mw)"
