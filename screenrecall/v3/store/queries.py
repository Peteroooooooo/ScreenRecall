"""Queries：v3 所有只读查询（网页、MCP、空闲整理共用，V3_CONTRACT.md §3.3 / §3.6）。

- 一个只读连接 + 一把锁，线程安全；库文件还不存在时不建库，所有查询返回空结果（库建好后下一次查询自动连上）；
- 每个查询在一个读事务里做完（WAL 快照），几条 SQL 看到的是同一时刻的库；
- 返回的都是能直接转 JSON 的 dict / list（形状见约定 §3.6）。
显示名字：name（大模型起的）> 清理后的标题 > 网址主机名 + 路径 / 文件名 > 软件名。
"""

from __future__ import annotations

import functools
import logging
import ntpath
import shutil
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

from ...store.fts import docs, lexical
from ...timeutil import utc_ms
from ..config import V3Config
from ..types import KIND_APP, PAUSE_FOREVER_MS
from . import schema

log = logging.getLogger("screenrecall.v3.queries")

ALIVE_MS = 10_000                # 心跳这么久内算录制进程在运行
DISK_CHECK_MS = 60_000
UNLISTED_DAYS = 30               # 名单页"最近出现过的未录软件"看多少天
HOUR_MS = 3_600_000
SEARCH_CAP = 20_000              # 一次搜索最多取这么多条命中（最近的在前），常见词也不会拖慢
_GB = 1024 ** 3


# ---------------- 小工具 ----------------

def hour_floor(t_ms: int) -> int:
    """UTC 毫秒所在的本机整点（UTC 毫秒）。"""
    dt = datetime.fromtimestamp(t_ms / 1000).replace(minute=0, second=0, microsecond=0)
    return int(dt.timestamp() * 1000)


def next_hour(hour_ms: int) -> int:
    return hour_floor(hour_ms + HOUR_MS)


def display_name(name: str | None, title: str | None, url: str | None, path: str | None, app: str) -> str:
    """name > 清理后的标题 > 网址主机名 + 路径 / 文件名 > 软件名。"""
    if name:
        return name
    if title:
        return title
    if url and not url.lower().startswith("file:"):
        u = urlsplit(url)
        shown = (u.hostname or "") + (u.path.rstrip("/") if u.path not in ("", "/") else "")
        if shown:
            return shown
    if path:
        return ntpath.basename(path.rstrip("\\/")) or path
    return app


def _ids_json(ids: Iterable[int]) -> str:
    return "[" + ",".join(str(int(x)) for x in ids) + "]"


def _source_dict(r: sqlite3.Row) -> dict:
    return {"source_id": r["source_id"], "kind": r["kind"], "strong": r["kind"] != KIND_APP, "app": r["app"],
            "title": r["title"], "url": r["url"], "path": r["path"],
            "name": display_name(r["name"], r["title"], r["url"], r["path"], r["app"]),
            "first_seen_ms": r["first_seen_ms"], "last_seen_ms": r["last_seen_ms"], "shot_id": r["shot_id"]}


def _line_dict(r: sqlite3.Row) -> dict:
    return {"line_id": r["line_id"], "text": r["text"], "via": r["via"],
            "first_seen_ms": r["first_seen_ms"], "last_seen_ms": r["last_seen_ms"]}


def _shot_dict(r: sqlite3.Row, boxes: list[dict] | None = None) -> dict:
    return {"shot_id": r["shot_id"], "t_ms": r["t_ms"], "kind": r["kind"], "width": r["width"],
            "height": r["height"], "source_id": r["source_id"], "visit_id": r["visit_id"], "boxes": boxes or []}


def _reads(empty: Callable):
    """查询方法的外壳：拿锁、连库（库不存在 → empty(同样的参数)）、在一个读事务里执行。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            with self._lock:
                c = self._db()
                if c is None:
                    return empty(*args, **kwargs)
                c.execute("BEGIN")
                try:
                    return fn(self, c, *args, **kwargs)
                finally:
                    if c.in_transaction:
                        c.execute("COMMIT")
        return wrapper
    return deco


class Queries:
    def __init__(self, cfg: V3Config, *, clock: Callable[[], int] = utc_ms):
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._disk_low = False
        self._disk_checked_ms: int | None = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _db(self) -> sqlite3.Connection | None:
        if self._conn is None:
            if not self.cfg.db_path.exists():
                return None
            try:
                self._conn = schema.connect_ro(self.cfg.db_path)
            except sqlite3.Error:
                return None
        return self._conn

    # ---------------- 公共的拼装 ----------------

    @staticmethod
    def _sources(c: sqlite3.Connection, ids: Iterable[int]) -> dict[int, dict]:
        ids = {int(x) for x in ids if x is not None}
        if not ids:
            return {}
        return {r["source_id"]: _source_dict(r) for r in c.execute(
            "SELECT * FROM sources WHERE source_id IN (SELECT value FROM json_each(?))", (_ids_json(ids),))}

    def _entries(self, c: sqlite3.Connection, rows: list[sqlite3.Row], now: int) -> list[dict]:
        """visits 行 → Entry（开着的条目看了多久算到 now；后台的算到最后一拍 end_ms）。"""
        if not rows:
            return []
        srcs = self._sources(c, (r["source_id"] for r in rows))
        reasons: dict[int, list[str]] = defaultdict(list)
        for r in c.execute("SELECT visit_id, reason, MIN(t_ms) AS t FROM snaps WHERE visit_id IN "
                           "(SELECT value FROM json_each(?)) GROUP BY visit_id, reason ORDER BY visit_id, t",
                           (_ids_json(r["visit_id"] for r in rows),)):
            reasons[r["visit_id"]].append(r["reason"])
        out = []
        for r in rows:
            src = srcs[r["source_id"]]
            is_open = r["open_since_ms"] is not None
            dwell = r["dwell_ms"]
            if is_open:
                dwell += max(0, (r["end_ms"] if r["background"] else now) - r["open_since_ms"])
            if r["shot_id"] is not None:
                thumb = {"shot_id": r["shot_id"], "own": True}
            elif src["strong"] and src["shot_id"] is not None:
                thumb = {"shot_id": src["shot_id"], "own": False}  # 方案里的"已有图"
            else:
                thumb = None
            out.append({"type": "entry", "visit_id": r["visit_id"], "source": src, "name": src["name"],
                        "start_ms": r["start_ms"],
                        "end_ms": max(r["end_ms"], now) if is_open and not r["background"] else r["end_ms"],
                        "open": is_open, "dwell_ms": dwell, "background": bool(r["background"]),
                        "merged": r["merged"], "new_lines": r["new_lines"], "copied": r["copied"],
                        "reasons": reasons.get(r["visit_id"], []), "thumb": thumb})
        return out

    def _clips(self, c: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
        srcs = self._sources(c, (r["source_id"] for r in rows))
        return [{"clip_id": r["clip_id"], "t_ms": r["t_ms"], "text": r["text"], "app": r["app"], "title": r["title"],
                 "source": srcs.get(r["source_id"]), "visit_id": r["visit_id"]} for r in rows]

    @staticmethod
    def _tiers(c: sqlite3.Connection) -> dict[str, str]:
        return {r["process"].lower(): r["tier"] for r in c.execute("SELECT process, tier FROM apps")}

    # ---------------- 控制 / 状态 ----------------

    @_reads(lambda *a, **kw: {})
    def control(self, c) -> dict:
        row = c.execute("SELECT * FROM control WHERE id = 1").fetchone()
        return dict(row) if row else {}

    def _disk_is_low(self, now: int) -> bool:
        if self._disk_checked_ms is None or now - self._disk_checked_ms >= DISK_CHECK_MS:
            self._disk_checked_ms = now
            try:
                self._disk_low = shutil.disk_usage(self.cfg.data_dir).free < self.cfg.min_free_gb * _GB
            except OSError:
                self._disk_low = False
        return self._disk_low

    def status(self, now_ms: int | None = None) -> dict:
        """Status：state = stopped（心跳超过 10 秒或已停）/ paused / locked / recording。
        暂停只看 control.paused_until_ms（录制端的暂停也只由它决定）：刚点继续、录制端心跳还写着 'paused' 的
        那一两秒按 recording 算，按钮马上翻过来。"""
        now = self.clock() if now_ms is None else int(now_ms)
        ctl = self.control()
        hb = ctl.get("heartbeat_ms")
        alive = hb is not None and now - hb <= ALIVE_MS and ctl.get("recorder_state") != "stopped"
        until = ctl.get("paused_until_ms")
        paused = until is not None and until > now
        if not alive:
            state = "stopped"
        elif paused:
            state = "paused"
        elif ctl.get("recorder_state") == "locked":
            state = "locked"
        else:
            state = "recording"
        current = ({"app": ctl["current_app"], "title": ctl.get("current_title") or ""}
                   if state == "recording" and ctl.get("current_app") else None)
        with self._lock:
            disk_low = self._disk_is_low(now) if self.cfg.data_dir.exists() else False
        return {"state": state, "paused_until_ms": until if paused else None,
                "paused_forever": bool(paused and until >= PAUSE_FOREVER_MS), "recorder_alive": alive,
                "last_snap_ms": ctl.get("last_snap_ms"), "now_ms": now, "current": current,
                "background": self._open_background() if state == "recording" else 0,
                "disk_low": disk_low, "agent_enabled": bool(ctl.get("agent_enabled", 1))}

    @_reads(lambda *a, **kw: 0)
    def _open_background(self, c) -> int:
        """开着的后台条目数 = 现在后台在录的窗口数（一直录的窗口；没再拍到 merge_ms 后条目就关了）。"""
        return int(c.execute("SELECT COUNT(*) FROM visits WHERE background = 1 AND open_since_ms IS NOT NULL")
                   .fetchone()[0])

    # ---------------- 时间线 / 条目 / 出处 ----------------

    @_reads(lambda start_ms, end_ms, **kw: {"from_ms": start_ms, "to_ms": end_ms, "items": [], "hours": []})
    def timeline(self, c, start_ms: int, end_ms: int, *, now_ms: int | None = None, limit: int = 500) -> dict:
        """[start_ms, end_ms) 里的条目和灰字，按开始时间从早到晚；有每小时一句话时一起给。
        超过 limit 项时留最近的那些（今天的时间线不能把还开着的那条丢掉）。"""
        now = self.clock() if now_ms is None else int(now_ms)
        limit = max(1, int(limit))
        rng = {"a": int(start_ms), "b": int(end_ms), "n": limit}
        visits = c.execute("SELECT * FROM visits WHERE start_ms < :b AND (end_ms >= :a OR open_since_ms IS NOT NULL) "
                           "ORDER BY start_ms DESC, visit_id DESC LIMIT :n", rng).fetchall()
        greys = c.execute("SELECT * FROM grey_spans WHERE start_ms < :b AND (end_ms IS NULL OR end_ms >= :a) "
                          "ORDER BY start_ms DESC, span_id DESC LIMIT :n", rng).fetchall()
        tiers = self._tiers(c)
        items = self._entries(c, visits, now)
        items += [{"type": "grey", "span_id": g["span_id"], "reason": g["reason"], "app": g["app"],
                   "start_ms": g["start_ms"], "end_ms": g["end_ms"],
                   "tier": tiers.get(g["app"].lower(), "never") if g["app"] else None} for g in greys]
        items.sort(key=lambda it: (it["start_ms"], it["type"] == "entry"))
        hours = [{"hour_ms": r["hour_ms"], "text": r["text"]} for r in c.execute(
            "SELECT hour_ms, text FROM hour_lines WHERE hour_ms >= :a AND hour_ms < :b ORDER BY hour_ms", rng)]
        return {"from_ms": int(start_ms), "to_ms": int(end_ms), "items": items[-limit:], "hours": hours}

    @_reads(lambda *a, **kw: None)
    def entry(self, c, visit_id: int) -> dict | None:
        """一条的详情：这个出处在这一条的时间里看到过的行（new = 第一次出现在这一条里）、这一条的截图、复制。"""
        v = c.execute("SELECT * FROM visits WHERE visit_id = ?", (visit_id,)).fetchone()
        if v is None:
            return None
        e = self._entries(c, [v], self.clock())[0]
        a, b = e["start_ms"], e["end_ms"]
        lines = []
        for r in c.execute(
                "SELECT l.line_id, l.text, l.via, sl.first_seen_ms, sl.last_seen_ms, l.first_seen_ms AS lf "
                "FROM source_lines sl JOIN lines l ON l.line_id = sl.line_id "
                "WHERE sl.source_id = ? AND sl.last_seen_ms >= ? AND sl.first_seen_ms <= ? "
                "ORDER BY sl.first_seen_ms, l.line_id LIMIT 500", (v["source_id"], a, b)):
            d = _line_dict(r)
            d["new"] = a <= r["lf"] <= b and r["first_seen_ms"] == r["lf"]
            lines.append(d)
        shots = [_shot_dict(r) for r in c.execute("SELECT * FROM shots WHERE visit_id = ? ORDER BY t_ms", (visit_id,))]
        clips = self._clips(c, c.execute("SELECT * FROM clips WHERE visit_id = ? ORDER BY t_ms",
                                         (visit_id,)).fetchall())
        return {"entry": e, "lines": lines, "shots": shots, "clips": clips}

    @_reads(lambda *a, **kw: None)
    def source(self, c, source_id: int, *, offset: int = 0, limit: int = 200) -> dict | None:
        srcs = self._sources(c, [source_id])
        if source_id not in srcs:
            return None
        visits = c.execute("SELECT * FROM visits WHERE source_id = ? ORDER BY start_ms DESC LIMIT 50",
                           (source_id,)).fetchall()
        lines = [_line_dict(r) for r in c.execute(
            "SELECT l.line_id, l.text, l.via, sl.first_seen_ms, sl.last_seen_ms FROM source_lines sl "
            "JOIN lines l ON l.line_id = sl.line_id WHERE sl.source_id = ? "
            "ORDER BY sl.first_seen_ms, l.line_id LIMIT ? OFFSET ?",
            (source_id, max(0, int(limit)), max(0, int(offset))))]
        total = c.execute("SELECT COUNT(*) FROM source_lines WHERE source_id = ?", (source_id,)).fetchone()[0]
        return {"source": srcs[source_id], "visits": self._entries(c, visits, self.clock()), "lines": lines,
                "total_lines": int(total)}

    # ---------------- 查找 ----------------

    @_reads(lambda q, **kw: {"query": q, "terms": [], "error": None, "total": 0, "results": [], "copied": []})
    def search(self, c, q: str, *, start_ms: int | None = None, end_ms: int | None = None, app: str | None = None,
               limit: int = 30, per_result: int = 5,
               semantic: Callable[[str, int], list[tuple[int, float]]] | None = None) -> dict:
        """全文搜字 + 标题 + 网址；同一个出处 search_merge_ms 内的命中并成一条；从新到旧。
        semantic(查询, k) → [(line_id, 分数)]：可选的按意思搜，只有它搜到的结果排在后面、按分数从高到低。"""
        p = lexical.plan(q or "")
        out = {"query": q, "terms": [t.text for t in p.terms], "error": None, "total": 0, "results": [],
               "copied": []}
        if p.error:
            out["error"] = p.error
            return out
        if not p.terms:
            out["error"] = ("只有符号的词没法搜：" + "、".join(p.dropped)) if p.dropped else "请输入要搜索的文字。"
            return out
        limit, per_result = max(1, int(limit)), max(1, int(per_result))
        params: dict = {"cap": SEARCH_CAP}
        filt_l, filt_s = ["1"], ["1"]
        if start_ms is not None:
            params["f_from"] = int(start_ms)
            filt_l.append("sl.last_seen_ms >= :f_from")
            filt_s.append("s.last_seen_ms >= :f_from")
        if end_ms is not None:
            params["f_to"] = int(end_ms)
            filt_l.append("sl.first_seen_ms < :f_to")
            filt_s.append("s.first_seen_ms < :f_to")
        if app:
            params["f_app"] = app
            filt_l.append("s.app = :f_app COLLATE NOCASE")
            filt_s.append("s.app = :f_app COLLATE NOCASE")
        line_sel = ("SELECT sl.source_id, l.line_id, l.text, l.via, sl.first_seen_ms, sl.last_seen_ms "
                    "FROM lines l JOIN source_lines sl ON sl.line_id = l.line_id "
                    "JOIN sources s ON s.source_id = sl.source_id ")
        cand = lexical.candidates_sql(p.terms, params)
        verify = lexical.verify_sql(p.terms, params) + lexical.word_prefix_sql(p.terms, params)
        rows = c.execute(f"{line_sel}WHERE l.line_id IN ({cand}) AND {verify} AND {' AND '.join(filt_l)} "
                         "ORDER BY sl.last_seen_ms DESC LIMIT :cap", params).fetchall()

        # 每个出处的"命中事件"：(时间, 命中种类, Hit 或 None, 按意思搜的分数)
        events: dict[int, list[tuple[int, str, dict | None, float]]] = defaultdict(list)
        lexical_ids: set[int] = set()
        for r in rows:
            lexical_ids.add(r["line_id"])
            events[r["source_id"]].append((r["last_seen_ms"], "text", dict(_line_dict(r), semantic=False), 0.0))
        t_title = lexical.verify_sql(p.terms, params, "s.title_norm")
        t_url = lexical.verify_sql(p.terms, params, "replace(sr_fold(COALESCE(s.url, s.path, '')), ' ', '')")
        for r in c.execute(f"SELECT s.source_id, s.last_seen_ms, ({t_title}) AS th, ({t_url}) AS uh FROM sources s "
                           f"WHERE (({t_title}) OR ({t_url})) AND {' AND '.join(filt_s)}", params):
            for kind, hit in (("title", r["th"]), ("url", r["uh"])):
                if hit:
                    events[r["source_id"]].append((r["last_seen_ms"], kind, None, 0.0))
        if semantic is not None:
            # 只认够像的（分数下限 semantic_min_score）、不太短的行（semantic_min_chars）：
            # 短的零碎字（"数据看板"、"冷却中"）和什么都像，它们靠字面搜就够了。数字见 results/search/v3-semantic-*.json
            floor = float(self.cfg.semantic_min_score)
            try:
                found = semantic(q, max(50, limit * per_result * 2)) or []
            except Exception:  # noqa: BLE001 —— 模型缺失 / 加载失败：只用字面命中，不让整个查找失败
                log.warning("按意思搜出错，这次只用字面命中", exc_info=True)
                found = []
            scored = {int(lid): float(s) for lid, s in found if float(s) >= floor and int(lid) not in lexical_ids}
            if scored:
                sp = dict(params, sem=_ids_json(scored), sem_chars=int(self.cfg.semantic_min_chars))
                for r in c.execute(f"{line_sel}WHERE l.line_id IN (SELECT value FROM json_each(:sem)) "
                                   f"AND length(trim(l.text)) >= :sem_chars AND {' AND '.join(filt_l)}", sp):
                    events[r["source_id"]].append((r["last_seen_ms"], "text", dict(_line_dict(r), semantic=True),
                                                   scored[r["line_id"]]))

        merge = self.cfg.search_merge_ms
        clusters: list[tuple[int, list]] = []
        for sid, evs in events.items():
            evs.sort(key=lambda e: e[0], reverse=True)
            cur = [evs[0]]
            for e in evs[1:]:
                if cur[-1][0] - e[0] <= merge:
                    cur.append(e)
                else:
                    clusters.append((sid, cur))
                    cur = [e]
            clusters.append((sid, cur))
        # 有全文 / 标题 / 网址命中的在前，从新到旧；只有按意思搜到的在后，最像的在前
        def order(sc):
            evs = sc[1]
            if any(not (e[2] and e[2]["semantic"]) for e in evs):
                return (1, float(evs[0][0]))
            return (0, max(e[3] for e in evs))

        clusters.sort(key=order, reverse=True)
        out["total"] = len(clusters)
        srcs = self._sources(c, (sid for sid, _ in clusters[:limit]))
        for sid, evs in clusters[:limit]:
            hits = [e[2] for e in evs if e[2] and not e[2]["semantic"]]
            hits += [e[2] for e in sorted((e for e in evs if e[2] and e[2]["semantic"]), key=lambda e: -e[3])]
            uniq = hits  # 一个出处里每行只有一条 source_lines，按意思搜的又去掉了全文已命中的，不会重复
            match = list(dict.fromkeys(e[1] for e in evs))
            last = evs[0][0]
            vrow = c.execute("SELECT visit_id FROM visits WHERE source_id = ? AND start_ms <= ? "
                             "ORDER BY start_ms DESC LIMIT 1", (sid, last)).fetchone()
            out["results"].append({
                "source": srcs[sid], "visit_id": vrow[0] if vrow else None, "start_ms": evs[-1][0], "end_ms": last,
                "last_seen_ms": last, "match": match, "hits": uniq[:per_result],
                "more_hits": max(0, len(uniq) - per_result),
                "shot": self._result_shot(c, srcs[sid], [h["line_id"] for h in uniq])})
        out["copied"] = self._copied(c, q, start_ms, end_ms, 20)
        return out

    def _result_shot(self, c: sqlite3.Connection, src: dict, hit_ids: list[int]) -> dict | None:
        """强标签：出处第一次的那张；弱标签（或那张已过期）：这个出处里包含命中行最多的那张，同样多取最近的。"""
        shot = None
        if src["strong"] and src["shot_id"] is not None:
            shot = c.execute("SELECT * FROM shots WHERE shot_id = ?", (src["shot_id"],)).fetchone()
        if shot is None:
            best = c.execute(
                "SELECT sh.shot_id, COUNT(x.line_id) AS n FROM shots sh LEFT JOIN shot_lines x "
                "ON x.shot_id = sh.shot_id AND x.line_id IN (SELECT value FROM json_each(?)) "
                "WHERE sh.source_id = ? GROUP BY sh.shot_id ORDER BY n DESC, sh.t_ms DESC, sh.shot_id DESC LIMIT 1",
                (_ids_json(hit_ids), src["source_id"])).fetchone()
            if best is None or (hit_ids and best["n"] == 0):
                return None
            shot = c.execute("SELECT * FROM shots WHERE shot_id = ?", (best["shot_id"],)).fetchone()
        boxes = [dict(r) for r in c.execute(
            "SELECT x.line_id, x.x, x.y, x.w, x.h, l.text FROM shot_lines x JOIN lines l ON l.line_id = x.line_id "
            "WHERE x.shot_id = ? AND x.line_id IN (SELECT value FROM json_each(?)) ORDER BY x.y, x.x",
            (shot["shot_id"], _ids_json(hit_ids)))]
        return _shot_dict(shot, boxes)

    def _copied(self, c: sqlite3.Connection, q: str | None, start_ms: int | None, end_ms: int | None,
                limit: int) -> list[dict]:
        where, params = ["1"], {"n": max(1, int(limit))}
        if q and q.strip():
            p = lexical.plan(q)
            terms = [t.text for t in p.terms] if not p.error and p.terms else [docs.norm(q)]
            for i, t in enumerate(x for x in terms if x):
                params[f"q{i}"] = t
                where.append(f"instr(norm, :q{i}) > 0")
        if start_ms is not None:
            params["a"] = int(start_ms)
            where.append("t_ms >= :a")
        if end_ms is not None:
            params["b"] = int(end_ms)
            where.append("t_ms < :b")
        rows = c.execute(f"SELECT * FROM clips WHERE {' AND '.join(where)} ORDER BY t_ms DESC, clip_id DESC LIMIT :n",
                         params).fetchall()
        return self._clips(c, rows)

    @_reads(lambda *a, **kw: [])
    def copied(self, c, *, q: str | None = None, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 100) -> list[dict]:
        """复制过的：从新到旧；q 按 norm 包含。"""
        return self._copied(c, q, start_ms, end_ms, limit)

    # ---------------- 截图 / 上下文 ----------------

    @_reads(lambda *a, **kw: None)
    def shot(self, c, shot_id: int) -> dict | None:
        r = c.execute("SELECT * FROM shots WHERE shot_id = ?", (shot_id,)).fetchone()
        if r is None:
            return None
        boxes = [dict(b) for b in c.execute(
            "SELECT x.line_id, x.x, x.y, x.w, x.h, l.text FROM shot_lines x JOIN lines l ON l.line_id = x.line_id "
            "WHERE x.shot_id = ? ORDER BY x.y, x.x", (shot_id,))]
        d = _shot_dict(r, boxes)
        d["source"] = self._sources(c, [r["source_id"]]).get(r["source_id"])
        return d

    @_reads(lambda *a, **kw: None)
    def shot_file(self, c, shot_id: int) -> Path | None:
        r = c.execute("SELECT path FROM shots WHERE shot_id = ?", (shot_id,)).fetchone()
        if r is None:
            return None
        p = (self.cfg.data_dir / r[0]).resolve()
        root = self.cfg.data_dir.resolve()
        return p if p.is_file() and p.is_relative_to(root) else None

    @_reads(lambda *a, **kw: None)
    def line_context(self, c, line_id: int, *, before: int = 12, after: int = 12) -> dict | None:
        """这一行最近在哪个出处看到的、那个出处的行按 (first_seen, line_id) 排的前后各若干行。"""
        sl = c.execute("SELECT source_id, first_seen_ms FROM source_lines WHERE line_id = ? "
                       "ORDER BY last_seen_ms DESC LIMIT 1", (line_id,)).fetchone()
        if sl is None:
            return None
        sid = sl["source_id"]
        idx = c.execute("SELECT COUNT(*) FROM source_lines WHERE source_id = ? AND (first_seen_ms, line_id) < (?, ?)",
                        (sid, sl["first_seen_ms"], line_id)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM source_lines WHERE source_id = ?", (sid,)).fetchone()[0]
        start = max(0, int(idx) - max(0, int(before)))
        lines = [_line_dict(r) for r in c.execute(
            "SELECT l.line_id, l.text, l.via, sl.first_seen_ms, sl.last_seen_ms FROM source_lines sl "
            "JOIN lines l ON l.line_id = sl.line_id WHERE sl.source_id = ? "
            "ORDER BY sl.first_seen_ms, l.line_id LIMIT ? OFFSET ?",
            (sid, int(idx) - start + max(0, int(after)) + 1, start))]
        return {"source": self._sources(c, [sid])[sid], "lines": lines, "target": int(idx) - start,
                "total": int(total)}

    # ---------------- 名单 ----------------

    @_reads(lambda *a, **kw: {"apps": [], "unlisted": [], "excludes": []})
    def apps(self, c) -> dict:
        """名单 + 最近 30 天出现过、现在还不在"录 / 一直录"里的软件 + 不录规则。"""
        now = self.clock()
        # 按加进来的先后排（改档不改 added_ms）：开关一拨，这一行不会跳到别处
        apps = [dict(r) for r in c.execute("SELECT process, tier, added_ms FROM apps ORDER BY added_ms, process")]
        listed = {a["process"].lower() for a in apps if a["tier"] != "never"}
        unlisted = [dict(r) for r in c.execute(
            "SELECT app, MAX(COALESCE(end_ms, :now)) AS last_ms, SUM(max(0, COALESCE(end_ms, :now) - start_ms)) "
            "AS total_ms FROM grey_spans WHERE reason = 'unlisted' AND app IS NOT NULL AND start_ms >= :since "
            "GROUP BY app COLLATE NOCASE ORDER BY last_ms DESC",
            {"now": now, "since": now - UNLISTED_DAYS * 86_400_000})]
        excludes = [dict(r) for r in c.execute(
            "SELECT exclude_id, kind, value, added_ms FROM excludes ORDER BY kind, exclude_id")]
        return {"apps": apps, "unlisted": [u for u in unlisted if u["app"].lower() not in listed],
                "excludes": excludes}

    # ---------------- 给空闲整理用 ----------------

    @_reads(lambda *a, **kw: [])
    def unnamed_sources(self, c, limit: int = 20) -> list[dict]:
        out = []
        for r in c.execute("SELECT * FROM sources WHERE name IS NULL ORDER BY last_seen_ms DESC LIMIT ?",
                           (max(1, int(limit)),)).fetchall():
            sample = [x[0] for x in c.execute(
                "SELECT l.text FROM source_lines sl JOIN lines l ON l.line_id = sl.line_id WHERE sl.source_id = ? "
                "ORDER BY sl.first_seen_ms, l.line_id LIMIT 20", (r["source_id"],))]
            out.append({"source_id": r["source_id"], "kind": r["kind"], "app": r["app"], "title": r["title"],
                        "url": r["url"], "path": r["path"], "sample": sample})
        return out

    @_reads(lambda *a, **kw: [])
    def hours_without_line(self, c, before_ms: int, limit: int = 24) -> list[int]:
        """有条目、还没写 hour_lines 的本机整点（只要 < before_ms 的完整小时）；最近的 limit 个，从早到晚。"""
        have = {r[0] for r in c.execute("SELECT hour_ms FROM hour_lines")}
        hours: set[int] = set()
        for r in c.execute("SELECT start_ms, end_ms FROM visits WHERE start_ms < ?", (int(before_ms),)):
            h = hour_floor(r[0])
            while h <= r[1]:
                nh = next_hour(h)
                if nh > before_ms:
                    break
                if h not in have:
                    hours.add(h)
                h = nh
        return sorted(hours)[-max(1, int(limit)):]

    @_reads(lambda *a, **kw: [])
    def hour_digest(self, c, hour_ms: int) -> list[dict]:
        """这一小时里的条目：[{name, app, dwell_ms, new_lines, copied}]，按开始时间。"""
        rows = c.execute("SELECT * FROM visits WHERE start_ms < ? AND end_ms >= ? ORDER BY start_ms, visit_id",
                         (next_hour(hour_ms), int(hour_ms))).fetchall()
        return [{"name": e["name"], "app": e["source"]["app"], "dwell_ms": e["dwell_ms"],
                 "new_lines": e["new_lines"], "copied": e["copied"]} for e in self._entries(c, rows, self.clock())]

    @_reads(lambda *a, **kw: [])
    def lines_after(self, c, line_id: int, limit: int = 1000) -> list[tuple[int, str, int, int]]:
        """line_id 之后的行：(line_id, 原文, last_seen_ms, 最近看到它的出处)。给可选的向量用。"""
        return [(r[0], r[1], r[2], r[3]) for r in c.execute(
            "SELECT l.line_id, l.text, l.last_seen_ms, (SELECT sl.source_id FROM source_lines sl "
            "WHERE sl.line_id = l.line_id ORDER BY sl.last_seen_ms DESC LIMIT 1) FROM lines l "
            "WHERE l.line_id > ? ORDER BY l.line_id LIMIT ?", (int(line_id), max(1, int(limit))))]
