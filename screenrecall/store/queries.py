"""只读查询接口：网页和 MCP 共用，返回可直接 JSON 序列化的 dict / list。

- 每个线程自己的只读连接（schema.connect_ro），录制进程没运行时也能打开；
- search：词法分支（fts.lexical）+ 可选向量分支，按内容源分组；按最近或按相关度排序，可翻页；
- tasks：任务标签与片段数（搜索页的任务下拉框）；
- timeline：窗口焦点、片段、缺口按开始时间排好；
- piece：片段里各块的文字按阅读顺序排列（合并过的块换算到根块坐标），并标出划过与缺口；
- summary / frame / day_overview / control / apps。
给 Agent 用时由 MCP 层传 exclude_apps / exclude_title_keywords（"对 Agent 隐藏"名单），这里只负责过滤。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from ..timeutil import local_dates_to_utc_range, utc_ms
from . import schema
from .fts import docs, lexical
from .gate import VectorGate, has_letters

# 向量分支的分数门槛（合成评测 experiments/search 定的，见 Queries._vector_groups）
MIN_VECTOR_SCORE = 0.45
VECTOR_MARGIN = 0.15
SEMANTIC_WITH_LITERAL = 0.55  # 有字面命中时，只有语义命中的组要达到的分数
VECTOR_K = 400         # 向量库一次取多少条候选（之后按门槛筛、回表核验）
LEXICAL_POOL = 200      # relevant 排序时，最近多少组字面命中参与按相关度排
Encoder = Callable[[str], np.ndarray]


class Hidden:
    """"对 Agent 隐藏"名单：应用名整名匹配、标题包含关键词，都按 NFKC + casefold 比较（与 SQL 里的 sr_fold 一致）。"""

    def __init__(self, apps: Sequence[str] = (), title_keywords: Sequence[str] = ()):
        self.apps = {docs.fold(a) for a in apps if a}
        self.keywords = [docs.fold(k) for k in title_keywords if k]

    def __bool__(self) -> bool:
        return bool(self.apps or self.keywords)

    def match(self, app: str | None, title: str | None) -> bool:
        if app and docs.fold(app) in self.apps:
            return True
        t = docs.fold(title or "")
        return any(k in t for k in self.keywords)


class Queries:
    def __init__(self, data_dir: Path | str, *, text_vectors=None, encoder: Encoder | None = None,
                 clock: Callable[[], int] = utc_ms):
        self.data_dir = Path(data_dir)
        self.db_path = schema.db_path(self.data_dir)
        self.text_vectors = text_vectors
        self.encoder = encoder
        self.clock = clock
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []  # 各线程的连接，close() 一起关
        self._conns_lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = schema.connect_ro(self.db_path)
            with self._conns_lock:
                self._conns.append(c)
        return c

    def close(self) -> None:
        """关闭所有线程打开的连接（各线程之后再查询会重新打开）。"""
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    # ---------------- 搜索 ----------------

    def search(self, query: str, time_from: int | None = None, time_to: int | None = None, app: str | None = None,
               task: str | None = None, limit: int = 20, encoder: Encoder | None = None, *, per_group: int = 3,
               exclude_apps: Sequence[str] = (), exclude_title_keywords: Sequence[str] = (), sort: str = "recent",
               offset: int = 0, min_score: float = MIN_VECTOR_SCORE, score_margin: float = VECTOR_MARGIN,
               gate: VectorGate | None = None) -> dict:
        """词法 + 可选向量分支，按内容源（segment）分组；返回第 offset 组起的 limit 组。

        向量库约定：id > 0 是单行（line_id），id < 0 是段落（text_chunks.chunk_id，一个向量对应几行），
        group = segment_id，ts = 行（段落里最早一行）的 first_seen_ms（不变）。
        向量命中先按分数筛（见 _vector_groups）；再在 SQL 里核验行的存在区间，与词法分支的时间语义一致
        （行的 [first_seen, last_seen] 与 [time_from, time_to) 重叠），也挡掉已删除（墓碑还没读到）的行。

        sort：
        - recent：按组里最后一次看到排（新的在前）；
        - relevant：字面命中的组在前（组的语义分 + 1，同分按最近），之后是只有语义命中的组（按语义分）。
          字面命中的组只在最近 LEXICAL_POOL 组里按相关度排，更早的接在后面、按最近排，翻页时顺序不变。
        每组带 sources（lexical / vector）、score（relevant 用的分数）、semantic_score（语义分，可能为 None）。
        has_more：后面还有组。

        gate（store.gate.VectorGate）：给了就按命中行的"凑数程度"修正分数再筛（见 gate 模块），
        只有语义命中的组的 semantic_score 是修正分；纯数字 / 符号的查询不走语义分支。
        没给就用 min_score / score_margin 的固定门槛（余弦）。
        """
        conn = self.conn()
        offset, limit = max(0, int(offset)), max(1, int(limit))
        filters = lexical.Filters(time_from, time_to, app, task, tuple(exclude_apps), tuple(exclude_title_keywords))
        encoder = encoder or self.encoder
        semantic = encoder is not None and self.text_vectors is not None and bool(query.strip())
        if semantic and gate is not None and not has_letters(query):
            semantic = False
        need = offset + limit
        pool = max(need + 1, LEXICAL_POOL) if semantic and sort == "relevant" else need + 1
        lex = lexical.search(conn, query, filters=filters, limit=pool, per_group=per_group)
        out = lex.to_dict()
        lex_groups = out["groups"]
        for g in lex_groups:
            g["sources"], g["semantic_score"] = ["lexical"], None
        vec_only: list[dict] = []
        if semantic:
            qv = np.asarray(encoder(query), dtype=np.float32)
            allowed = None
            if app or task or exclude_apps or exclude_title_keywords or time_from is not None or time_to is not None:
                allowed = self._segment_ids(conn, filters)
            by_seg = {int(g["segment_id"]): g for g in lex_groups}
            for seg_id, (score, rows) in self._vector_groups(conn, qv, allowed, time_from, time_to, per_group,
                                                             min_score, score_margin, gate).items():
                g = by_seg.get(seg_id)
                if g is None:
                    g = _vector_group(rows)
                    g["semantic_score"] = score
                    vec_only.append(g)
                else:
                    g["sources"].append("vector")
                    g["semantic_score"] = score
            if lex_groups:
                # 已有字面命中：只有意思很接近的才另外列出（查生僻词、半个词时向量分支多半是凑数）
                bar = SEMANTIC_WITH_LITERAL if gate is None else gate.config.floor_with_literal
                vec_only = [g for g in vec_only if g["semantic_score"] >= bar]
            self._literal_scores(qv, lex_groups)
            out["mode"] = out["mode"] + "+vector"
        for g in lex_groups:
            g["score"] = 1.0 + (g["semantic_score"] or 0.0)
        for g in vec_only:
            g["score"] = float(g["semantic_score"])
        recency = lambda g: (-int(g["last_seen_ms"]), -int(g["segment_id"]))  # noqa: E731
        if sort == "relevant":
            head = sorted(lex_groups[:LEXICAL_POOL] + vec_only, key=lambda g: (-g["score"],) + recency(g))
            ordered = head + lex_groups[LEXICAL_POOL:]
        else:
            ordered = sorted(lex_groups + vec_only, key=recency)
        out["groups"] = ordered[offset:need]
        out["has_more"] = len(ordered) > need
        out["total_groups"] = int(out["total_groups"]) + len(vec_only)
        out["offset"], out["sort"] = offset, sort
        return out

    def _vector_groups(self, conn, qv: np.ndarray, allowed: list[int] | None, time_from: int | None,
                       time_to: int | None, per_group: int, min_score: float,
                       margin: float, gate: VectorGate | None = None) -> dict[int, tuple[float, list]]:
        """向量分支 → {segment_id: (组里最高的语义分, 要显示的行)}，按分数从高到低。

        有 gate：修正分（余弦 - 命中行的凑数程度）>= max(floor, 最高修正分 - margin)，见 store.gate；
        没有：余弦 >= max(min_score, 最高分 - margin)。
        组里的行：单行命中就是那一行；段落命中是段落里的行（阅读顺序），按命中分数先后、去重，最多 per_group 行。"""
        # ts = first_seen：first_seen < time_to 可以直接在向量库里筛；time_from 要看 last_seen，只能回表核验
        hits = self.text_vectors.search(qv, k=VECTOR_K, time_to=time_to, groups=allowed)
        if not hits:
            return {}
        if gate is not None:
            # 修正分 <= 余弦（凑数程度 >= 0）：余弦不到 floor 的不可能留下，不用取向量
            cand = [h for h in hits if h.score >= gate.config.floor]
            vecs = self.text_vectors.vectors([h.id for h in cand]) if cand else {}
            cand = [h for h in cand if h.id in vecs]
            if not cand:
                return {}
            keep, adj = gate.select([h.score for h in cand], np.stack([vecs[h.id] for h in cand]))
            hits = [replace(cand[int(i)], score=float(a)) for i, a in zip(keep, adj)]
        else:
            cut = max(float(min_score), float(hits[0].score) - float(margin))
            hits = [h for h in hits if h.score >= cut]
        rows_of = self._vector_rows(conn, [h.id for h in hits], time_from, time_to)
        out: dict[int, tuple[float, list]] = {}
        for h in hits:
            rows = rows_of.get(int(h.id))
            if not rows:  # 行已删除（墓碑还没读到），或存在区间不在范围里
                continue
            seg_id = int(rows[0]["segment_id"])
            score, shown = out.get(seg_id, (float(h.score), []))
            have = {int(r["line_id"]) for r in shown}
            for r in rows:
                if len(shown) < max(1, per_group) and int(r["line_id"]) not in have:
                    shown.append(r)
                    have.add(int(r["line_id"]))
            out[seg_id] = (score, shown)
        return out

    def _literal_scores(self, qv: np.ndarray, groups: list[dict]) -> None:
        """字面命中的行自己的语义分（单行向量 id = line_id）：同是字面命中，意思更贴近的组排前面。"""
        ids = [int(ln["line_id"]) for g in groups for ln in g["lines"]]
        if not ids:
            return
        best = {int(h.id): float(h.score) for h in self.text_vectors.search(qv, k=len(ids), ids=ids)}
        for g in groups:
            s = max((best[int(ln["line_id"])] for ln in g["lines"] if int(ln["line_id"]) in best), default=None)
            if s is not None and (g["semantic_score"] is None or s > g["semantic_score"]):
                g["semantic_score"] = s

    @staticmethod
    def _vector_rows(conn, vec_ids: Sequence[int], time_from: int | None, time_to: int | None) -> dict[int, list]:
        """向量 id → 行（阅读顺序）。id < 0：段落，text_chunks 里登记的成员；id > 0：单行（id = line_id）；
        老布局的段落 id 是正数，同样先查 text_chunks。只返回存在区间与 [time_from, time_to) 重叠的行。"""
        if not vec_ids:
            return {}
        ids = json.dumps(sorted({int(x) for x in vec_ids}))
        rows = conn.execute(
            "WITH v(id) AS (SELECT CAST(value AS INTEGER) FROM json_each(:ids)), "
            "m(id, line_id) AS (SELECT c.chunk_id, c.line_id FROM text_chunks c JOIN v ON v.id = c.chunk_id "
            "UNION SELECT v.id, v.id FROM v WHERE v.id > 0 AND NOT EXISTS "
            "(SELECT 1 FROM text_chunks c WHERE c.chunk_id = v.id)) "
            "SELECT m.id AS vec_id, l.line_id, l.block_id, l.text, l.x0, l.y0, l.x1, l.y1, l.source, l.confidence, "
            "l.first_seen_ms, l.last_seen_ms, l.disappeared_ms, s.segment_id, s.app, s.title "
            "FROM m JOIN lines l ON l.line_id = m.line_id JOIN segments s ON s.segment_id = l.segment_id "
            "WHERE l.alias_of IS NULL AND l.last_seen_ms >= :lo AND l.first_seen_ms < :hi "
            "ORDER BY m.id, l.order_key, l.line_id",
            {"ids": ids, "lo": -(2 ** 62) if time_from is None else int(time_from),
             "hi": 2 ** 62 if time_to is None else int(time_to)},
        ).fetchall()
        out: dict[int, list] = {}
        for r in rows:
            out.setdefault(int(r["vec_id"]), []).append(r)
        return out

    def tasks(self, time_from: int | None = None, time_to: int | None = None, limit: int = 200) -> list[dict]:
        """时间范围内片段的任务标签（大模型写的短语）与片段数，给搜索页的"任务"下拉框用。
        大小写、全半角、空白不同的写法算同一个任务，显示最常用（同样多时最近）的写法。"""
        lo = -(2 ** 62) if time_from is None else int(time_from)
        hi = 2 ** 62 if time_to is None else int(time_to)
        rows = self.conn().execute(
            "SELECT task_label, COUNT(*) AS n, MAX(end_ms) AS last FROM pieces "
            "WHERE task_label IS NOT NULL AND TRIM(task_label) != '' AND end_ms >= ? AND start_ms < ? "
            "GROUP BY task_label", (lo, hi)).fetchall()
        groups: dict[str, dict] = {}
        for r in rows:
            label = str(r["task_label"]).strip()
            key = docs.norm(label)
            g = groups.setdefault(key, {"task": label, "n": 0, "last_ms": 0, "_best": (-1, 0)})
            g["n"] += int(r["n"])
            g["last_ms"] = max(g["last_ms"], int(r["last"]))
            if (int(r["n"]), int(r["last"])) > g["_best"]:
                g["task"], g["_best"] = label, (int(r["n"]), int(r["last"]))
        out = sorted(groups.values(), key=lambda g: (-g["n"], -g["last_ms"], g["task"]))[:max(1, int(limit))]
        for g in out:
            g.pop("_best")
        return out

    def _segment_ids(self, conn, f: lexical.Filters) -> list[int]:
        params: dict = {}
        sql = lexical.segment_filter_sql(f, params) or "SELECT s.segment_id FROM segments s WHERE 1"
        if f.time_from is not None:
            params["seg_from"] = int(f.time_from)
            sql += " AND s.last_seen_ms >= :seg_from"
        if f.time_to is not None:
            params["seg_to"] = int(f.time_to)
            sql += " AND s.first_seen_ms < :seg_to"
        return [int(r[0]) for r in conn.execute(sql, params)]

    # ---------------- 时间线 ----------------

    def timeline(self, time_from: int, time_to: int, app: str | None = None, *,
                 exclude_apps: Sequence[str] = (), exclude_title_keywords: Sequence[str] = ()) -> dict:
        conn = self.conn()
        lo, hi = int(time_from), int(time_to)
        now = self.clock()
        hidden = Hidden(exclude_apps, exclude_title_keywords).match

        items: list[dict] = []
        app_sql = " AND process = ? COLLATE NOCASE" if app else ""
        for r in conn.execute(
            f"SELECT * FROM activity WHERE COALESCE(end_ms, ?) > ? AND start_ms < ?{app_sql} ORDER BY start_ms",
            [now, lo, hi] + ([app] if app else []),
        ):
            if hidden(r["process"], r["title"]):
                continue
            items.append({"kind": "activity", "id": r["activity_id"], "start_ms": r["start_ms"],
                          "end_ms": r["end_ms"], "open": r["end_ms"] is None, "process": r["process"],
                          "title": None if r["excluded"] else r["title"], "excluded": bool(r["excluded"])})
        app_sql = " AND app = ? COLLATE NOCASE" if app else ""
        for r in conn.execute(
            f"SELECT * FROM pieces WHERE end_ms >= ? AND start_ms < ?{app_sql} ORDER BY start_ms",
            [lo, hi] + ([app] if app else []),
        ):
            if hidden(r["app"], r["title"]):
                continue
            items.append({"kind": "piece", "id": r["piece_id"], "start_ms": r["start_ms"], "end_ms": r["end_ms"],
                          "app": r["app"], "title": r["title"], "task_label": r["task_label"]})
        for r in conn.execute(
            "SELECT * FROM gaps WHERE COALESCE(end_ms, ?) > ? AND start_ms < ? ORDER BY start_ms", (now, lo, hi)
        ):
            if app and r["process"] and r["process"].casefold() != app.casefold():
                continue
            items.append({"kind": "gap", "id": r["gap_id"], "start_ms": r["start_ms"], "end_ms": r["end_ms"],
                          "open": r["end_ms"] is None, "reason": r["reason"],
                          # detail 常常就是窗口标题：标题关键词同样要拦
                          "detail": None if hidden(r["process"], r["detail"]) else r["detail"]})
        order = {"gap": 0, "activity": 1, "piece": 2}
        items.sort(key=lambda x: (x["start_ms"], order[x["kind"]], x["id"]))
        return {"time_from": lo, "time_to": hi, "items": items}

    # ---------------- 片段 ----------------

    def piece(self, piece_id: int, *, max_lines: int = 5000, exclude_apps: Sequence[str] = (),
              exclude_title_keywords: Sequence[str] = ()) -> dict | None:
        """片段详情；片段本身命中隐藏名单时返回 None，其中命中隐藏名单的内容源（及其证据帧）不返回。"""
        conn = self.conn()
        hidden = Hidden(exclude_apps, exclude_title_keywords)
        p = conn.execute("SELECT * FROM pieces WHERE piece_id = ?", (int(piece_id),)).fetchone()
        if p is None or hidden.match(p["app"], p["title"]):
            return None
        seg_ids = [int(r[0]) for r in conn.execute(
            "SELECT segment_id FROM piece_segments WHERE piece_id = ? ORDER BY segment_id", (int(piece_id),))]
        start, end = int(p["start_ms"]), int(p["end_ms"])
        segments = []
        shown: list[int] = []
        truncated = False
        budget = max_lines
        for sid in seg_ids:
            seg = conn.execute("SELECT * FROM segments WHERE segment_id = ?", (sid,)).fetchone()
            if seg is None or hidden.match(seg["app"], seg["title"]):
                continue
            shown.append(sid)
            roots = _block_roots(conn, sid)
            rows = conn.execute(
                "SELECT * FROM lines WHERE segment_id = ? AND last_seen_ms >= ? AND first_seen_ms <= ? "
                "AND alias_of IS NULL ORDER BY first_seen_ms LIMIT ?", (sid, start, end, budget + 1),
            ).fetchall()
            if len(rows) > budget:
                rows = rows[:budget]
                truncated = True
            budget -= len(rows)
            blocks: dict[int, dict] = {}

            def block_entry(root: int) -> dict:
                b = blocks.get(root)
                if b is None:
                    info = roots.get(root, (root, 0, "none", 0))
                    b = blocks[root] = {"block_id": root, "anchor": info[2], "created_ms": info[3],
                                        "merged": [], "lines": [], "skims": []}
                return b

            for r in rows:
                root, off, _, _ = roots.get(int(r["block_id"]), (int(r["block_id"]), 0, "none", 0))
                b = block_entry(root)
                if int(r["block_id"]) != root and int(r["block_id"]) not in b["merged"]:
                    b["merged"].append(int(r["block_id"]))
                b["lines"].append({
                    "line_id": r["line_id"], "text": r["text"], "x0": r["x0"], "y0": r["y0"] + off,
                    "x1": r["x1"], "y1": r["y1"] + off, "order": r["order_key"] + off, "source": r["source"],
                    "confidence": r["confidence"], "first_seen_ms": r["first_seen_ms"],
                    "last_seen_ms": r["last_seen_ms"], "disappeared_ms": r["disappeared_ms"],
                    "replaces_line_id": r["replaces_line_id"],
                })
            for s in conn.execute(
                "SELECT * FROM skims WHERE segment_id = ? AND end_ms >= ? AND start_ms <= ? ORDER BY start_ms",
                (sid, start, end),
            ):
                bid = s["block_id"]
                root, off = (roots.get(int(bid), (int(bid), 0, "none", 0))[:2] if bid is not None else (None, 0))
                entry = {"start_ms": s["start_ms"], "end_ms": s["end_ms"],
                         "y_from": None if s["y_from"] is None else s["y_from"] + off,
                         "y_to": None if s["y_to"] is None else s["y_to"] + off}
                if root is None:
                    entry["block_id"] = None
                    block_entry(-1)["skims"].append(entry)
                else:
                    block_entry(root)["skims"].append(entry)
            ordered = sorted(blocks.values(), key=lambda b: (b["created_ms"], b["block_id"]))
            for b in ordered:
                b["lines"].sort(key=lambda x: (x["order"], x["first_seen_ms"], x["line_id"]))
                b.pop("created_ms")
            segments.append({"segment_id": sid, "app": seg["app"], "title": seg["title"], "blocks": ordered})
        gaps = [{"gap_id": g["gap_id"], "start_ms": g["start_ms"], "end_ms": g["end_ms"], "reason": g["reason"]}
                for g in conn.execute("SELECT * FROM gaps WHERE COALESCE(end_ms, ?) > ? AND start_ms < ? "
                                      "ORDER BY start_ms", (self.clock(), start, end))]
        frames = [dict(r) for r in conn.execute(
            "SELECT frame_id, captured_ms, segment_id, block_id, pos_y, width, height FROM frames "
            "WHERE status = 'ok' AND captured_ms >= ? AND captured_ms <= ? AND segment_id IN "
            "(SELECT CAST(value AS INTEGER) FROM json_each(?)) ORDER BY captured_ms",
            (start, end, json.dumps(shown)))]
        return {"piece_id": p["piece_id"], "start_ms": start, "end_ms": end, "app": p["app"], "title": p["title"],
                "task_label": p["task_label"], "segments": segments, "gaps": gaps, "frames": frames,
                "truncated": truncated, "summary": self.summary("piece", str(p["piece_id"]))}

    # ---------------- 概括、帧、概览 ----------------

    def summary(self, level: str, key: str, *, exclude_apps: Sequence[str] = (),
                exclude_title_keywords: Sequence[str] = ()) -> dict | None:
        """缓存的概括。piece 级：片段命中隐藏名单时返回 None。

        hour / day 级的概括是整段时间的文字，这里没法按应用拆开；给 Agent 用时由生成方（空闲整理进程）
        把隐藏名单里的内容排除在外，或由 MCP 层在名单非空时不提供。
        """
        conn = self.conn()
        hidden = Hidden(exclude_apps, exclude_title_keywords)
        if level == "piece" and hidden:
            try:
                p = conn.execute("SELECT app, title FROM pieces WHERE piece_id = ?", (int(key),)).fetchone()
            except ValueError:
                p = None
            if p is not None and hidden.match(p["app"], p["title"]):
                return None
        r = conn.execute("SELECT * FROM summaries WHERE level = ? AND key = ?", (level, str(key))).fetchone()
        if r is None:
            return None
        return {"level": r["level"], "key": r["key"], "text": r["text"], "refs": json.loads(r["refs"] or "[]"),
                "status": r["status"], "model": r["model"], "endpoint_host": r["endpoint_host"],
                "start_ms": r["start_ms"], "end_ms": r["end_ms"], "created_ms": r["created_ms"]}

    def frame(self, frame_id: str, highlight: int | None = None, *, exclude_apps: Sequence[str] = (),
              exclude_title_keywords: Sequence[str] = ()) -> dict | None:
        """证据帧（文件路径 + 各行在帧里的位置）。帧或它的内容源命中隐藏名单时返回 None。"""
        conn = self.conn()
        r = conn.execute(
            "SELECT f.*, s.app AS seg_app, s.title AS seg_title FROM frames f "
            "LEFT JOIN segments s ON s.segment_id = f.segment_id WHERE f.frame_id = ?", (str(frame_id),)).fetchone()
        if r is None:
            return None
        hidden = Hidden(exclude_apps, exclude_title_keywords)
        if hidden and (hidden.match(r["app"], r["title"]) or hidden.match(r["seg_app"], r["seg_title"])):
            return None
        lines = [{"line_id": x["line_id"], "text": x["text"], "rect": [x["x0"], x["y0"], x["x1"], x["y1"]],
                  "highlight": highlight is not None and int(x["line_id"]) == int(highlight)}
                 for x in conn.execute(
                     "SELECT f.line_id, l.text, f.x0, f.y0, f.x1, f.y1 FROM line_in_frame f "
                     "JOIN lines l ON l.line_id = f.line_id WHERE f.frame_id = ? ORDER BY f.y0, f.x0", (frame_id,))]
        return {"frame_id": r["frame_id"], "frame_no": r["frame_no"], "status": r["status"],
                "rel_path": r["image_path"], "path": str(self.data_dir / r["image_path"]),
                "width": r["width"], "height": r["height"], "bytes": r["bytes"], "captured_ms": r["captured_ms"],
                "last_seen_ms": r["last_seen_ms"],
                "segment_id": r["segment_id"], "block_id": r["block_id"], "pos_y": r["pos_y"], "app": r["app"],
                "title": r["title"], "keep": bool(r["keep"]), "lines": lines}

    def day_overview(self, day: date | None = None) -> dict:
        """某一天（本地日期，默认今天）的计数：各应用使用时长、新文字行、证据帧、片段、缺口、待 OCR。"""
        day = day or datetime.now().date()
        lo, hi = local_dates_to_utc_range(day, day)
        conn = self.conn()
        now = self.clock()
        apps = [{"process": r[0], "ms": int(r[1] or 0), "switches": int(r[2])} for r in conn.execute(
            "SELECT process, SUM(MIN(COALESCE(end_ms, :now), :hi) - MAX(start_ms, :lo)), COUNT(*) FROM activity "
            "WHERE COALESCE(end_ms, :now) > :lo AND start_ms < :hi AND excluded = 0 GROUP BY process "
            "ORDER BY 2 DESC", {"now": now, "lo": lo, "hi": hi})]
        gaps = {r[0]: {"count": int(r[1]), "ms": int(r[2] or 0)} for r in conn.execute(
            "SELECT reason, COUNT(*), SUM(MIN(COALESCE(end_ms, :now), :hi) - MAX(start_ms, :lo)) FROM gaps "
            "WHERE COALESCE(end_ms, :now) > :lo AND start_ms < :hi GROUP BY reason", {"now": now, "lo": lo, "hi": hi})}

        def count(sql: str, *args) -> int:
            return int(conn.execute(sql, args).fetchone()[0])

        return {
            "day": day.isoformat(), "time_from": lo, "time_to": hi, "apps": apps, "gaps": gaps,
            # first_seen 没有索引；first_seen >= lo 蕴含 last_seen >= lo，加上这一条才能走 idx_lines_last_seen
            "lines_new": count("SELECT COUNT(*) FROM lines WHERE last_seen_ms >= ? AND first_seen_ms >= ? "
                               "AND first_seen_ms < ?", lo, lo, hi),
            "lines_seen": count("SELECT COUNT(*) FROM lines WHERE last_seen_ms >= ? AND first_seen_ms < ?", lo, hi),
            "frames": count("SELECT COUNT(*) FROM frames WHERE status = 'ok' AND captured_ms >= ? AND captured_ms < ?",
                            lo, hi),
            "pieces": count("SELECT COUNT(*) FROM pieces WHERE end_ms >= ? AND start_ms < ?", lo, hi),
            "ocr_pending": count("SELECT COUNT(*) FROM ocr_regions WHERE status IN ('pending', 'running')"),
            "summary": self.summary("day", day.isoformat()),
        }

    def control(self, alive_ms: int = 15_000) -> dict:
        r = self.conn().execute("SELECT * FROM control WHERE id = 1").fetchone()
        d: dict[str, Any] = dict(r)
        for k in ("pinned_windows", "excluded_processes", "excluded_title_keywords", "hidden_from_agents"):
            d[k] = json.loads(d[k] or "null")
        d["agent_enabled"] = bool(d["agent_enabled"])
        d["recorder_alive"] = self.clock() - int(d["recorder_heartbeat_ms"] or 0) <= alive_ms
        return d

    def apps(self, time_from: int | None = None, time_to: int | None = None, *,
             exclude_apps: Sequence[str] = ()) -> list[dict]:
        """出现过的应用（内容源）与最近看到时间，供筛选下拉框用。"""
        lo = -(2 ** 62) if time_from is None else int(time_from)
        hi = 2 ** 62 if time_to is None else int(time_to)
        hidden = Hidden(exclude_apps)
        return [{"app": r[0], "segments": int(r[1]), "last_seen_ms": int(r[2])} for r in self.conn().execute(
            "SELECT app, COUNT(*), MAX(last_seen_ms) FROM segments WHERE last_seen_ms >= ? AND first_seen_ms < ? "
            "GROUP BY app ORDER BY 3 DESC", (lo, hi)) if not hidden.match(r[0], None)]


def _vector_group(rows) -> dict:
    """只有向量分支命中的内容源：一组；行 = 命中段落里（时间范围内）的前几行。"""
    row = rows[0]
    return {
        "segment_id": row["segment_id"], "app": row["app"], "title": row["title"], "hits": 0,
        "last_seen_ms": max(r["last_seen_ms"] for r in rows), "sources": ["vector"],
        "lines": [{
            "line_id": r["line_id"], "block_id": r["block_id"], "text": r["text"],
            "rect": [r["x0"], r["y0"], r["x1"], r["y1"]], "source": r["source"],
            "confidence": r["confidence"], "first_seen_ms": r["first_seen_ms"],
            "last_seen_ms": r["last_seen_ms"], "disappeared_ms": r["disappeared_ms"],
        } for r in rows],
    }


def _block_roots(conn, segment_id: int) -> dict[int, tuple[int, int, str, int]]:
    """本内容源每个块 → (根块, 到根块的偏移, 根块锚点, 根块创建时间)。"""
    rows = {int(r[0]): (r[1], int(r[2]), r[3], int(r[4])) for r in conn.execute(
        "SELECT block_id, merged_into, offset_px, anchor, created_ms FROM blocks WHERE segment_id = ?", (segment_id,))}
    out: dict[int, tuple[int, int, str, int]] = {}
    for bid in rows:
        cur, off, seen = bid, 0, set()
        while True:
            seen.add(cur)
            info = rows.get(cur)
            if info is None:  # 并入了别的内容源的块：查一次
                r = conn.execute("SELECT merged_into, offset_px, anchor, created_ms FROM blocks WHERE block_id = ?",
                                 (cur,)).fetchone()
                if r is None:
                    break
                info = (r[0], int(r[1]), r[2], int(r[3]))
            if info[0] is None or int(info[0]) in seen:
                out[bid] = (cur, off, info[2], info[3])
                break
            off += info[1]
            cur = int(info[0])
        out.setdefault(bid, (bid, 0, rows[bid][2], rows[bid][3]))
    return out
