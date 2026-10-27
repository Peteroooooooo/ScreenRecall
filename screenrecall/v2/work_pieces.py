"""第 4 步：从窗口焦点记录（activity）切片段（pieces）。

规则：同一应用 + 同一标题，中间离开不超过 piece_gap_ms 算同一个片段（离开期间用过别的窗口也一样）；
命中排除规则的焦点记录（excluded = 1）只记时间，永远不成为片段。
片段关联的内容源 = 同应用同标题、存在时间与片段重叠的 segments。

增量：游标 worker.piece_activity_id = 已处理完的最后一条已结束记录。仍在进行的记录（end_ms 为空，
最多一条）每次都重新处理，把它所在的片段延长到现在（录制进程没心跳时只到最后一次心跳）。
"""

from __future__ import annotations

from .work_common import Ctx, timed

PIECE_CURSOR = "worker.piece_activity_id"
BATCH = 5000


def _near(p: dict, start: int, end: int, gap: int) -> bool:
    return start - p["end_ms"] <= gap and end >= p["start_ms"] - gap


class PieceStep:
    name = "pieces"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def backlog(self) -> int:
        cur = self.ctx.meta_get(PIECE_CURSOR)
        return int(self.ctx.conn.execute("SELECT COUNT(*) FROM activity WHERE activity_id > ?",
                                          (cur,)).fetchone()[0])

    def run(self) -> int:
        ctx = self.ctx
        conn = ctx.conn
        gap = int(ctx.cfg.idle.piece_gap_ms)
        total = 0
        with timed(ctx, self.name) as st:
            while not ctx.stop.is_set():
                cur = ctx.meta_get(PIECE_CURSOR)
                rows = conn.execute("SELECT * FROM activity WHERE activity_id > ? ORDER BY activity_id LIMIT ?",
                                    (cur, BATCH)).fetchall()
                if not rows:
                    break
                now = ctx.clock()
                hb = int(ctx.control().get("recorder_heartbeat_ms") or 0)
                open_ids = [int(r["activity_id"]) for r in rows if r["end_ms"] is None]
                # 游标只推进到第一条还没结束的记录之前
                new_cur = (min(open_ids) - 1) if open_ids else int(rows[-1]["activity_id"])
                pieces: dict[tuple[str, str], dict] = {}
                for r in sorted(rows, key=lambda x: (x["start_ms"], x["activity_id"])):
                    if r["excluded"]:
                        continue
                    app, title = r["process"] or "", r["title"] or ""
                    if not app and not title:
                        continue
                    start = int(r["start_ms"])
                    end = int(r["end_ms"]) if r["end_ms"] is not None else min(now, max(start, hb))
                    end = max(start, end)
                    key = (app, title)
                    p = pieces.get(key)
                    if p is None or not _near(p, start, end, gap):
                        found = self._find(app, title, start, end, gap)
                        if found is not None:
                            if p is not None and p["dirty"]:
                                self._save(app, title, p)
                            p = found
                    if p is not None and _near(p, start, end, gap):
                        if start < p["start_ms"] or end > p["end_ms"]:
                            p["start_ms"], p["end_ms"] = min(p["start_ms"], start), max(p["end_ms"], end)
                            p["dirty"] = True
                    else:
                        if p is not None and p["dirty"]:
                            self._save(app, title, p)  # 上一个片段结束了：先存，再开新的
                        p = {"piece_id": None, "start_ms": start, "end_ms": end, "task_label": None, "dirty": True}
                    pieces[key] = p
                for (app, title), p in pieces.items():
                    if p["dirty"]:
                        self._save(app, title, p)
                ctx.meta_put(PIECE_CURSOR, new_cur)
                n = len(rows)
                total += n
                st.add(n)
                if open_ids or len(rows) < BATCH:
                    break
        return total

    def _find(self, app: str, title: str, start: int, end: int, gap: int) -> dict | None:
        """库里与这条记录相邻（间隔不超过 gap）的同应用同标题片段（重跑同一批记录时找回已建的片段）。"""
        r = self.ctx.conn.execute(
            "SELECT piece_id, start_ms, end_ms, task_label FROM pieces WHERE app = ? AND title = ? "
            "AND end_ms >= ? AND start_ms <= ? ORDER BY end_ms DESC LIMIT 1",
            (app, title, start - gap, end + gap)).fetchone()
        if r is None:
            return None
        return {"piece_id": int(r[0]), "start_ms": int(r[1]), "end_ms": int(r[2]), "task_label": r[3],
                "dirty": False}

    def _save(self, app: str, title: str, p: dict) -> None:
        segs = [int(r[0]) for r in self.ctx.conn.execute(
            "SELECT segment_id FROM segments WHERE app = ? AND title = ? AND last_seen_ms >= ? AND first_seen_ms <= ?",
            (app, title, p["start_ms"], p["end_ms"]))]
        p["piece_id"] = self.ctx.writer.piece_put(p["start_ms"], p["end_ms"], app=app, title=title, segment_ids=segs,
                                                  task_label=p["task_label"], piece_id=p["piece_id"]).result(30)
        p["dirty"] = False
