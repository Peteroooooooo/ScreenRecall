"""文字检索（P0 词法分支，方案 5.2）。

规则：
- 成对 "…" 或 “…” 内是必须包含的 OCR 原串，多段之间 AND；引号外文字按空白切成排序片段：
  有引号时只影响排序（未命中片段的排在后面），没有引号时各片段 OR 召回；
- 默认忽略大小写（只作用于检索副本），可选区分大小写；空白、数字和符号按字面处理，% 和 _ 不是通配符；
- 不少于 3 个字符的片段用 FTS5 trigram 生成候选，更短的用参数化 instr() 扫描；最终以 Python 子串核验为准；
- 排序：只有引号条件时按最新匹配时间；片段都够长时按 bm25；含短片段时按命中的不同片段数；
  同分依次按筛选内最新时间、帧 ID，保证重复查询结果顺序一致；
- 日期和应用筛选作用于出现记录；每个画面只占一个名次，展示筛选范围内最近一次出现。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import textutil

MIN_TRIGRAM_CHARS = 3
_QUOTES = {'"': '"', "\u201c": "\u201d"}
_STRAY_CLOSERS = {"\u201d"}
_UNBALANCED = "引号没有成对，请补全后再搜索。"


@dataclass(frozen=True)
class ParsedQuery:
    raw: str
    quoted: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.quoted and not self.terms


def parse_query(raw: str) -> ParsedQuery:
    quoted: list[str] = []
    outside: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch in _QUOTES:
            j = raw.find(_QUOTES[ch], i + 1)
            if j == -1:
                return ParsedQuery(raw, error=_UNBALANCED)
            if j > i + 1:
                quoted.append(raw[i + 1 : j])  # 引号内保持原样，包括空白
            outside.append(" ")
            i = j + 1
            continue
        if ch in _STRAY_CLOSERS:
            return ParsedQuery(raw, error=_UNBALANCED)
        outside.append(ch)
        i += 1
    terms = "".join(outside).split()
    return ParsedQuery(raw, tuple(dict.fromkeys(quoted)), tuple(dict.fromkeys(terms)))


@dataclass(frozen=True)
class Filters:
    start_ms: int | None = None  # UTC 半开区间 [start_ms, end_ms)
    end_ms: int | None = None
    apps: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()  # 预测类别（用户主动选择时才筛选；没有预测的画面不在筛选结果里）


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    session_id: str
    first_seen_ms: int
    last_seen_ms: int
    observations: int
    app: str | None
    window_title: str | None
    source_type: str = "live"  # live 自动采样 / manual 补拍


@dataclass
class SearchResult:
    frame_id: str
    image_path: str
    image_status: str
    width: int
    height: int
    latest: Occurrence
    occurrences_in_filter: int
    matched_terms: tuple[str, ...]
    matched_lines: list[int]
    snippet: list[str]
    bm25: float | None = None
    semantic_lines: list[int] = field(default_factory=list)  # 贡献最高的文本块所在 OCR 行
    score: float | None = None  # RRF 或余弦分数，只用于排序，不是正确率
    sources: tuple[str, ...] = ()  # 命中的分支：词法 / 语义 / 画面


@dataclass
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    total: int = 0
    rank_basis: str = ""  # 'time' | 'bm25' | 'terms'
    message: str | None = None
    # 严格规则下引号原串没有匹配：页面据此提供"放宽匹配"按钮，只有用户点击才放宽（方案 5.2）
    can_relax: bool = False
    relaxed: bool = False


RELAX_RULE = "放宽匹配：忽略大小写和全部空白（含 OCR 行之间的换行），所有引号原串仍须同时包含。放宽可能改变代码含义，请回原图确认。"
_NO_QUOTED = "没有画面同时包含全部引号原串。"
_RELAX_EMPTY = "引号原串去掉空白后为空，无法放宽匹配。"


def _relax_key(s: str) -> str:
    return textutil.squeeze(textutil.fold(s))[0]


def _relaxed_rows(conn: sqlite3.Connection, frame_ids: set[str]) -> list[sqlite3.Row]:
    """放宽时从基础筛选后的全部文字重新扫描，不沿用严格匹配得到的空集合。"""
    return conn.execute(
        "SELECT ft.frame_id, ft.text, ft.text_fold, f.image_path, f.image_status, f.width, f.height "
        "FROM frame_text ft JOIN frames f ON f.frame_id = ft.frame_id "
        "WHERE ft.frame_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(frame_ids)),),
    ).fetchall()


def _norm(s: str, case_sensitive: bool) -> str:
    return s if case_sensitive else textutil.fold(s)


def _fts_phrase(s: str) -> str:
    """FTS5 字符串字面量：整体作为短语，内部只有双引号需要转义。"""
    return '"' + s.replace('"', '""') + '"'


def _candidate_ids(conn: sqlite3.Connection, needle: str, case_sensitive: bool) -> set[str]:
    """包含 needle 的候选帧（超集，随后核验）。FTS 与扫描都在等长小写副本上进行。"""
    key = textutil.fold(needle)
    if len(key) >= MIN_TRIGRAM_CHARS:
        rows = conn.execute(
            "SELECT ft.frame_id FROM ocr_fts JOIN frame_text ft ON ft.text_id = ocr_fts.rowid "
            "WHERE ocr_fts MATCH ?",
            (_fts_phrase(key),),
        )
    elif case_sensitive:
        rows = conn.execute("SELECT frame_id FROM frame_text WHERE instr(text, ?) > 0", (needle,))
    else:
        rows = conn.execute("SELECT frame_id FROM frame_text WHERE instr(text_fold, ?) > 0", (key,))
    return {r[0] for r in rows}


def _qualifying_occurrences(
    conn: sqlite3.Connection, frame_ids: set[str] | None, filters: Filters
) -> dict[str, list[Occurrence]]:
    """筛选范围内的出现记录，按帧分组、最近的在前。frame_ids 为 None 时不限帧（供向量分支取全部候选）。"""
    sql = (
        "SELECT o.occurrence_id, o.frame_id, o.session_id, o.first_seen_ms, o.last_seen_ms, o.observations, "
        "o.app, o.window_title, o.source_type FROM occurrences o JOIN frames f ON f.frame_id = o.frame_id "
        "WHERE f.image_status != 'deleted'"
    )
    params: list = []
    if frame_ids is not None:
        sql += " AND o.frame_id IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(sorted(frame_ids)))
    if filters.start_ms is not None:
        sql += " AND o.last_seen_ms >= ?"
        params.append(filters.start_ms)
    if filters.end_ms is not None:
        sql += " AND o.first_seen_ms < ?"
        params.append(filters.end_ms)
    if filters.apps:
        sql += " AND o.app IN (SELECT value FROM json_each(?))"
        params.append(json.dumps(list(filters.apps)))
    if filters.labels:
        sql += (" AND o.frame_id IN (SELECT frame_id FROM frame_labels "
                "WHERE label IN (SELECT value FROM json_each(?)))")
        params.append(json.dumps(list(filters.labels)))
    sql += " ORDER BY o.last_seen_ms DESC, o.occurrence_id"
    out: dict[str, list[Occurrence]] = {}
    for r in conn.execute(sql, params):
        out.setdefault(r["frame_id"], []).append(
            Occurrence(
                r["occurrence_id"], r["session_id"], r["first_seen_ms"], r["last_seen_ms"],
                r["observations"], r["app"], r["window_title"], r["source_type"],
            )
        )
    return out


def _bm25(conn: sqlite3.Connection, terms: tuple[str, ...]) -> dict[str, float]:
    query = " OR ".join(_fts_phrase(textutil.fold(t)) for t in terms)
    rows = conn.execute(
        "SELECT ft.frame_id, bm25(ocr_fts) FROM ocr_fts JOIN frame_text ft ON ft.text_id = ocr_fts.rowid "
        "WHERE ocr_fts MATCH ?",
        (query,),
    )
    return {r[0]: float(r[1]) for r in rows}


def match_lines(
    text: str, text_fold: str, line_spans: list[tuple[int, int]], needles: tuple[str, ...], case_sensitive: bool,
    relaxed: bool = False,
) -> list[int]:
    """needles 在本帧 OCR 全文中命中的行号，用于摘要与高亮。放宽时命中可能跨行，相关各行都会标出。"""
    spans: list[tuple[int, int]] = []
    if relaxed:
        for n in needles:
            spans.extend(textutil.find_all_squeezed(text_fold, textutil.fold(n)))
        return textutil.lines_for_spans(spans, line_spans)
    hay = text if case_sensitive else text_fold
    for n in needles:
        spans.extend(textutil.find_all(hay, _norm(n, case_sensitive)))
    return textutil.lines_for_spans(spans, line_spans)


def required_frames(
    conn: sqlite3.Connection, parsed: ParsedQuery, case_sensitive: bool, *,
    relaxed: bool = False, within: set[str] | None = None,
) -> set[str] | None:
    """全部引号原串（AND）核验通过的帧；没有引号时返回 None（不限制）。

    relaxed=True 时在 within（基础筛选后的帧，None 表示全部）的 OCR 全文中重新扫描。
    """
    if not parsed.quoted:
        return None
    if relaxed:
        keys = [_relax_key(q) for q in parsed.quoted]
        if not all(keys):
            return set()
        if within is None:
            within = {r[0] for r in conn.execute("SELECT frame_id FROM frame_text")}
        return {
            r["frame_id"] for r in _relaxed_rows(conn, within)
            if all(k in textutil.squeeze(r["text_fold"])[0] for k in keys)
        }
    required: set[str] | None = None
    for q in parsed.quoted:
        ids = _candidate_ids(conn, q, case_sensitive)
        required = ids if required is None else required & ids
        if not required:
            return set()
    rows = conn.execute(
        "SELECT frame_id, text, text_fold FROM frame_text WHERE frame_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(required)),),
    )
    out = set()
    for r in rows:
        hay = r["text"] if case_sensitive else r["text_fold"]
        if all(_norm(q, case_sensitive) in hay for q in parsed.quoted):
            out.add(r["frame_id"])
    return out


def _strict_verify(conn, parsed: ParsedQuery, filters: Filters, case_sensitive: bool):
    """严格规则：返回 (verified, occs)，或说明为何为空的 SearchResponse。"""
    # 1. 引号硬过滤（AND）
    required: set[str] | None = None
    for q in parsed.quoted:
        ids = _candidate_ids(conn, q, case_sensitive)
        required = ids if required is None else required & ids
        if not required:
            return SearchResponse(message=_NO_QUOTED)

    # 2. 候选集合：有引号时引号条件决定集合（引号外文字只用于排序）；
    #    没有引号时由各片段 OR 召回
    if required is not None:
        pool = set(required)
    else:
        pool = set()
        for t in parsed.terms:
            pool |= _candidate_ids(conn, t, case_sensitive)
    if not pool:
        return SearchResponse(message="没有找到匹配的记录。")

    # 3. 显式筛选作用于出现记录
    occs = _qualifying_occurrences(conn, pool, filters)
    pool = {fid for fid in pool if fid in occs}
    if not pool:
        return SearchResponse(message="有匹配的文字，但不在当前日期或应用筛选范围内。")

    # 4. 以子串核验为准
    rows = conn.execute(
        "SELECT ft.frame_id, ft.text, ft.text_fold, f.image_path, f.image_status, f.width, f.height "
        "FROM frame_text ft JOIN frames f ON f.frame_id = ft.frame_id "
        "WHERE ft.frame_id IN (SELECT value FROM json_each(?))",
        (json.dumps(sorted(pool)),),
    ).fetchall()
    verified: dict[str, tuple[sqlite3.Row, tuple[str, ...]]] = {}
    for r in rows:
        hay = r["text"] if case_sensitive else r["text_fold"]
        if any(_norm(q, case_sensitive) not in hay for q in parsed.quoted):
            continue
        hits = tuple(t for t in parsed.terms if _norm(t, case_sensitive) in hay)
        if not parsed.quoted and not hits:
            continue
        verified[r["frame_id"]] = (r, hits)
    if not verified:
        return SearchResponse(message="没有找到匹配的记录。")
    return verified, occs


def _relaxed_verify(conn, parsed: ParsedQuery, filters: Filters):
    """放宽规则：从日期 / 应用筛选后的全部文字重新扫描，忽略大小写与空白，引号原串仍为 AND。"""
    occs = _qualifying_occurrences(conn, None, filters)
    if not occs:
        return SearchResponse(message="当前日期或应用筛选范围内没有记录。", relaxed=True)
    keys = [_relax_key(q) for q in parsed.quoted]
    if not all(keys):
        return SearchResponse(message=_RELAX_EMPTY, relaxed=True)
    term_keys = [(t, _relax_key(t)) for t in parsed.terms]
    verified: dict[str, tuple[sqlite3.Row, tuple[str, ...]]] = {}
    for r in _relaxed_rows(conn, set(occs)):
        hay = textutil.squeeze(r["text_fold"])[0]
        if any(k not in hay for k in keys):
            continue
        hits = tuple(t for t, k in term_keys if k and k in hay)
        if not parsed.quoted and not hits:
            continue
        verified[r["frame_id"]] = (r, hits)
    if not verified:
        return SearchResponse(message="放宽匹配后仍没有符合条件的记录。", relaxed=True)
    return verified, occs


def search(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    filters: Filters = Filters(),
    *,
    case_sensitive: bool = False,
    limit: int = 10,
    relaxed: bool = False,
) -> SearchResponse:
    if parsed.error:
        return SearchResponse(message=parsed.error)
    if parsed.is_empty:
        return SearchResponse(message="请输入要查找的文字。")

    if relaxed:
        found = _relaxed_verify(conn, parsed, filters)
        if isinstance(found, SearchResponse):
            return found
        verified, occs = found
    else:
        found = _strict_verify(conn, parsed, filters, case_sensitive)
        if isinstance(found, SearchResponse):
            found.can_relax = bool(parsed.quoted)
            return found
        verified, occs = found

    # 5. 排序
    def latest(fid: str) -> int:
        return occs[fid][0].last_seen_ms

    scores: dict[str, float] = {}
    if not parsed.terms:
        basis = "time"
        order = sorted(verified, key=lambda f: (-latest(f), f))
    elif not relaxed and all(len(t) >= MIN_TRIGRAM_CHARS for t in parsed.terms):
        basis = "bm25"
        scores = _bm25(conn, parsed.terms)
        # bm25 越小越相关；没有命中任何片段的（仅满足引号条件）排在最后
        order = sorted(
            verified,
            key=lambda f: (not verified[f][1], scores.get(f, 0.0) if verified[f][1] else 0.0, -latest(f), f),
        )
    else:
        basis = "terms"
        order = sorted(verified, key=lambda f: (-len(verified[f][1]), -latest(f), f))
    top = order[:limit]

    # 6. 组装：命中行与摘要
    lines_by_frame: dict[str, list[sqlite3.Row]] = {}
    for lr in conn.execute(
        "SELECT frame_id, line_no, text, char_start, char_end FROM ocr_lines "
        "WHERE frame_id IN (SELECT value FROM json_each(?)) ORDER BY frame_id, line_no",
        (json.dumps(top),),
    ):
        lines_by_frame.setdefault(lr["frame_id"], []).append(lr)

    results: list[SearchResult] = []
    for fid in top:
        r, hits = verified[fid]
        lines = lines_by_frame.get(fid, [])
        spans = [(lr["char_start"], lr["char_end"]) for lr in lines]
        hit_lines = match_lines(r["text"], r["text_fold"], spans, parsed.quoted + hits, case_sensitive, relaxed)
        snippet = [lines[i]["text"] for i in hit_lines[:3]] if hit_lines else [lr["text"] for lr in lines[:1]]
        results.append(
            SearchResult(
                frame_id=fid,
                image_path=r["image_path"],
                image_status=r["image_status"],
                width=r["width"],
                height=r["height"],
                latest=occs[fid][0],
                occurrences_in_filter=len(occs[fid]),
                matched_terms=hits,
                matched_lines=hit_lines,
                snippet=snippet,
                bm25=scores.get(fid) if basis == "bm25" else None,
            )
        )
    return SearchResponse(results=results, total=len(verified), rank_basis=basis, relaxed=relaxed)


@dataclass
class FrameDetail:
    frame_id: str
    image_path: str
    image_status: str
    ocr_status: str
    width: int
    height: int
    captured_at_ms: int
    capture_meta: dict
    text: str
    text_fold: str
    lines: list[dict]
    occurrences: list[Occurrence]


def frame_detail(conn: sqlite3.Connection, frame_id: str) -> FrameDetail | None:
    f = conn.execute("SELECT * FROM frames WHERE frame_id = ?", (frame_id,)).fetchone()
    if f is None or f["image_status"] == "deleted":
        return None
    t = conn.execute("SELECT text, text_fold FROM frame_text WHERE frame_id = ?", (frame_id,)).fetchone()
    lines = [
        {
            "line_no": r["line_no"],
            "text": r["text"],
            "confidence": r["confidence"],
            "quad": json.loads(r["quad"]),
            "span": (r["char_start"], r["char_end"]),
        }
        for r in conn.execute(
            "SELECT line_no, text, confidence, quad, char_start, char_end FROM ocr_lines "
            "WHERE frame_id = ? ORDER BY line_no",
            (frame_id,),
        )
    ]
    occurrences = [
        Occurrence(
            r["occurrence_id"], r["session_id"], r["first_seen_ms"], r["last_seen_ms"],
            r["observations"], r["app"], r["window_title"], r["source_type"],
        )
        for r in conn.execute(
            "SELECT * FROM occurrences WHERE frame_id = ? ORDER BY first_seen_ms DESC", (frame_id,)
        )
    ]
    return FrameDetail(
        frame_id=frame_id,
        image_path=f["image_path"],
        image_status=f["image_status"],
        ocr_status=f["ocr_status"],
        width=f["width"],
        height=f["height"],
        captured_at_ms=f["captured_at_ms"],
        capture_meta=json.loads(f["capture_meta"] or "{}"),
        text=t["text"] if t else "",
        text_fold=t["text_fold"] if t else "",
        lines=lines,
        occurrences=occurrences,
    )


def known_apps(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT app FROM occurrences WHERE app IS NOT NULL ORDER BY app")]
