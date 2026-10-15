"""两种搜索模式：文字（词法 + 文本语义，固定等权 RRF）与画面（多语言 CLIP 描述—全图）。

规则（提案 4.3–4.5；方案 5.2、5.3）：
- 先应用日期/应用筛选（作用于出现记录）与全部引号原串（AND），得到共同候选集合，各分支只在其中排名；
- 只有引号原串时按捕获时间展示，不调用向量模型（沿用词法实现）；
- 文字模式：词法分支取有片段命中的帧；语义分支对候选帧的文本块求相似度，每帧取最高块，
  合并到帧后各取 Top-50，再做 k=60 的两路等权 RRF；同分按筛选内最新时间、再按帧 ID；
- 画面模式：只检索原图可用且已有全图向量的帧，按描述与整图的余弦相似度排序；
- 向量未完成的帧不视为不相关：照常参与词法分支，并在结果里说明索引覆盖；
- 分数不是正确率；首期不设"无答案"阈值。

向量矩阵从 SQLite 读入 NumPy，按 meta.index_revision 判断是否需要重新加载（方案 3.6）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np

from . import db, models, search, vectors
from .search import Filters, Occurrence, ParsedQuery, SearchResponse, SearchResult

TEXT = "text"
IMAGE = "image"


# ---------------- 向量矩阵 ----------------

class TextVectorIndex:
    """文本块向量矩阵 + 帧-块关联。线程安全：查询前按修订号刷新。"""

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.revision = -1
        self._lock = threading.Lock()
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.pair_frame: np.ndarray = np.zeros(0, dtype=object)  # 每个 (帧, 块) 对的帧 ID
        self.pair_row: np.ndarray = np.zeros(0, dtype=np.int64)  # 对应矩阵行
        self.pair_seq: np.ndarray = np.zeros(0, dtype=np.int64)  # 帧内块序号
        self.frames_with_vectors: set[str] = set()

    def refresh(self, conn: sqlite3.Connection) -> None:
        rev = db.get_revision(conn)
        with self._lock:
            if rev == self.revision:
                return
            conn.execute("BEGIN")  # 短读快照：矩阵与关联来自同一时刻
            try:
                rows = conn.execute(
                    "SELECT target_id, dim, vector FROM embeddings WHERE target_kind = 'block' AND branch = 'text' "
                    "AND model_id = ? AND preprocess_version = ? ORDER BY emb_id",
                    (self.model_id, vectors.TEXT_PREPROCESS),
                ).fetchall()
                pairs = conn.execute("SELECT frame_id, seq, block_id FROM frame_blocks").fetchall()
            finally:
                conn.execute("COMMIT")
            row_of: dict[int, int] = {}
            vecs = []
            for r in rows:
                row_of[int(r["target_id"])] = len(vecs)
                vecs.append(vectors.from_blob(r["vector"], r["dim"]))
            self.matrix = np.vstack(vecs) if vecs else np.zeros((0, 0), dtype=np.float32)
            kept = [(p["frame_id"], row_of[p["block_id"]], p["seq"]) for p in pairs if p["block_id"] in row_of]
            self.pair_frame = np.array([k[0] for k in kept], dtype=object)
            self.pair_row = np.array([k[1] for k in kept], dtype=np.int64)
            self.pair_seq = np.array([k[2] for k in kept], dtype=np.int64)
            self.frames_with_vectors = set(self.pair_frame.tolist())
            self.revision = rev

    def rank(self, qvec: np.ndarray, allowed: set[str], top: int) -> list[tuple[str, float, int]]:
        """候选帧内按最高块相似度排序，返回 [(frame_id, score, best_seq)]，每帧一次。"""
        with self._lock:
            if not len(self.pair_row) or not allowed:
                return []
            mask = np.fromiter((f in allowed for f in self.pair_frame), dtype=bool, count=len(self.pair_frame))
            if not mask.any():
                return []
            sims = self.matrix @ qvec.astype(np.float32)
            frames, rows, seqs = self.pair_frame[mask], self.pair_row[mask], self.pair_seq[mask]
        scores = sims[rows]
        order = np.argsort(-scores, kind="stable")
        best: dict[str, tuple[float, int]] = {}
        for i in order:
            f = frames[i]
            if f not in best:
                best[f] = (float(scores[i]), int(seqs[i]))
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
        return [(f, s, q) for f, (s, q) in ranked[:top]]


class ImageVectorIndex:
    def __init__(self, model_id: str):
        self.model_id = model_id
        self.revision = -1
        self._lock = threading.Lock()
        self.matrix = np.zeros((0, 0), dtype=np.float32)
        self.frame_ids: list[str] = []

    def refresh(self, conn: sqlite3.Connection) -> None:
        rev = db.get_revision(conn)
        with self._lock:
            if rev == self.revision:
                return
            rows = conn.execute(
                "SELECT target_id, dim, vector FROM embeddings WHERE target_kind = 'frame' AND branch = 'image' "
                "AND model_id = ? AND preprocess_version = ? ORDER BY emb_id",
                (self.model_id, models.IMAGE_PREPROCESS),
            ).fetchall()
            self.frame_ids = [r["target_id"] for r in rows]
            self.matrix = (
                np.vstack([vectors.from_blob(r["vector"], r["dim"]) for r in rows])
                if rows else np.zeros((0, 0), dtype=np.float32)
            )
            self.revision = rev

    def rank(self, qvec: np.ndarray, allowed: set[str], top: int) -> list[tuple[str, float]]:
        with self._lock:
            if not self.frame_ids or not allowed:
                return []
            sims = self.matrix @ qvec.astype(np.float32)
            ids = self.frame_ids
        cand = [(ids[i], float(sims[i])) for i in range(len(ids)) if ids[i] in allowed]
        cand.sort(key=lambda kv: (-kv[1], kv[0]))
        return cand[:top]


# ---------------- 融合 ----------------

def rrf(lists: Sequence[Sequence[str]], k: int = 60) -> dict[str, float]:
    """等权 RRF：名次从 1 开始，缺席分支贡献 0。"""
    out: dict[str, float] = {}
    for lst in lists:
        for rank, fid in enumerate(lst, start=1):
            out[fid] = out.get(fid, 0.0) + 1.0 / (k + rank)
    return out


# ---------------- 编排 ----------------

@dataclass
class Engines:
    """页面持有的查询侧资源；encoder 为 None 表示该分支不可用（模型未就绪或加载失败）。"""

    text_encoder: Callable[[Sequence[str]], np.ndarray] | None = None
    clip_text_encoder: Callable[[Sequence[str]], np.ndarray] | None = None
    text_index: TextVectorIndex | None = None
    image_index: ImageVectorIndex | None = None
    unavailable_reason: dict[str, str] = field(default_factory=dict)


@dataclass
class ModeResponse(SearchResponse):
    mode: str = TEXT
    notes: list[str] = field(default_factory=list)


def _allowed_frames(conn, parsed: ParsedQuery, filters: Filters, case_sensitive: bool):
    occs = search._qualifying_occurrences(conn, None, filters)
    allowed = set(occs)
    req = search.required_frames(conn, parsed, case_sensitive)
    if req is not None:
        allowed &= req
    return allowed, occs, req


def _assemble(conn, fids: list[str], occs: dict[str, list[Occurrence]], *, lex: dict[str, SearchResult],
              sem_best: dict[str, int], scores: dict[str, float], sources: dict[str, tuple[str, ...]]) -> list[SearchResult]:
    out: list[SearchResult] = []
    rows = {
        r["frame_id"]: r
        for r in conn.execute(
            "SELECT frame_id, image_path, image_status, width, height FROM frames "
            "WHERE frame_id IN (SELECT value FROM json_each(?)) AND image_status != 'deleted'",
            (json.dumps(fids),),
        )
    }
    for fid in fids:
        if fid not in rows or fid not in occs:
            continue  # 返回前再次核对有效性
        if fid in lex:
            res = lex[fid]
        else:
            r = rows[fid]
            res = SearchResult(fid, r["image_path"], r["image_status"], r["width"], r["height"], occs[fid][0],
                               len(occs[fid]), (), [], [])
        if fid in sem_best:
            blk = conn.execute(
                "SELECT line_start, line_end FROM frame_blocks WHERE frame_id = ? AND seq = ?", (fid, sem_best[fid])
            ).fetchone()
            if blk is not None:
                res.semantic_lines = list(range(blk["line_start"], blk["line_end"] + 1))
                if not res.snippet or fid not in lex:
                    res.snippet = [
                        r["text"] for r in conn.execute(
                            "SELECT text FROM ocr_lines WHERE frame_id = ? AND line_no BETWEEN ? AND ? "
                            "ORDER BY line_no LIMIT 3",
                            (fid, blk["line_start"], blk["line_end"]),
                        )
                    ]
        res.score = scores.get(fid)
        res.sources = sources.get(fid, ())
        out.append(res)
    return out


def run(
    conn: sqlite3.Connection,
    parsed: ParsedQuery,
    filters: Filters,
    *,
    mode: str,
    engines: Engines,
    case_sensitive: bool = False,
    limit: int = 10,
    rrf_k: int = 60,
    branch_top: int = 50,
) -> ModeResponse:
    if parsed.error:
        return ModeResponse(message=parsed.error, mode=mode)
    if parsed.is_empty:
        return ModeResponse(message="请输入要查找的文字或画面描述。", mode=mode)

    # 只有引号原串：两种模式都按捕获时间展示，不调用模型（提案 4.3）
    if not parsed.terms:
        base = search.search(conn, parsed, filters, case_sensitive=case_sensitive, limit=limit)
        resp = ModeResponse(results=base.results, total=base.total, rank_basis=base.rank_basis,
                            message=base.message, mode=mode)
        if mode == IMAGE:
            resp.results = [r for r in resp.results if r.image_status == "ok"]
            resp.notes.append("只有引号原串：按捕获时间展示，画面模式只列原图仍可用的记录。")
        return resp

    allowed, occs, req = _allowed_frames(conn, parsed, filters, case_sensitive)
    if req is not None and not req:
        return ModeResponse(message="没有画面同时包含全部引号原串。", mode=mode)
    if not allowed:
        return ModeResponse(message="当前日期、应用或引号条件下没有候选画面。", mode=mode)
    description = " ".join(parsed.terms)

    if mode == IMAGE:
        return _run_image(conn, description, allowed, occs, engines, limit, branch_top)
    return _run_text(conn, parsed, filters, description, allowed, occs, engines, case_sensitive, limit, rrf_k, branch_top)


def _run_text(conn, parsed, filters, description, allowed, occs, engines, case_sensitive, limit, rrf_k, branch_top):
    notes: list[str] = []
    lexical = search.search(conn, parsed, filters, case_sensitive=case_sensitive, limit=branch_top)
    lex_hits = [r for r in lexical.results if r.matched_terms and r.frame_id in allowed]
    lex_by_id = {r.frame_id: r for r in lex_hits}
    lex_list = [r.frame_id for r in lex_hits]

    sem: list[tuple[str, float, int]] = []
    if engines.text_encoder is None or engines.text_index is None:
        notes.append("语义分支不可用：" + engines.unavailable_reason.get(TEXT, "文本模型未加载") + "；当前只按词法排序。")
    else:
        engines.text_index.refresh(conn)
        qvec = engines.text_encoder([description])[0]
        sem = engines.text_index.rank(qvec, allowed, branch_top)
        pending = sum(
            1 for r in conn.execute(
                "SELECT frame_id FROM frames WHERE text_embed_status = 'pending' AND image_status != 'deleted'"
            ) if r[0] in allowed
        )
        if pending:
            notes.append(f"{pending} 个候选画面的语义向量尚未完成，暂时只参与词法排序。")
    sem_list = [f for f, _, _ in sem]
    fused = rrf([lex_list, sem_list], rrf_k)
    if not fused:
        return ModeResponse(message="没有找到匹配的记录。", mode=TEXT, notes=notes)

    def latest(fid: str) -> int:
        return occs[fid][0].last_seen_ms

    order = sorted(fused, key=lambda f: (-fused[f], -latest(f), f))
    sem_set = set(sem_list)
    sources = {f: tuple(n for n, hit in (("词法", f in lex_by_id), ("语义", f in sem_set)) if hit) for f in order}
    results = _assemble(conn, order[:limit], occs, lex=lex_by_id, sem_best={f: q for f, _, q in sem},
                        scores=fused, sources=sources)
    return ModeResponse(results=results, total=len(order), rank_basis="rrf", mode=TEXT, notes=notes)


def _run_image(conn, description, allowed, occs, engines, limit, branch_top):
    notes: list[str] = []
    if engines.clip_text_encoder is None or engines.image_index is None:
        reason = engines.unavailable_reason.get(IMAGE, "画面模型未加载")
        return ModeResponse(message=f"画面模式暂不可用：{reason}。不会自动切换到文字模式。", mode=IMAGE)
    ok = {
        r[0] for r in conn.execute("SELECT frame_id FROM frames WHERE image_status = 'ok'") if r[0] in allowed
    }
    engines.image_index.refresh(conn)
    have = set(engines.image_index.frame_ids)
    pending = len(ok - have)
    if pending:
        notes.append(f"{pending} 个候选画面的全图向量尚未完成，本次未参与画面排序。")
    if len(ok) < len(allowed):
        notes.append("原图已过期或缺失的记录不参与画面模式。")
    qvec = engines.clip_text_encoder([description])[0]
    ranked = engines.image_index.rank(qvec, ok, max(limit, branch_top))
    if not ranked:
        return ModeResponse(message="没有可用于画面排序的候选（全图向量可能尚未完成）。", mode=IMAGE, notes=notes)
    order = [f for f, _ in ranked]
    scores = dict(ranked)
    results = _assemble(conn, order[:limit], occs, lex={}, sem_best={}, scores=scores,
                        sources={f: ("画面",) for f in order})
    return ModeResponse(results=results, total=len(order), rank_basis="image", mode=IMAGE, notes=notes)
