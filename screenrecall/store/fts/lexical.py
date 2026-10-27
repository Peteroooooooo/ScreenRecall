"""词法检索：按内容源分组、按最近一次看到排序、条数限制都在 SQL 里做。

查询规则：
- 成对 "…" 或 “…” 内是一个整体短语，引号外按空白切成多个词；各词之间 AND（同一行内，含跨行 tail）；
- 每个词先 norm()（NFKC + casefold + 去空白），匹配语义是"行文字（去空白后）包含该词"，
  可以从行尾延伸到下一行开头（最多 docs.TAIL_CHARS 个字）；只落在下一行里的不算本行；
- 不少于 3 个字的词：lines_tri 三元组出候选；
  1-2 个字的词：lines_bi 出候选（中日韩两字 = 二元组词元，单字 = 前缀 "字"*；
  字母数字 = 以它开头的词，例如 "ai" 能搜到 AI、AIGC，但搜不到 rain）；
  有三字以上的词时只用三元组出候选，短词只做核验；
- 所有候选最后都用 SQL 里的 instr() 核验，结果是精确的；
- 排序 = 最近一次看到（last_seen_ms），不用 rowid（空闲 OCR 会晚插入，重看到的行保留旧 id）。

执行方式（exact / segment / recent）按估算代价选，见 search() 的说明。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
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


@dataclass
class Filters:
    time_from: int | None = None
    time_to: int | None = None
    app: str | None = None
    task: str | None = None
    exclude_apps: Sequence[str] = ()
    exclude_title_keywords: Sequence[str] = ()
    segment_ids: Sequence[int] | None = None


@dataclass
class Group:
    segment_id: int
    app: str
    title: str
    hits: int
    last_seen_ms: int
    lines: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"segment_id": self.segment_id, "app": self.app, "title": self.title, "hits": self.hits,
                "last_seen_ms": self.last_seen_ms, "lines": self.lines}


@dataclass
class Result:
    query: str
    terms: list[str]
    mode: str  # 'exact' | 'recent' | 'empty'
    total: int
    total_is_lower_bound: bool
    total_groups: int
    groups: list[Group]
    note: str | None = None

    def to_dict(self) -> dict:
        return {"query": self.query, "terms": self.terms, "mode": self.mode, "total": self.total,
                "total_is_lower_bound": self.total_is_lower_bound, "total_groups": self.total_groups,
                "groups": [g.to_dict() for g in self.groups], "note": self.note}


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
    toks: list[str] = []
    for run in docs.cjk_runs(t):
        if len(run) >= 2:
            toks.extend(_quote(run[i:i + 2]) for i in range(len(run) - 1))
        else:
            toks.append(_quote(run) + "*")
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
                terms.append(Term(t, "short", tuple(toks), bool(docs.words(t))))
            else:
                dropped.append(p)
    return Plan(raw, tuple(terms), tuple(dropped))


# ---------------- SQL 片段 ----------------

def _verify_sql(terms: Sequence[Term], params: dict) -> str:
    """每个词：本行包含，或跨到下一行（起点在本行、终点在 tail 里）。"""
    clauses = []
    for i, term in enumerate(terms):
        params[f"t{i}"] = term.text
        n1 = len(term.text) - 1
        if n1 >= 1:
            params[f"n{i}"] = n1
            clauses.append(
                f"(instr(l.norm, :t{i}) > 0 OR "
                f"instr(substr(l.norm, -:n{i}) || substr(l.tail, 1, :n{i}), :t{i}) > 0)"
            )
        else:
            clauses.append(f"instr(l.norm, :t{i}) > 0")
    return " AND ".join(clauses) if clauses else "1"


def _filter_sql(f: Filters, params: dict) -> str:
    clauses: list[str] = []
    if f.time_from is not None:
        params["time_from"] = int(f.time_from)
        clauses.append("l.last_seen_ms >= :time_from")
    if f.time_to is not None:
        params["time_to"] = int(f.time_to)
        clauses.append("l.first_seen_ms < :time_to")
    seg = segment_filter_sql(f, params)
    if seg:
        clauses.append(f"l.segment_id IN ({seg})")
    return " AND ".join(clauses) if clauses else "1"


def segment_filter_sql(f: Filters, params: dict) -> str | None:
    """内容源级筛选（应用、任务、隐藏名单）→ 子查询 SELECT segment_id ...；没有筛选时返回 None。"""
    seg: list[str] = []
    if f.app:
        params["app"] = f.app
        seg.append("s.app = :app COLLATE NOCASE")
    for i, a in enumerate(f.exclude_apps):
        params[f"xa{i}"] = a
        seg.append(f"s.app != :xa{i} COLLATE NOCASE")
    for i, kw in enumerate(f.exclude_title_keywords):
        if kw:
            params[f"xk{i}"] = kw.casefold()
            seg.append(f"instr(lower(s.title), :xk{i}) = 0")
    if f.task:
        params["task"] = f.task
        seg.append(
            "s.segment_id IN (SELECT ps.segment_id FROM piece_segments ps JOIN pieces p "
            "ON p.piece_id = ps.piece_id WHERE p.task_label = :task)"
        )
    if f.segment_ids is not None:
        params["seg_ids"] = ",".join(str(int(x)) for x in f.segment_ids)
        seg.append("s.segment_id IN (SELECT CAST(value AS INTEGER) FROM json_each('[' || :seg_ids || ']'))")
    if not seg:
        return None
    return "SELECT s.segment_id FROM segments s WHERE " + " AND ".join(seg)


_GROUPED = """
, ranked AS (
    SELECT line_id, segment_id, last_seen_ms,
           COUNT(*) OVER (PARTITION BY segment_id) AS seg_hits,
           MAX(last_seen_ms) OVER (PARTITION BY segment_id) AS seg_last,
           ROW_NUMBER() OVER (PARTITION BY segment_id ORDER BY last_seen_ms DESC, line_id DESC) AS rn,
           COUNT(*) OVER () AS total
    FROM hits
), g AS (
    SELECT *, DENSE_RANK() OVER (ORDER BY seg_last DESC, segment_id DESC) AS gr,
           SUM(rn = 1) OVER () AS total_groups
    FROM ranked WHERE rn <= :per_group
)
SELECT g.segment_id, g.seg_hits, g.seg_last, g.total, g.total_groups, g.gr,
       s.app, s.title,
       l.line_id, l.block_id, l.text, l.x0, l.y0, l.x1, l.y1, l.source, l.confidence,
       l.first_seen_ms, l.last_seen_ms, l.disappeared_ms
FROM g JOIN lines l ON l.line_id = g.line_id JOIN segments s ON s.segment_id = g.segment_id
WHERE g.gr <= :groups
ORDER BY g.gr, g.rn
"""


def _candidates_sql(terms: Sequence[Term], params: dict) -> str:
    tri = [t for t in terms if t.kind == "tri"]
    if tri:
        params["m"] = " AND ".join(q for t in tri for q in t.fts)
        return "SELECT rowid AS id FROM lines_tri WHERE lines_tri MATCH :m"
    params["m"] = " AND ".join(q for t in terms for q in t.fts)
    return "SELECT rowid AS id FROM lines_bi WHERE lines_bi MATCH :m"


def _word_prefix_sql(terms: Sequence[Term], params: dict) -> str:
    """短的字母数字词（"以它开头的词"）不能只靠 instr 核验：额外要求在 lines_bi 里命中。"""
    toks = [q for t in terms if t.word_prefix for q in t.fts]
    if not toks:
        return ""
    params["mw"] = " AND ".join(toks)
    return " AND l.line_id IN (SELECT rowid FROM lines_bi WHERE lines_bi MATCH :mw)"


def _collect(rows: list[sqlite3.Row]) -> tuple[int, int, list[Group]]:
    groups: dict[int, Group] = {}
    total = total_groups = 0
    for r in rows:
        total, total_groups = int(r["total"]), int(r["total_groups"])
        g = groups.get(r["segment_id"])
        if g is None:
            g = groups[r["segment_id"]] = Group(r["segment_id"], r["app"], r["title"], int(r["seg_hits"]),
                                                int(r["seg_last"]))
        g.lines.append({
            "line_id": r["line_id"], "block_id": r["block_id"], "text": r["text"],
            "rect": [r["x0"], r["y0"], r["x1"], r["y1"]], "source": r["source"],
            "confidence": r["confidence"], "first_seen_ms": r["first_seen_ms"],
            "last_seen_ms": r["last_seen_ms"], "disappeared_ms": r["disappeared_ms"],
        })
    return total, total_groups, list(groups.values())


# 代价模型（本机 1M 行库实测）：按 rowid 顺序取候选并核验约 2.5 µs/条；
# 沿 last_seen 索引或内容源索引逐行回表核验约 4.5 µs/行。只用来比较三种执行方式的相对代价
EXACT_US_PER_CANDIDATE = 2.5
SCAN_US_PER_ROW = 4.5
COUNT_CAP = 2_000_000


def _count(conn, sql: str, params: dict, cap: int = COUNT_CAP) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM ({sql} LIMIT {int(cap) + 1})", params).fetchone()[0])


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    filters: Filters | None = None,
    limit: int = 20,
    per_group: int = 3,
    scan_hits: int | None = None,
    scan_rows: int = 300_000,
    mode: str | None = None,
) -> Result:
    """limit = 返回的分组（内容源）数；per_group = 每组返回的最新命中行数。

    三种执行方式，按估算代价选（mode 可强制指定，测试用）：
    - exact：全文索引出全部候选 → 核验 → 分组计数（结果和计数都精确）；
    - segment：有应用/任务等内容源筛选且范围很小时，沿内容源索引逐行核验（精确）；
    - recent：常见词，沿 last_seen 倒序边扫边核验，凑够 scan_hits 条命中就停；
      分组顺序与每组最新命中精确，计数是下限。扫满 scan_rows 行还凑不出 limit 组时退回精确方式。
    """
    f = filters or Filters()
    p = plan(query)
    terms_txt = [t.text for t in p.terms]
    if p.error:
        return Result(query, [], "empty", 0, False, 0, [], p.error)
    note = f"已忽略只有符号的短词：{'、'.join(p.dropped)}" if p.dropped else None
    if not p.terms:
        return Result(query, [], "empty", 0, False, 0, [], note or "请输入要搜索的文字。")
    limit = max(1, int(limit))
    per_group = max(1, int(per_group))
    if scan_hits is None:
        scan_hits = max(200, limit * per_group * 10)

    params: dict = {"groups": limit, "per_group": per_group, "scan_hits": int(scan_hits),
                    "scan_rows": int(scan_rows)}
    cand = _candidates_sql(p.terms, params)
    verify = _verify_sql(p.terms, params)
    words_only_from_bi = not any(t.kind == "tri" for t in p.terms)  # 候选本身来自 lines_bi 时已满足
    word_sql = _word_prefix_sql(p.terms, params)
    verify_full = verify + word_sql
    where = _filter_sql(f, params)
    seg = segment_filter_sql(f, params)

    if mode is None:
        n_cand = _count(conn, cand, params)
        row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'lines'").fetchone()
        n_total = max(1, int(row[0]) if row else 1)
        seg_lines = (_count(conn, f"SELECT 1 FROM lines INDEXED BY idx_lines_segment WHERE segment_id IN ({seg})",
                            params) if seg else n_total)
        rate = (n_cand / n_total) * (seg_lines / n_total)
        costs = {
            "exact": n_cand * EXACT_US_PER_CANDIDATE,
            "recent": min(scan_hits / max(rate, 1e-12), n_total, scan_rows) * SCAN_US_PER_ROW,
        }
        if seg:
            costs["segment"] = seg_lines * SCAN_US_PER_ROW
        mode = min(costs, key=costs.get)

    if mode == "recent":
        # time_to 不能用来缩小扫描起点：last_seen 晚于 time_to 的行，first_seen 仍可能在范围内
        start = "WHERE last_seen_ms >= :time_from " if f.time_from is not None else ""
        # 扫描下界：最近第 scan_rows 行的 last_seen（只走索引），保证最多核验约 scan_rows 行
        thr = conn.execute(
            f"SELECT last_seen_ms FROM lines INDEXED BY idx_lines_last_seen {start}"
            "ORDER BY last_seen_ms DESC LIMIT 1 OFFSET :scan_rows", params,
        ).fetchone()
        params["thr"] = int(thr[0]) if thr else -(2 ** 62)
        sql = (
            "WITH hits AS (SELECT l.line_id, l.segment_id, l.last_seen_ms FROM lines l "
            "INDEXED BY idx_lines_last_seen "
            f"WHERE l.last_seen_ms >= :thr AND {verify_full} AND {where} "
            "ORDER BY l.last_seen_ms DESC LIMIT :scan_hits)" + _GROUPED
        )
        total, total_groups, groups = _collect(conn.execute(sql, params).fetchall())
        if len(groups) >= limit or total >= scan_hits or thr is None:
            # thr 为空 = 范围内的行不到 scan_rows，已经全部扫过，结果完整
            exact = thr is None and total < scan_hits
            return Result(query, terms_txt, "exact" if exact else "recent", total, not exact, total_groups,
                          groups, note)
        mode = "segment" if seg else "exact"  # 筛选很窄：最近 scan_rows 行里凑不出 limit 组

    if mode == "segment" and seg:
        time_where = _filter_sql(Filters(f.time_from, f.time_to), params)
        sql = (
            "WITH hits AS (SELECT l.line_id, l.segment_id, l.last_seen_ms FROM lines l INDEXED BY idx_lines_segment "
            f"WHERE l.segment_id IN ({seg}) AND {verify_full} AND {time_where})" + _GROUPED
        )
    else:
        mode = "exact"
        sql = (
            f"WITH cand AS ({cand}), "
            "hits AS (SELECT l.line_id, l.segment_id, l.last_seen_ms FROM cand JOIN lines l ON l.line_id = cand.id "
            f"WHERE {verify if words_only_from_bi else verify_full} AND {where})" + _GROUPED
        )
    total, total_groups, groups = _collect(conn.execute(sql, params).fetchall())
    return Result(query, terms_txt, mode, total, False, total_groups, groups, note)
