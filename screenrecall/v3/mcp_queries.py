"""app.mcp_server.Queries 的 v3 实现：把 v3.store.Queries 的结果（V3_CONTRACT §3.6 的 JSON 形状）换成 MCP 工具的数据类。

编号：piece = 时间线上的一条，f"v{visit_id}"；frame = 一张截图，f"s{shot_id}"；line_id 就是库里的行号。
格式不对的编号一律当"找不到"（返回 None），不抛异常。
"对 Agent 隐藏"的应用 / 标题关键词（Exclusion）在返回前按 app 和标题（原标题、显示名字都看）过滤；v3 默认空。
只读：只调 Queries 的查询方法，不写库。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from mcp.server.fastmcp.exceptions import ToolError

from ..app.mcp_server import (Context, Copied, CtxLine, Exclusion, Frame, GapItem, Hit, LineRect, Piece, Summary,
                              fmt_minutes)
from ..timeutil import local_dates_to_utc_range

_PIECE = re.compile(r"v(\d{1,18})")
_FRAME = re.compile(r"s(\d{1,18})")
REASON_TEXT = {"still": "画面停下", "forced": "一直在动", "copy": "复制"}
SEARCH_MAX = 100          # 一次最多向 Queries 要几条结果（有隐藏规则时多要一些，过滤后再截）


def piece_id(visit_id) -> str | None:
    return None if visit_id is None else f"v{int(visit_id)}"


def frame_id(shot_id) -> str | None:
    return None if shot_id is None else f"s{int(shot_id)}"


def parse_piece(pid: str | None) -> int | None:
    m = _PIECE.fullmatch((pid or "").strip())
    return int(m.group(1)) if m else None


def parse_frame(fid: str | None) -> int | None:
    m = _FRAME.fullmatch((fid or "").strip())
    return int(m.group(1)) if m else None


def where(src: dict | None) -> str | None:
    """出处的网址或文件路径（软件 + 标题的出处没有）。"""
    src = src or {}
    if src.get("kind") == "file" and src.get("path"):
        return str(src["path"])
    return src.get("url") or src.get("path") or None


def display_name(src: dict | None) -> str:
    """显示名字：name > 清理后的 title > 网址主机名 + 路径 / 文件名 > app。"""
    src = src or {}
    if src.get("name"):
        return str(src["name"])
    if src.get("title"):
        return str(src["title"])
    if src.get("path"):
        return Path(str(src["path"]).replace("\\", "/")).name or str(src["path"])
    if src.get("url"):
        u = urlsplit(str(src["url"]))
        return (u.hostname or "") + (u.path if u.path not in ("", "/") else "")
    return str(src.get("app") or "")


def _shot_frame(shot: dict | None) -> str | None:
    return frame_id(shot.get("shot_id")) if shot else None


class McpQueries:
    """MCP 工具用的只读查询。queries 是 v3.store.queries.Queries（或测试里的假对象）；
    semantic：可选的按意思搜（v3.vectors.LineVectors.search），交给 Queries.search。"""

    def __init__(self, queries, *, semantic: Callable[[str, int], list[tuple[int, float]]] | None = None):
        self.q = queries
        self.semantic = semantic

    # ---------------- 工具方法 ----------------

    @staticmethod
    def _hidden(ex: Exclusion, src: dict | None, *titles: str | None) -> bool:
        src = src or {}
        app = src.get("app")
        return any(ex.hides(app, t) for t in (src.get("title"), display_name(src), *titles))

    @staticmethod
    def _has_rules(ex: Exclusion) -> bool:
        return bool(ex.apps or ex.title_keywords)

    # ---------------- Queries 协议 ----------------

    def search(self, query, *, start_ms, end_ms, app, limit, exclude, task=None):
        want = max(1, int(limit)) * (3 if self._has_rules(exclude) else 1)
        res = self.q.search(query, start_ms=start_ms, end_ms=end_ms, app=app or None, limit=min(want, SEARCH_MAX),
                            per_result=5, semantic=self.semantic) or {}
        results = res.get("results") or []
        if res.get("error") and not results:
            raise ToolError(f"没法按这个词搜：{res['error']}（换个说法试试）")
        out: list[Hit] = []
        for r in results:
            src = r.get("source") or {}
            if self._hidden(exclude, src):
                continue
            name, frame, pid, w = display_name(src), _shot_frame(r.get("shot")), piece_id(r.get("visit_id")), where(src)
            a = str(src.get("app") or "")
            hits = r.get("hits") or []
            if not hits:  # 只是标题 / 网址里有这些词
                m = "url" if "url" in (r.get("match") or []) and "title" not in (r.get("match") or []) else "title"
                out.append(Hit(None, (w or name) if m == "url" else name, a, name, int(r.get("last_seen_ms") or 0),
                               frame, r.get("start_ms"), pid, w, None, m))
            for h in hits:
                out.append(Hit(int(h["line_id"]), str(h.get("text") or ""), a, name,
                               int(h.get("last_seen_ms") or r.get("last_seen_ms") or 0), frame, h.get("first_seen_ms"),
                               pid, w, h.get("via"), "text", bool(h.get("semantic"))))
            if len(out) >= limit:
                break
        return out[:limit]

    def timeline(self, *, start_ms, end_ms, app, limit, exclude):
        t = self.q.timeline(start_ms, end_ms, limit=max(500, int(limit))) or {}
        pieces: list[Piece] = []
        gaps: list[GapItem] = []
        for it in t.get("items") or []:
            if it.get("type") == "grey":
                gaps.append(GapItem(int(it["start_ms"]), it.get("end_ms"), str(it.get("reason") or "")))
                continue
            src = it.get("source") or {}
            a = str(src.get("app") or "")
            if (app and a.lower() != app.lower()) or self._hidden(exclude, src, it.get("name")):
                continue
            pieces.append(Piece(piece_id(it["visit_id"]), a, it.get("name") or display_name(src), int(it["start_ms"]),
                                int(it["end_ms"]), where(src), it.get("dwell_ms"), int(it.get("new_lines") or 0),
                                int(it.get("copied") or 0), bool(it.get("open")), bool(it.get("background"))))
        pieces.sort(key=lambda p: (p.start_ms, p.end_ms))
        return (pieces[-limit:] if limit > 0 else []) + gaps

    def summary(self, level, key, *, exclude):
        if level == "piece":
            return self._piece_summary(key, exclude)
        try:
            d = date.fromisoformat(key)
        except ValueError:
            return None
        lo, hi = local_dates_to_utc_range(d, d)
        t = self.q.timeline(lo, hi, limit=5000) or {}
        hours = [h for h in t.get("hours") or [] if h.get("text")]
        if not hours:
            return None
        entries = [it for it in t.get("items") or [] if it.get("type") == "entry"]
        # 每小时一句话说不清用到了哪几条：那天有被隐藏的条目就整份不给
        if any(self._hidden(exclude, it.get("source"), it.get("name")) for it in entries):
            return None
        text = "\n".join(f"{datetime.fromtimestamp(int(h['hour_ms']) / 1000):%H:00} {h['text']}"
                         for h in sorted(hours, key=lambda h: int(h["hour_ms"])))
        apps = tuple(sorted({str((it.get("source") or {}).get("app") or "") for it in entries} - {""}))
        return Summary("day", key, text, lo, hi, apps)

    def _piece_summary(self, key: str, exclude: Exclusion) -> Summary | None:
        vid = parse_piece(key)
        e = self.q.entry(vid) if vid is not None else None
        if not e:
            return None
        ent = e.get("entry") or {}
        src = ent.get("source") or {}
        name = ent.get("name") or display_name(src)
        if self._hidden(exclude, src, name):
            return None
        w = where(src)
        parts = [name]
        if w:
            parts.append(("网址：" if "://" in w else "文件：") + w)
        parts.append(f"看了 {fmt_minutes(ent.get('dwell_ms'))}" + ("（还在看）" if ent.get("open") else "")
                     + ("（后台）" if ent.get("background") else ""))
        parts.append(f"新存 {int(ent.get('new_lines') or 0)} 行" if src.get("kind") != "file"
                     else "本地文件：只记路径和截图，不存字")
        if ent.get("copied"):
            parts.append(f"复制过 {int(ent['copied'])} 次")
        if ent.get("merged"):
            parts.append(f"5 分钟内回来过 {int(ent['merged'])} 次（合成这一条）")
        reasons = [REASON_TEXT.get(r, r) for r in ent.get("reasons") or []]
        if reasons:
            parts.append("为什么拍：" + "、".join(reasons))
        return Summary("piece", f"v{vid}", "\n".join(parts), ent.get("start_ms"), ent.get("end_ms"),
                       (str(src.get("app") or ""),), name)

    def get_frame(self, frame_id, *, exclude):
        sid = parse_frame(frame_id)
        s = self.q.shot(sid) if sid is not None else None
        if not s:
            return None
        src = s.get("source") or {}
        if self._hidden(exclude, src):
            return None
        p = self.q.shot_file(sid)
        if p is None:
            return None
        try:
            data = Path(p).read_bytes()
        except OSError:
            return None
        lines = tuple(LineRect(int(b["line_id"]), str(b.get("text") or ""), int(b["x"]), int(b["y"]), int(b["w"]),
                               int(b["h"])) for b in s.get("boxes") or [])
        return Frame(f"s{sid}", data, int(s.get("width") or 0), int(s.get("height") or 0), str(src.get("app") or ""),
                     display_name(src), int(s.get("t_ms") or 0), lines, where(src))

    @staticmethod
    def _ctx_line(x: dict) -> CtxLine:
        return CtxLine(int(x["line_id"]), str(x.get("text") or ""), int(x.get("first_seen_ms") or 0),
                       int(x.get("last_seen_ms") or 0), str(x.get("via") or "uia"), bool(x.get("new")))

    def context(self, *, line_id, piece_id, before, after, offset, limit, exclude):
        if line_id is not None:
            r = self.q.line_context(int(line_id), before=before, after=after)
            if not r:
                return None
            src = r.get("source") or {}
            if self._hidden(exclude, src):
                return None
            lines = tuple(self._ctx_line(x) for x in r.get("lines") or [])
            first = r.get("first", r.get("offset"))
            return Context(None, str(src.get("app") or ""), display_name(src), int(src.get("first_seen_ms") or 0),
                           int(src.get("last_seen_ms") or 0), lines, int(r.get("total") or len(lines)),
                           None if first is None else int(first), int(line_id), where(src),
                           frame_id(src.get("shot_id")))
        vid = parse_piece(piece_id)
        e = self.q.entry(vid) if vid is not None else None
        if not e:
            return None
        ent = e.get("entry") or {}
        src = ent.get("source") or {}
        name = ent.get("name") or display_name(src)
        if self._hidden(exclude, src, name):
            return None
        rows = e.get("lines") or []
        off = max(0, int(offset))
        page = tuple(self._ctx_line(x) for x in rows[off:off + max(0, int(limit))])
        thumb = ent.get("thumb") or {}
        return Context(f"v{vid}", str(src.get("app") or ""), name, int(ent.get("start_ms") or 0),
                       int(ent.get("end_ms") or 0), page, len(rows), off, None, where(src),
                       frame_id(thumb.get("shot_id")))

    def copied(self, *, query, start_ms, end_ms, limit, exclude):
        want = max(1, int(limit)) * (3 if self._has_rules(exclude) else 1)
        out: list[Copied] = []
        for c in self.q.copied(q=query or None, start_ms=start_ms, end_ms=end_ms, limit=want) or []:
            src = c.get("source") or {"app": c.get("app"), "title": c.get("title")}
            app = str(c.get("app") or src.get("app") or "")
            if self._hidden(exclude, {**src, "app": app}, c.get("title")):
                continue
            out.append(Copied(int(c["clip_id"]), str(c.get("text") or ""), int(c["t_ms"]), app,
                              c.get("title") or display_name(src), where(c.get("source")), piece_id(c.get("visit_id"))))
        return out[:limit]
