"""只读查询接口：网页和 MCP 共用，返回可直接 JSON 序列化的 dict / list。

- 每个线程自己的只读连接（schema.connect_ro），录制进程没运行时也能打开；
- search：词法分支（fts.lexical）+ 可选向量分支，按内容源分组后用 RRF（k = 60）融合；
- timeline：窗口焦点、片段、缺口按开始时间排好；
- piece：片段里各块的文字按阅读顺序排列（合并过的块换算到根块坐标），并标出划过与缺口；
- summary / frame / day_overview / control / apps。
给 Agent 用时由 MCP 层传 exclude_apps / exclude_title_keywords（"对 Agent 隐藏"名单），这里只负责过滤。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from ..timeutil import local_dates_to_utc_range, utc_ms
from . import schema
from .fts import docs, lexical

RRF_K = 60
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
               exclude_apps: Sequence[str] = (), exclude_title_keywords: Sequence[str] = ()) -> dict:
        """词法 + 可选向量分支（RRF 融合）。

        向量库约定：id = line_id 或段落编号 text_chunks.chunk_id（一个向量对应几行），group = segment_id，
        ts = 行（段落里最早一行）的 first_seen_ms（不变）。
        向量分支先按内容源（筛选 + 时间与内容源的存在区间重叠）限定组，再逐条在 SQL 里核验行的存在区间，
        与词法分支的时间语义一致（行的 [first_seen, last_seen] 与 [time_from, time_to) 重叠）。
        """
        conn = self.conn()
        filters = lexical.Filters(time_from, time_to, app, task, tuple(exclude_apps), tuple(exclude_title_keywords))
        lex = lexical.search(conn, query, filters=filters, limit=limit, per_group=per_group)
        out = lex.to_dict()
        for g in out["groups"]:
            g["sources"] = ["lexical"]
        encoder = encoder or self.encoder
        if encoder is None or self.text_vectors is None or not query.strip():
            return out

        allowed = None
        if app or task or exclude_apps or exclude_title_keywords or time_from is not None or time_to is not None:
            allowed = self._segment_ids(conn, filters)
        qv = np.asarray(encoder(query), dtype=np.float32)
        # ts = first_seen：first_seen < time_to 可以直接在向量库里筛；time_from 要看 last_seen，只能回表核验
        hits = self.text_vectors.search(qv, k=max(limit * 3, 30), time_to=time_to, groups=allowed,
                                        best_per_group=True)
        fused: dict[int, float] = {}
        for rank, g in enumerate(out["groups"], 1):
            fused[g["segment_id"]] = fused.get(g["segment_id"], 0.0) + 1.0 / (RRF_K + rank)
        by_seg = {g["segment_id"]: g for g in out["groups"]}
        rank = 0
        for h in hits:
            rows = self._vector_lines(conn, h.id, time_from, time_to)
            if not rows:  # 行已删除（墓碑还没读到），或存在区间不在范围里
                continue
            row = rows[0]
            rank += 1
            if rank > limit:
                break
            seg_id = int(row["segment_id"])
            fused[seg_id] = fused.get(seg_id, 0.0) + 1.0 / (RRF_K + rank)
            g = by_seg.get(seg_id)
            if g is None:
                g = by_seg[seg_id] = _vector_group(rows[:max(1, per_group)])
            elif "vector" not in g["sources"]:
                g["sources"].append("vector")
            g["vector_score"] = h.score
        groups = [g for s, g in by_seg.items() if s in fused]
        for g in groups:
            g["score"] = fused[g["segment_id"]]
        groups.sort(key=lambda g: (-g["score"], -g["last_seen_ms"]))
        out["groups"] = groups[:limit]
        out["mode"] = out["mode"] + "+vector"
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

    @staticmethod
    def _vector_lines(conn, vec_id: int, time_from: int | None, time_to: int | None) -> list:
        """向量 id → 行（阅读顺序）。空闲整理进程按段落算向量：id = text_chunks.chunk_id，对应段落的全部行；
        没有段落登记的 id 按单行（id = line_id）处理。只返回存在区间与 [time_from, time_to) 重叠的行。"""
        return conn.execute(
            "SELECT l.line_id, l.block_id, l.text, l.x0, l.y0, l.x1, l.y1, l.source, l.confidence, l.first_seen_ms, "
            "l.last_seen_ms, l.disappeared_ms, s.segment_id, s.app, s.title "
            "FROM lines l JOIN segments s ON s.segment_id = l.segment_id WHERE l.line_id IN "
            "(SELECT line_id FROM text_chunks WHERE chunk_id = :id UNION SELECT :id) "
            "AND l.last_seen_ms >= :lo AND l.first_seen_ms < :hi ORDER BY l.order_key, l.line_id",
            {"id": int(vec_id), "lo": -(2 ** 62) if time_from is None else int(time_from),
             "hi": 2 ** 62 if time_to is None else int(time_to)},
        ).fetchall()

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
                "ORDER BY first_seen_ms LIMIT ?", (sid, start, end, budget + 1),
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
