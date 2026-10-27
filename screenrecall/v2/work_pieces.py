"""第 4 步：从窗口焦点记录（activity）切片段（pieces）。

规则：同一应用 + 同一标题，中间离开不超过 piece_gap_ms 算同一个片段（离开期间用过别的窗口也一样）；
命中排除规则的焦点记录（excluded = 1）只记时间，永远不成为片段。
片段关联的内容源 = 同应用同标题、存在时间与片段重叠的 segments。
钉住的窗口在后台录下的内容没有焦点记录：按内容源补片段（_background），否则它们够不着"今天"、概括和删除。

增量：游标 worker.piece_activity_id = 已处理完的最后一条已结束记录。仍在进行的记录（end_ms 为空，
最多一条）每次都重新处理，把它所在的片段延长到现在（录制进程没心跳时只到最后一次心跳）。

谁来切：主进程每 30 秒切一次（adapters.LivePieces，"今天"页面一分钟内就能看到正在做的事，进行中的片段
一直延长到现在），空闲整理进程整理时也切一次（主进程没开时的兜底）。两边用同一个游标和同一把跨进程锁
（data_dir/pieces.lock，拿不到就这次不切），不会同时切出两份；_find() 让重跑同一批记录时找回已建的片段，
片段编号不变，整理进程之后写的概括、任务标签都挂在同一个片段上。
"""

from __future__ import annotations

from .work_common import Ctx, FileLock, timed

PIECE_CURSOR = "worker.piece_activity_id"
SEG_CURSOR = "worker.piece_segment_ms"
PIECES_LOCK = "pieces.lock"
BATCH = 5000


def _near(p: dict, start: int, end: int, gap: int) -> bool:
    return start - p["end_ms"] <= gap and end >= p["start_ms"] - gap


class PieceStep:
    name = "pieces"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def backlog(self) -> int:
        cur = self.ctx.meta_get(PIECE_CURSOR)
        return int(self.ctx.conn.execute("SELECT COUNT(*) FROM activity WHERE activity_id > ? AND end_ms IS NOT NULL",
                                          (cur,)).fetchone()[0])

    def run(self) -> int:
        lock = FileLock(self.ctx.data_dir / PIECES_LOCK)
        if not lock.acquire():
            return 0  # 另一个进程正在切，下次再来
        try:
            return self._run()
        finally:
            lock.release()

    def _run(self) -> int:
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
                if new_cur != cur:  # 主进程每 30 秒来一次：没变就不写（写一次就多一个 revision）
                    ctx.meta_put(PIECE_CURSOR, new_cur)
                # 只数新处理完的已结束记录：仍在进行的那条每轮都会重做（延长到现在），不算"有活"，
                # 否则录制进程开着（几乎总有一条进行中的记录）时整理进程永远不会自己退出
                n = sum(1 for r in rows if int(r["activity_id"]) <= new_cur)
                total += n
                st.add(n)
                if open_ids or len(rows) < BATCH:
                    break
            if not ctx.stop.is_set():
                self._background(gap)  # 不算"有活"：钉住的窗口一直在更新，算了整理进程就不会自己退出
        return total

    def _background(self, gap: int) -> None:
        """钉住的窗口在后台也录（帧、文字），但焦点记录只有前台：这些内容源没有片段，"今天"、片段概括、
        搜索结果的片段号、按片段删除都够不着它们。这里按内容源补：没被任何片段盖住的那段时间
        （没关联片段的整段，或最后一个关联片段之后的部分）按同应用同标题并进相邻片段或新建一个。
        游标 worker.piece_segment_ms = 已处理到的内容源 last_seen_ms；重跑幂等（_find 找回已建的片段）。"""
        ctx = self.ctx
        since = ctx.meta_get(SEG_CURSOR)
        rows = ctx.conn.execute(
            "SELECT s.segment_id, s.app, s.title, s.first_seen_ms, s.last_seen_ms, "
            "(SELECT MAX(p.end_ms) FROM piece_segments ps JOIN pieces p ON p.piece_id = ps.piece_id "
            " WHERE ps.segment_id = s.segment_id) AS covered "
            "FROM segments s WHERE s.last_seen_ms > ? AND s.app != '' AND s.title != '' "
            "AND (EXISTS (SELECT 1 FROM lines l WHERE l.segment_id = s.segment_id) "
            "     OR EXISTS (SELECT 1 FROM frames f WHERE f.segment_id = s.segment_id)) "
            "ORDER BY s.last_seen_ms LIMIT ?", (since, BATCH)).fetchall()
        if not rows:
            return
        for r in rows:
            ctx.stop.check()
            app, title, last = r["app"], r["title"], int(r["last_seen_ms"])
            covered = r["covered"]
            lo = int(r["first_seen_ms"]) if covered is None else max(int(r["first_seen_ms"]), int(covered))
            if covered is not None and last <= int(covered):
                continue  # 已经被片段盖住
            # 按真正录到东西的时刻（证据帧、新出现的行）分段：中间超过 gap 没有新内容就是另一个片段，
            # 和前台的规则一样。只看内容源的 first_seen..last_seen 的话，钉了一整天的窗口会变成一个跨过
            # 空闲时间、甚至跨过隐私删除范围的大片段
            for start, end in self._bursts(int(r["segment_id"]), lo, last, gap):
                p = self._find(app, title, start, end, gap)
                if p is not None and _near(p, start, end, gap):
                    if start < p["start_ms"] or end > p["end_ms"]:
                        p["start_ms"], p["end_ms"] = min(p["start_ms"], start), max(p["end_ms"], end)
                    elif covered is not None:
                        continue
                    self._save(app, title, p)  # 没关联过的内容源：存一次就关联上
                else:
                    self._save(app, title, {"piece_id": None, "start_ms": start, "end_ms": max(start, end),
                                            "task_label": None, "dirty": True})
        ctx.meta_put(SEG_CURSOR, int(rows[-1]["last_seen_ms"]))

    def _bursts(self, segment_id: int, lo: int, hi: int, gap: int) -> list[tuple[int, int]]:
        """内容源在 [lo, hi] 里录到东西的时刻（证据帧的截图时间、行第一次出现的时间），间隔不超过 gap 的连成一段。"""
        times = sorted(int(t) for (t,) in self.ctx.conn.execute(
            "SELECT captured_ms FROM frames WHERE segment_id = ? AND captured_ms BETWEEN ? AND ? "
            "UNION SELECT first_seen_ms FROM lines WHERE segment_id = ? AND first_seen_ms BETWEEN ? AND ?",
            (segment_id, lo, hi, segment_id, lo, hi)))
        out: list[tuple[int, int]] = []
        for t in times:
            if out and t - out[-1][1] <= gap:
                out[-1] = (out[-1][0], t)
            else:
                out.append((t, t))
        return out

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
        # 读记录和写片段之间可能发生了隐私删除：写线程里会按删除范围裁掉 / 丢弃（返回 None），不会把删掉的
        # 时段和标题再变回来
        p["piece_id"] = self.ctx.writer.piece_put(p["start_ms"], p["end_ms"], app=app, title=title, segment_ids=segs,
                                                  task_label=p["task_label"], piece_id=p["piece_id"]).result(30)
        p["dirty"] = False
