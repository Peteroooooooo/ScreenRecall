"""第 4 步：清理截图、保留期、空间上限（设计 9 "清理截图"；隐私与删除页"保留多久"）。

1. 多余的旧截图：一张证据帧连着的每一行，后面（截图时间更晚）另一张留着的帧（status = ok）里也有 → 删掉旧帧。
   不删的：没连到任何行的帧（可能是只有图的画面）、还有待识别 / 识别失败区域的帧、标了"保留"（keep）的帧、
   存下不到 dedupe_min_age_s 的帧（录制进程可能还在往上面登记行）。每一行最后出现的那张帧永远留着
   （它后面没有更晚的帧连着这一行），所以每一行至少还在一张图里。
2. 截图保留 image_days 天（0 = 一直保留）：本地那天零点之前的截图删掉，文字留着，仍能搜到。keep 的不删。
3. 文字保留 text_days 天（0 = 一直保留）：本地那天零点之前的记录按隐私删除的语义整段删掉
   （store.delete.delete_data：文字、全文索引、截图、焦点与缺口、片段、片段概括、向量），
   那之前几天的天 / 小时概括整行删掉。
4. 空间上限 storage_cap_gb（0 = 不限）：数据库 + 截图 + 向量超过上限时，从最旧的截图删起（keep 的不删）。
   主进程每 cap_check_s 秒也查一次（读几个数，很便宜；main.StorageCap），超了当场按同样的规则删。

删截图崩溃安全：一个事务里先把帧标成 'deleted'（所有查询只认 'ok'，它不再是证据）并删掉它的 line_in_frame /
ocr_regions，再抹掉文件（先覆盖零再删）、删图片向量，最后删行；中途被杀，下次从 status = 'deleted' 的行接着做。
向量删不掉（向量库被占用）时 id 记在 <数据目录>/vector_deletes_pending.worker.json，下次先补删。
主进程和整理进程用同一把锁（<数据目录>/frames.lock）：同一时间只有一处在清理。日志只记数量。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from ..store import schema
from ..store import delete as store_delete
from ..store.vectors import VectorStore
from .work_common import Ctx, FileLock, log, timed

FRAMES_LOCK = "frames.lock"
PENDING_FILE = "vector_deletes_pending.worker.json"
BATCH = 500
GB = 1024 ** 3
DAY_MS = 86_400_000


def local_midnight_ms(now_ms: int, days_back: int) -> int:
    """本地时间 days_back 天前那天的零点（UTC 毫秒）。保留 N 天 = 删掉这个时刻之前的。"""
    d = datetime.fromtimestamp(now_ms / 1000).date() - timedelta(days=int(days_back))
    return int(datetime.combine(d, dtime.min).timestamp() * 1000)


def _in(ids: Sequence) -> str:
    return ", ".join("?" * len(ids))


# ---------------- 占用 ----------------

def dir_bytes(root: Path) -> int:
    total = 0
    stack = [Path(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def db_bytes(data_dir: Path) -> int:
    db = schema.db_path(data_dir)
    return sum(p.stat().st_size for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")) if p.exists())


def usage_bytes(conn, data_dir: Path) -> int:
    """空间上限按这个算：数据库文件 + 截图（库里记的每张字节数，不用逐个看文件）+ 向量目录。"""
    frames = int(conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM frames").fetchone()[0])
    return db_bytes(data_dir) + frames + dir_bytes(Path(data_dir) / "vectors")


# ---------------- 向量补删清单 ----------------

class VectorPending:
    """没删掉的向量 id（只有编号，没有内容）。持有 frames.lock 时才读写。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / PENDING_FILE
        self.ids: dict[str, set[int]] = {"text": set(), "image": set()}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in self.ids:
                    self.ids[k].update(int(x) for x in raw.get(k, ()) if isinstance(x, int))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("读取待补删向量清单失败", exc_info=True)

    def add(self, kind: str, ids: Iterable[int]) -> None:
        self.ids[kind].update(int(i) for i in ids)

    def flush(self) -> int:
        """删掉记下的向量；返回还剩几个没删掉。"""
        from .searchsvc import image_vectors_dir, text_vectors_dir

        for kind, root in (("text", text_vectors_dir(self.data_dir)), ("image", image_vectors_dir(self.data_dir))):
            ids = self.ids[kind]
            if not ids:
                continue
            manifest = root / "manifest.json"
            if not manifest.is_file():
                ids.clear()  # 没有向量库：没有要删的
                continue
            try:
                dim = int(json.loads(manifest.read_text(encoding="utf-8"))["dim"])
                vs = VectorStore(root, dim)
                try:
                    vs.delete(sorted(ids))
                finally:
                    vs.close()
                ids.clear()
            except Exception as exc:  # noqa: BLE001 - 下次再补
                log.warning("删除%s向量失败，下次再试：%s", kind, type(exc).__name__)
        self._save()
        return sum(len(v) for v in self.ids.values())

    def _save(self) -> None:
        try:
            if any(self.ids.values()):
                from ..app.paths import write_private_file

                write_private_file(self.path, json.dumps({k: sorted(v) for k, v in self.ids.items()}).encode("utf-8"))
            else:
                self.path.unlink(missing_ok=True)
        except OSError:
            log.warning("保存待补删向量清单失败", exc_info=True)


# ---------------- 写线程里执行的 SQL ----------------

def _mark_deleted(conn, frame_ids: list[str]) -> int:
    """（事务之外）把帧标成 deleted 并删掉它的行位置、待识别区域；keep 的、已经不是 ok / failed 的跳过。"""
    conn.execute("PRAGMA secure_delete = ON")
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for s in range(0, len(frame_ids), BATCH):
            part = frame_ids[s:s + BATCH]
            ids = [r[0] for r in conn.execute(
                f"SELECT frame_id FROM frames WHERE frame_id IN ({_in(part)}) AND keep = 0 "
                "AND status IN ('ok', 'failed')", part)]
            if not ids:
                continue
            ph = _in(ids)
            conn.execute(f"DELETE FROM line_in_frame WHERE frame_id IN ({ph})", ids)
            conn.execute(f"DELETE FROM ocr_regions WHERE frame_id IN ({ph})", ids)
            conn.execute(f"UPDATE frames SET status = 'deleted' WHERE frame_id IN ({ph})", ids)
            n += len(ids)
        if n:
            schema.bump_revision(conn)  # 开着的页面据此刷新（证据条少了图）
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return n


def _drop_rows(conn, frame_ids: list[str]) -> int:
    conn.execute("PRAGMA secure_delete = ON")
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for s in range(0, len(frame_ids), BATCH):
            part = frame_ids[s:s + BATCH]
            ph = _in(part)
            # 标记之后别的进程又登记了行位置（极少见）：一起删
            conn.execute(f"DELETE FROM line_in_frame WHERE frame_id IN ({ph})", part)
            conn.execute(f"DELETE FROM ocr_regions WHERE frame_id IN ({ph})", part)
            n += conn.execute(f"DELETE FROM frames WHERE frame_id IN ({ph}) AND status = 'deleted'", part).rowcount
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    store_delete.checkpoint(conn, tries=1)  # 旧页面别留在 -wal 里；有读者占着就算了（下次删除时再做）
    return n


def _drop_old_period_summaries(conn, cutoff: int) -> int:
    """文字到期删掉之后，那几天的天 / 小时概括（已被清空标 stale）整行删掉。"""
    n = 0
    for sid, level, key, start, end in conn.execute(
            "SELECT summary_id, level, key, start_ms, end_ms FROM summaries WHERE level IN ('hour', 'day')").fetchall():
        span = store_delete.period_span(level, key)
        end = span[1] if span else end
        if end is not None and int(end) <= cutoff:
            conn.execute("DELETE FROM summaries WHERE summary_id = ?", (sid,))
            n += 1
    return n


def _noop(_conn) -> None:
    return None


# ---------------- 清理 ----------------

class FrameCleanup:
    """清理截图 + 保留期 + 空间上限。整理进程每一轮调 run()；主进程定时调 enforce_cap()。"""

    name = "frames"

    def __init__(self, ctx: Ctx, retention=None):
        self.ctx = ctx
        self._retention = retention  # 测试用；None = 现读配置（页面改了保留期，下一轮就用新的）

    @property
    def retention(self):
        if self._retention is not None:
            return self._retention
        from .config import apply_user_settings

        return apply_user_settings(self.ctx.cfg).retention

    # ---------- 对外 ----------

    def run(self) -> int:
        """返回删了多少项（截图 + 到期的文字行）；拿不到锁（主进程正在清）返回 0。"""
        lock = FileLock(self.ctx.data_dir / FRAMES_LOCK)
        if not lock.acquire():
            return 0
        try:
            with timed(self.ctx, self.name) as st:
                ret = self.retention
                pending = VectorPending(self.ctx.data_dir)
                done = self._resume(pending)
                if ret.text_days > 0:
                    done += self._expire_text(ret, pending, st)
                if ret.image_days > 0:
                    done += self._expire_images(ret, pending, st)
                if ret.dedupe_frames:
                    done += self._dedupe(ret, pending, st)
                if ret.storage_cap_gb > 0:
                    done += self._cap(ret, pending, st)
                pending.flush()
                st.add(done)
                return done
        finally:
            lock.release()

    def enforce_cap(self) -> int:
        """（主进程）只做空间上限这一项；没超或拿不到锁返回 0。"""
        ret = self.retention
        if ret.storage_cap_gb <= 0:
            return 0
        if usage_bytes(self.ctx.conn, self.ctx.data_dir) <= ret.storage_cap_gb * GB:
            return 0
        lock = FileLock(self.ctx.data_dir / FRAMES_LOCK)
        if not lock.acquire():
            return 0
        try:
            pending = VectorPending(self.ctx.data_dir)
            n = self._resume(pending) + self._cap(ret, pending, self.ctx.stat(self.name))
            pending.flush()
            return n
        finally:
            lock.release()

    def candidates(self, min_age_s: float | None = None, limit: int = BATCH) -> list[str]:
        """多余的旧截图（见模块说明第 1 条），最旧的在前。"""
        ret = self.retention
        age = ret.dedupe_min_age_s if min_age_s is None else min_age_s
        old = self.ctx.clock() - int(age * 1000)
        return [r[0] for r in self.ctx.conn.execute(
            "SELECT f.frame_id FROM frames f WHERE f.status = 'ok' AND f.keep = 0 AND f.captured_ms < ? "
            "AND EXISTS (SELECT 1 FROM line_in_frame x WHERE x.frame_id = f.frame_id) "
            "AND NOT EXISTS (SELECT 1 FROM ocr_regions r WHERE r.frame_id = f.frame_id "
            "                AND r.status IN ('pending', 'running', 'failed')) "
            "AND NOT EXISTS (SELECT 1 FROM line_in_frame a WHERE a.frame_id = f.frame_id AND NOT EXISTS ("
            "     SELECT 1 FROM line_in_frame b JOIN frames g ON g.frame_id = b.frame_id "
            "     WHERE b.line_id = a.line_id AND g.status = 'ok' AND (g.captured_ms > f.captured_ms "
            "           OR (g.captured_ms = f.captured_ms AND g.frame_no > f.frame_no)))) "
            "ORDER BY f.captured_ms, f.frame_no LIMIT ?", (old, int(limit)))]

    # ---------- 各项 ----------

    def _delete_frames(self, frame_ids: list[str], pending: VectorPending) -> tuple[int, int]:
        """标记 → 抹文件 → 删图片向量 → 删行。返回 (删了几张, 释放的字节)。"""
        if frame_ids:
            self.ctx.writer.run_raw(_mark_deleted, list(frame_ids)).result(120)
        return self._finish_marked(pending)

    def _finish_marked(self, pending: VectorPending) -> tuple[int, int]:
        conn = self.ctx.conn
        rows = conn.execute("SELECT frame_id, frame_no, image_path, bytes FROM frames WHERE status = 'deleted' "
                            "ORDER BY frame_no LIMIT 5000").fetchall()
        if not rows:
            return 0, 0
        freed = 0
        for r in rows:
            try:
                store_delete.wipe_file(self.ctx.data_dir / r["image_path"])
            except OSError as exc:  # 被占用（正在看这张图）：行留着 'deleted'，下次再删文件
                log.warning("截图文件删除失败，下次再试：%s", type(exc).__name__)
                continue
            freed += int(r["bytes"] or 0)
        gone = [r for r in rows if not (self.ctx.data_dir / r["image_path"]).exists()]
        pending.add("image", [int(r["frame_no"]) for r in gone])
        pending.flush()  # 图片向量没删掉时编号已记在清单里：行照样删（补删只要编号）
        n =self.ctx.writer.run_raw(_drop_rows, [r["frame_id"] for r in gone]).result(120) if gone else 0
        return n, freed

    def _resume(self, pending: VectorPending) -> int:
        """上次标了 deleted、没做完的（被杀、文件被占用）接着做。"""
        return self._finish_marked(pending)[0]

    def _expire_text(self, ret, pending: VectorPending, st) -> int:
        ctx = self.ctx
        now = ctx.clock()
        cutoff = local_midnight_ms(now, ret.text_days)
        conn = ctx.conn
        has_old = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM lines WHERE first_seen_ms < ?) OR EXISTS (SELECT 1 FROM frames WHERE "
            "captured_ms < ?) OR EXISTS (SELECT 1 FROM activity WHERE start_ms < ?) OR EXISTS (SELECT 1 FROM gaps "
            "WHERE start_ms < ?) OR EXISTS (SELECT 1 FROM pieces WHERE start_ms < ?) OR EXISTS (SELECT 1 FROM skims "
            "WHERE start_ms < ?)", (cutoff,) * 6).fetchone()[0]
        old_sums = any(
            (sp := store_delete.period_span(level, key)) is not None and sp[1] <= cutoff
            for level, key in conn.execute("SELECT level, key FROM summaries WHERE level IN ('hour', 'day')"))
        if not has_old and not old_sums:
            return 0
        ctx.stop.check()
        n = 0
        if has_old:
            rep = ctx.writer.run_raw(lambda c: store_delete.delete_data(
                c, ctx.data_dir, time_to=cutoff, now_ms=now,
                keep_recent_segments_ms=int(ctx.writer.cfg.segment_reuse_ms))).result(600)
            pending.add("text", list(rep.line_ids) + list(rep.chunk_ids))
            pending.add("image", rep.frame_nos)
            n = rep.lines + rep.frames
            st.extra["text_lines"] = st.extra.get("text_lines", 0) + rep.lines
            st.extra["text_frames"] = st.extra.get("text_frames", 0) + rep.frames
            log.info("文字保留 %d 天：删除了 %d 行文字、%d 张截图", ret.text_days, rep.lines, rep.frames)
        ctx.writer.call(_drop_old_period_summaries, cutoff).result(60)  # 同时加 revision
        return n

    def _expire_images(self, ret, pending: VectorPending, st) -> int:
        ctx = self.ctx
        cutoff = local_midnight_ms(ctx.clock(), ret.image_days)
        total = 0
        while not ctx.stop.is_set():
            ids = [r[0] for r in ctx.conn.execute(
                "SELECT frame_id FROM frames WHERE captured_ms < ? AND keep = 0 AND status IN ('ok', 'failed') "
                "ORDER BY captured_ms LIMIT ?", (cutoff, BATCH))]
            if not ids:
                break
            n, freed = self._delete_frames(ids, pending)
            total += n
            st.extra["expired"] = st.extra.get("expired", 0) + n
            st.extra["bytes_freed"] = st.extra.get("bytes_freed", 0) + freed
            if n == 0:
                break  # 都删不掉（文件被占用）：下次再说
        if total:
            log.info("截图保留 %d 天：删除了 %d 张", ret.image_days, total)
        return total

    def _dedupe(self, ret, pending: VectorPending, st) -> int:
        ctx = self.ctx
        total = 0
        while not ctx.stop.is_set():
            ids = self.candidates()
            if not ids:
                break
            n, freed = self._delete_frames(ids, pending)
            total += n
            st.extra["redundant"] = st.extra.get("redundant", 0) + n
            st.extra["bytes_freed"] = st.extra.get("bytes_freed", 0) + freed
            if n == 0:
                break
        if total:
            log.info("清理截图：删除了 %d 张多余的旧截图", total)
        return total

    def _cap(self, ret, pending: VectorPending, st) -> int:
        ctx = self.ctx
        cap = int(ret.storage_cap_gb * GB)
        over = usage_bytes(ctx.conn, ctx.data_dir) - cap
        total = 0
        while over > 0 and not ctx.stop.is_set():
            ids, need = [], over
            for fid, nbytes in ctx.conn.execute(
                    "SELECT frame_id, COALESCE(bytes, 0) FROM frames WHERE keep = 0 AND status IN ('ok', 'failed') "
                    "ORDER BY captured_ms, frame_no LIMIT ?", (BATCH,)):
                ids.append(fid)
                need -= int(nbytes)
                if need <= 0:
                    break
            if not ids:
                ctx.once("cap-text", "空间上限比文字和向量占的还小：截图已经删完了，还是超出上限")
                break
            n, freed = self._delete_frames(ids, pending)
            total += n
            st.extra["cap"] = st.extra.get("cap", 0) + n
            st.extra["bytes_freed"] = st.extra.get("bytes_freed", 0) + freed
            if n == 0:
                break
            over -= freed
        if total:
            log.info("空间上限 %.1f GB：从最旧的删了 %d 张截图", ret.storage_cap_gb, total)
        return total
