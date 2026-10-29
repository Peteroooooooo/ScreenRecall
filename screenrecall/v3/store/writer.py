"""Store：v3 所有写库（V3_CONTRACT.md §3.3）。

一个读写连接 + 一把锁，线程安全；每个写操作一个 BEGIN IMMEDIATE 短事务（多个进程写同一个库靠 busy_timeout 排队）。
时间参数都是 UTC 毫秒；clock 只用来填 added_ms / updated_ms / heartbeat_ms 这类"写的时候是几点"。
截图文件：<数据目录>/shots/YYYY-MM-DD（本机日期）/<uuid hex>.jpg，先写临时名再改名；库里只存相对路径。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator, Sequence

from ...app.paths import ensure_private_dir
from ...store.fts import docs
from ...timeutil import utc_ms
from ..config import TIERS, V3Config
from ..types import SHOT_FIRST, SHOT_VISIT, EncodedShot, LinesOutcome, SourceLabel, TextLine
from . import schema

log = logging.getLogger(__name__)

DISK_CHECK_MS = 60_000           # 硬盘剩余空间最多这么久查一次
_GB = 1024 ** 3
_CHUNK = 500                     # IN (...) 一次最多带这么多个 id

# set_control 能写的字段（id、updated_ms 由这里管）
CONTROL_FIELDS = ("paused_until_ms", "rev", "agent_enabled", "recorder_pid", "recorder_state", "heartbeat_ms",
                  "last_snap_ms", "current_app", "current_title")
EXCLUDE_KINDS = ("host", "title")
SHOT_KINDS = ("first", "visit", "fallback")
GREY_REASONS = ("unlisted", "paused", "locked")


def local_day(t_ms: int) -> str:
    """UTC 毫秒 → 本机日期 YYYY-MM-DD（截图按天分目录）。"""
    return datetime.fromtimestamp(t_ms / 1000).strftime("%Y-%m-%d")


def _ids_json(ids: Sequence[int]) -> str:
    return "[" + ",".join(str(int(x)) for x in ids) + "]"


class Store:
    def __init__(self, cfg: V3Config, *, clock: Callable[[], int] = utc_ms):
        self.cfg = cfg
        self.clock = clock
        self._lock = threading.RLock()
        ensure_private_dir(cfg.data_dir)
        self._conn: sqlite3.Connection | None = schema.connect(cfg.db_path, now_ms=clock())
        self._disk_ok = True
        self._disk_checked_ms: int | None = None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---------------- 事务 ----------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            c = self._conn
            if c is None:
                raise RuntimeError("Store 已关闭")
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise

    def _read(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            if self._conn is None:
                raise RuntimeError("Store 已关闭")
            return self._conn.execute(sql, params).fetchall()

    # ---------------- 建库 / 恢复 ----------------

    def init_defaults(self, record_apps: Sequence[str]) -> bool:
        """第一次（meta 'apps_seeded' 没有）把 record_apps 写成"录"；返回是否第一次。以后以页面上改的为准。"""
        now = self.clock()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM meta WHERE key = 'apps_seeded'").fetchone():
                return False
            c.executemany("INSERT OR IGNORE INTO apps (process, tier, added_ms) VALUES (?, 'record', ?)",
                          [(p.strip().lower(), now) for p in record_apps if p.strip()])
            c.execute("INSERT INTO meta (key, value) VALUES ('apps_seeded', ?)", (str(now),))
            c.execute("UPDATE control SET rev = rev + 1, updated_ms = ? WHERE id = 1", (now,))
            return True

    def recover(self, now_ms: int) -> dict:
        """录制进程启动时收尾上次没关好的：开着的条目在 end_ms 离开；没结束的灰色时段在 max(开始, 最后心跳) 结束。"""
        with self._tx() as c:
            ctl = c.execute("SELECT recorder_state, heartbeat_ms FROM control WHERE id = 1").fetchone()
            hb = ctl["heartbeat_ms"] if ctl else None
            n_vis = c.execute(
                "UPDATE visits SET dwell_ms = dwell_ms + max(0, end_ms - open_since_ms), open_since_ms = NULL "
                "WHERE open_since_ms IS NOT NULL").rowcount
            n_grey = c.execute("UPDATE grey_spans SET end_ms = max(start_ms, COALESCE(?, start_ms)) "
                               "WHERE end_ms IS NULL", (hb,)).rowcount
            state = ctl["recorder_state"] if ctl else None
            c.execute("UPDATE control SET updated_ms = ? WHERE id = 1", (int(now_ms),))
        return {"crashed": state not in (None, "stopped"), "visits": n_vis, "greys": n_grey}

    # ---------------- 出处 / 时间线 ----------------

    def upsert_source(self, label: SourceLabel, t_ms: int) -> tuple[int, bool]:
        """(source_id, 已有截图?)；已有的更新 title / 网址 / 路径 / last_seen。"""
        title_norm = docs.norm(label.title)
        with self._tx() as c:
            row = c.execute("SELECT source_id, shot_id FROM sources WHERE key = ?", (label.key,)).fetchone()
            if row is None:
                cur = c.execute(
                    "INSERT INTO sources (kind, key, app, title, title_norm, url, path, first_seen_ms, last_seen_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (label.kind, label.key, label.app, label.title, title_norm, label.url, label.path, t_ms, t_ms))
                return int(cur.lastrowid), False
            c.execute("UPDATE sources SET app = ?, title = ?, title_norm = ?, url = COALESCE(?, url), "
                      "path = COALESCE(?, path), last_seen_ms = max(last_seen_ms, ?) WHERE source_id = ?",
                      (label.app, label.title, title_norm, label.url, label.path, t_ms, row["source_id"]))
            return int(row["source_id"]), row["shot_id"] is not None

    def visit_enter(self, source_id: int, t_ms: int, *, background: bool = False) -> int:
        """这个出处最新的一条还开着 → 返回它；结束不超过 merge_ms → 合进它（重新打开）；否则新建。
        前台、后台看同一个东西是同一条（一直录的窗口切到前台又切回去，时间线上不出两条并排的）：
        - 前台进来：那条变成前台条目（background = 0）；原来是后台条目时"看了多久"从 0 算（后台的跨度不是在看）；
        - 后台进来、合进的是前台条目：只算回来过一次（merged + 1），不打开（后台这段不算进"看了多久"），
          之后的拍照样 visit_touch 它。"""
        bg = 1 if background else 0
        with self._tx() as c:
            row = c.execute("SELECT visit_id, end_ms, open_since_ms, background FROM visits WHERE source_id = ? "
                            "ORDER BY end_ms DESC, visit_id DESC LIMIT 1", (source_id,)).fetchone()
            if row is not None and (row["open_since_ms"] is not None or t_ms - row["end_ms"] <= self.cfg.merge_ms):
                vid, was_open = int(row["visit_id"]), row["open_since_ms"] is not None
                if not bg and row["background"]:  # 后台条目到了前台：从现在起是前台条目
                    c.execute("UPDATE visits SET background = 0, dwell_ms = 0, open_since_ms = ?, "
                              "merged = merged + ?, end_ms = max(end_ms, ?) WHERE visit_id = ?",
                              (t_ms, 0 if was_open else 1, t_ms, vid))
                elif not was_open:
                    opened = t_ms if bg == row["background"] else None
                    c.execute("UPDATE visits SET open_since_ms = ?, merged = merged + 1, end_ms = max(end_ms, ?) "
                              "WHERE visit_id = ?", (opened, t_ms, vid))
                return vid
            cur = c.execute("INSERT INTO visits (source_id, start_ms, end_ms, open_since_ms, background) "
                            "VALUES (?, ?, ?, ?, ?)", (source_id, t_ms, t_ms, t_ms, bg))
            return int(cur.lastrowid)

    def visit_touch(self, visit_id: int, t_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE visits SET end_ms = max(end_ms, ?) WHERE visit_id = ?", (t_ms, visit_id))

    def visit_leave(self, visit_id: int, t_ms: int, away_ms: int = 0) -> None:
        """开着才算：dwell += max(0, t - open_since - away)；end_ms = max(end_ms, t)；open_since = NULL。
        后台的（一直录的窗口）没有"走开"可扣：dwell 就是这一段从第一拍到最后一拍的跨度。"""
        with self._tx() as c:
            c.execute("UPDATE visits SET dwell_ms = dwell_ms + max(0, ? - open_since_ms - "
                      "CASE WHEN background = 0 THEN ? ELSE 0 END), end_ms = max(end_ms, ?), open_since_ms = NULL "
                      "WHERE visit_id = ? AND open_since_ms IS NOT NULL",
                      (t_ms, max(0, int(away_ms)), t_ms, visit_id))

    def grey_enter(self, reason: str, app: str | None, t_ms: int) -> int:
        """同 reason + app 的上一段还开着 → 返回它；结束不超过 merge_ms → 重新打开那段；否则新建。"""
        if reason not in GREY_REASONS:
            raise ValueError(f"不认识的灰色时段原因：{reason}")
        with self._tx() as c:
            row = c.execute("SELECT span_id, end_ms FROM grey_spans WHERE reason = ? AND app IS ? "
                            "ORDER BY start_ms DESC, span_id DESC LIMIT 1", (reason, app)).fetchone()
            if row is not None and (row["end_ms"] is None or t_ms - row["end_ms"] <= self.cfg.merge_ms):
                c.execute("UPDATE grey_spans SET end_ms = NULL WHERE span_id = ?", (row["span_id"],))
                return int(row["span_id"])
            cur = c.execute("INSERT INTO grey_spans (reason, app, start_ms) VALUES (?, ?, ?)", (reason, app, t_ms))
            return int(cur.lastrowid)

    def grey_leave(self, span_id: int, t_ms: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE grey_spans SET end_ms = max(start_ms, ?) WHERE span_id = ? AND end_ms IS NULL",
                      (t_ms, span_id))

    # ---------------- 字 / 拍 / 图 / 复制 ----------------

    def record_lines(self, source_id: int, lines: Sequence[TextLine], t_ms: int, *,
                     visit_id: int | None = None) -> LinesOutcome:
        """④ 按 docs.norm 全库去重：没有的新建并进全文索引，有的只更新 last_seen；source_lines 新建或更新 last_seen。"""
        items: dict[str, TextLine] = {}
        for ln in lines:
            n = docs.norm(ln.text)
            if n and n not in items:
                items[n] = ln
        if not items:
            return LinesOutcome()
        new_ids: list[int] = []
        seen_ids: list[int] = []
        ids_by_norm: dict[str, int] = {}
        with self._tx() as c:
            fresh = []
            for n, ln in items.items():
                row = c.execute("SELECT line_id FROM lines WHERE norm = ?", (n,)).fetchone()
                if row is None:
                    lid = int(c.execute("INSERT INTO lines (text, norm, via, first_seen_ms, last_seen_ms) "
                                        "VALUES (?, ?, ?, ?, ?)", (ln.text, n, ln.via, t_ms, t_ms)).lastrowid)
                    new_ids.append(lid)
                    fresh.append((lid, ln.text, n, ""))
                else:
                    lid = int(row[0])
                    c.execute("UPDATE lines SET last_seen_ms = max(last_seen_ms, ?) WHERE line_id = ?", (t_ms, lid))
                    seen_ids.append(lid)
                ids_by_norm[n] = lid
            docs.index_rows(c, fresh)
            c.executemany(
                "INSERT INTO source_lines (source_id, line_id, first_seen_ms, last_seen_ms) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (source_id, line_id) DO UPDATE SET last_seen_ms = max(last_seen_ms, excluded.last_seen_ms)",
                [(source_id, lid, t_ms, t_ms) for lid in ids_by_norm.values()])
            if visit_id is not None and new_ids:
                c.execute("UPDATE visits SET new_lines = new_lines + ? WHERE visit_id = ?", (len(new_ids), visit_id))
        return LinesOutcome(tuple(new_ids), tuple(seen_ids), ids_by_norm)

    def add_snap(self, *, t_ms: int, source_id: int, visit_id: int | None, reason: str, via: str,
                 new_lines: int, seen_lines: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "INSERT INTO snaps (t_ms, source_id, visit_id, reason, via, new_lines, seen_lines) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t_ms, source_id, visit_id, reason, via, int(new_lines), int(seen_lines))).lastrowid)

    def _disk_has_room(self) -> bool:
        """硬盘剩不到 cfg.min_free_gb 就不存图；最多 DISK_CHECK_MS 查一次。"""
        now = self.clock()
        if self._disk_checked_ms is None or now - self._disk_checked_ms >= DISK_CHECK_MS:
            self._disk_checked_ms = now
            try:
                free = shutil.disk_usage(self.cfg.data_dir).free
                self._disk_ok = free >= self.cfg.min_free_gb * _GB
            except OSError:
                log.warning("查不到数据目录所在硬盘的剩余空间，照常存图")
                self._disk_ok = True
        return self._disk_ok

    def save_shot(self, *, source_id: int, visit_id: int | None, snap_id: int | None, t_ms: int, kind: str,
                  shot: EncodedShot, ids_by_norm: dict[str, int], replaces: int | None = None) -> int | None:
        """⑤ 存一张截图（文件 + shots 行 + 能对上 line_id 的框）。硬盘不够时返回 None。
        replaces：同一个事务里删掉这张旧图（弱标签这一次换成新字更多的那张；行、框、文件都删，指向它的
        snaps / visits 的 shot_id 跟着置空或换成新的）。"""
        if kind not in SHOT_KINDS:
            raise ValueError(f"不认识的截图种类：{kind}")
        with self._lock:
            if not self._disk_has_room():
                return None
            day = local_day(t_ms)
            rel = f"shots/{day}/{uuid.uuid4().hex}.jpg"
            dest = self.cfg.data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)  # 在私有数据目录里，权限继承过来
            tmp = dest.with_name(f".{dest.name}.tmp")
            try:
                tmp.write_bytes(shot.jpeg)
                os.replace(tmp, dest)
            finally:
                tmp.unlink(missing_ok=True)
            try:
                with self._tx() as c:
                    shot_id = int(c.execute(
                        "INSERT INTO shots (t_ms, source_id, visit_id, kind, path, width, height, bytes) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (t_ms, source_id, visit_id, kind, rel, int(shot.width), int(shot.height),
                         len(shot.jpeg))).lastrowid)
                    # 只登记库里还在的行（同时可能有隐私删除）；同一行出现两次只留第一个框
                    c.executemany("INSERT OR IGNORE INTO shot_lines (shot_id, line_id, x, y, w, h) "
                                  "SELECT ?, line_id, ?, ?, ?, ? FROM lines WHERE line_id = ?",
                                  [(shot_id, int(x), int(y), int(w), int(h), ids_by_norm[n])
                                   for n, (x, y, w, h) in shot.boxes if n in ids_by_norm])
                    if snap_id is not None:
                        c.execute("UPDATE snaps SET shot_id = ? WHERE snap_id = ?", (shot_id, snap_id))
                    if kind == SHOT_FIRST:
                        c.execute("UPDATE sources SET shot_id = ? WHERE source_id = ?", (shot_id, source_id))
                    if visit_id is not None:
                        c.execute("UPDATE visits SET shot_id = ? WHERE visit_id = ? AND (shot_id IS NULL OR ?)",
                                  (shot_id, visit_id, 1 if kind == SHOT_VISIT else 0))
                    old = None
                    if replaces is not None and int(replaces) != shot_id:
                        old = c.execute("SELECT path FROM shots WHERE shot_id = ?", (int(replaces),)).fetchone()
                        c.execute("DELETE FROM shots WHERE shot_id = ?", (int(replaces),))
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
            if old is not None:
                self._unlink_shots([old["path"]])
            return shot_id

    def add_clip(self, *, t_ms: int, text: str, app: str, title: str, source_id: int | None,
                 visit_id: int | None) -> int:
        with self._tx() as c:
            clip_id = int(c.execute(
                "INSERT INTO clips (t_ms, text, norm, source_id, visit_id, app, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (t_ms, text, docs.norm(text), source_id, visit_id, app, title)).lastrowid)
            if visit_id is not None:
                c.execute("UPDATE visits SET copied = copied + 1 WHERE visit_id = ?", (visit_id,))
            return clip_id

    # ---------------- 控制 ----------------

    def control(self) -> dict:
        rows = self._read("SELECT * FROM control WHERE id = 1")
        return dict(rows[0]) if rows else {}

    def _write_control(self, fields: dict) -> None:
        bad = sorted(set(fields) - set(CONTROL_FIELDS))
        if bad:
            raise ValueError(f"control 里没有这些字段：{', '.join(bad)}")
        fields = dict(fields, updated_ms=self.clock())
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE control SET {cols} WHERE id = 1", fields)

    def set_control(self, **fields) -> None:
        """写任意 control 字段（paused_until_ms、agent_enabled …），updated_ms 自动。"""
        self._write_control(fields)

    def heartbeat(self, **fields) -> None:
        """heartbeat_ms = 现在，并写给出的字段（recorder_state、last_snap_ms、current_app …）。"""
        self._write_control(dict(fields, heartbeat_ms=self.clock()))

    def _bump_rev(self, c: sqlite3.Connection) -> None:
        c.execute("UPDATE control SET rev = rev + 1, updated_ms = ? WHERE id = 1", (self.clock(),))

    def set_app_tier(self, process: str, tier: str) -> None:
        process = (process or "").strip().lower()
        if not process:
            raise ValueError("进程名不能为空")
        if tier not in TIERS:
            raise ValueError(f"不认识的档：{tier}")
        with self._tx() as c:
            c.execute("INSERT INTO apps (process, tier, added_ms) VALUES (?, ?, ?) "
                      "ON CONFLICT (process) DO UPDATE SET tier = excluded.tier", (process, tier, self.clock()))
            self._bump_rev(c)

    def app_tiers(self) -> dict[str, str]:
        return {r["process"].lower(): r["tier"] for r in self._read("SELECT process, tier FROM apps")}

    def url_apps(self) -> set[str]:
        """出过网址出处的软件（进程名小写）：录制进程据此认"这个软件的页面靠网址认"。"""
        return {str(r[0]).lower() for r in self._read("SELECT DISTINCT app FROM sources WHERE kind = 'url'")}

    def alive(self, *, visits: Sequence[int] = (), sources: Sequence[int] = (),
              spans: Sequence[int] = ()) -> tuple[set[int], set[int], set[int]]:
        """给出的条目 / 出处 / 灰色时段里还在库里的（隐私删除之后录制进程对一遍内存状态）。"""
        def have(table: str, col: str, ids) -> set[int]:
            ids = sorted({int(x) for x in ids if x is not None})
            if not ids:
                return set()
            return {int(r[0]) for r in self._read(
                f"SELECT {col} FROM {table} WHERE {col} IN (SELECT value FROM json_each(?))", (_ids_json(ids),))}
        return have("visits", "visit_id", visits), have("sources", "source_id", sources),             have("grey_spans", "span_id", spans)

    def add_exclude(self, kind: str, value: str) -> int:
        """不录的网站（host，存小写）或窗口标题关键词（title）。已有同样的就返回它的 id。"""
        if kind not in EXCLUDE_KINDS:
            raise ValueError(f"不认识的不录规则：{kind}")
        value = (value or "").strip()
        if kind == "host":
            value = value.lower().rstrip(".")
        if not value:
            raise ValueError("不录规则不能为空")
        with self._tx() as c:
            c.execute("INSERT OR IGNORE INTO excludes (kind, value, added_ms) VALUES (?, ?, ?)",
                      (kind, value, self.clock()))
            row = c.execute("SELECT exclude_id FROM excludes WHERE kind = ? AND value = ?", (kind, value)).fetchone()
            self._bump_rev(c)
            return int(row[0])

    def remove_exclude(self, exclude_id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM excludes WHERE exclude_id = ?", (exclude_id,))
            self._bump_rev(c)

    def excludes(self) -> list[dict]:
        return [dict(r) for r in self._read(
            "SELECT exclude_id, kind, value, added_ms FROM excludes ORDER BY kind, exclude_id")]

    # ---------------- 维护 ----------------

    def _unlink_shots(self, paths: Sequence[str]) -> None:
        dirs = set()
        for rel in paths:
            p = self.cfg.data_dir / rel
            try:
                p.unlink(missing_ok=True)
                dirs.add(p.parent)
            except OSError:
                log.warning("删不掉一张截图文件（可能正被打开），下次再试")
        for d in dirs:  # 当天目录空了就删掉
            try:
                d.rmdir()
            except OSError:
                pass

    def expire_shots(self, before_ms: int) -> int:
        """删 t_ms < before_ms 的截图（文件 + 行，框跟着删）；字留着。返回张数。"""
        with self._tx() as c:
            rows = c.execute("SELECT shot_id, path FROM shots WHERE t_ms < ?", (before_ms,)).fetchall()
            c.execute("DELETE FROM shots WHERE t_ms < ?", (before_ms,))
        self._unlink_shots([r["path"] for r in rows])
        return len(rows)

    def delete_range(self, start_ms: int, end_ms: int) -> dict:
        """隐私删除 [start_ms, end_ms)：这段里开始的条目 / 拍 / 截图（含文件）/ 复制 / 灰色时段、第一次在这段里看到的
        出处-行关系；再删掉没人引用的行（连同全文索引文档）和既没条目又没行的出处，以及和这段重叠的每小时一句话。
        meta 'delete_rev' + 1（录制进程据此丢掉指向删掉的东西的内存状态）。"""
        rng = (int(start_ms), int(end_ms))
        out: dict[str, int] = {}
        with self._tx() as c:
            vids = [int(r[0]) for r in c.execute(
                "SELECT visit_id FROM visits WHERE start_ms >= ? AND start_ms < ?", rng)]
            touched: set[int] = set()
            touched.update(int(r[0]) for r in c.execute(
                "SELECT DISTINCT source_id FROM visits WHERE start_ms >= ? AND start_ms < ?", rng))
            # 截图：这段里拍的 + 属于要删的条目的
            shot_rows = c.execute(
                "SELECT shot_id, path, source_id FROM shots WHERE (t_ms >= ? AND t_ms < ?) "
                "OR visit_id IN (SELECT value FROM json_each(?))", (*rng, _ids_json(vids))).fetchall()
            paths = [r["path"] for r in shot_rows]
            touched.update(int(r["source_id"]) for r in shot_rows)
            c.execute("DELETE FROM shots WHERE shot_id IN (SELECT value FROM json_each(?))",
                      (_ids_json([r["shot_id"] for r in shot_rows]),))
            out["shots"] = len(shot_rows)
            n_snaps = c.execute("SELECT COUNT(*) FROM snaps WHERE (t_ms >= ? AND t_ms < ?) "
                                "OR visit_id IN (SELECT value FROM json_each(?))",
                                (*rng, _ids_json(vids))).fetchone()[0]
            c.execute("DELETE FROM snaps WHERE t_ms >= ? AND t_ms < ?", rng)
            out["visits"] = c.execute("DELETE FROM visits WHERE start_ms >= ? AND start_ms < ?", rng).rowcount
            out["snaps"] = int(n_snaps)  # 条目删掉时它的拍跟着级联删
            out["clips"] = c.execute("DELETE FROM clips WHERE t_ms >= ? AND t_ms < ?", rng).rowcount
            out["grey_spans"] = c.execute("DELETE FROM grey_spans WHERE start_ms >= ? AND start_ms < ?",
                                          rng).rowcount
            sl = c.execute("SELECT source_id, line_id FROM source_lines WHERE first_seen_ms >= ? AND first_seen_ms < ?",
                           rng).fetchall()
            touched.update(int(r[0]) for r in sl)
            c.execute("DELETE FROM source_lines WHERE first_seen_ms >= ? AND first_seen_ms < ?", rng)
            out["source_lines"] = len(sl)
            # 没有出处再引用的行：先按原文档删全文索引，再删行（框跟着级联删）
            cand = sorted({int(r[1]) for r in sl})
            orphans: list = []
            for i in range(0, len(cand), _CHUNK):
                orphans += c.execute(
                    "SELECT line_id, text, norm FROM lines WHERE line_id IN (SELECT value FROM json_each(?)) "
                    "AND NOT EXISTS (SELECT 1 FROM source_lines s WHERE s.line_id = lines.line_id)",
                    (_ids_json(cand[i:i + _CHUNK]),)).fetchall()
            docs.unindex_rows(c, [(int(r[0]), r[1], r[2], "") for r in orphans])
            c.execute("DELETE FROM lines WHERE line_id IN (SELECT value FROM json_each(?))",
                      (_ids_json([r[0] for r in orphans]),))
            out["lines"] = len(orphans)
            # 既没条目又没行的出处（只看这次碰到的，免得删掉别的进程刚建、还没来得及建条目的出处）
            dead = [int(r[0]) for r in c.execute(
                "SELECT source_id FROM sources WHERE source_id IN (SELECT value FROM json_each(?)) "
                "AND NOT EXISTS (SELECT 1 FROM visits v WHERE v.source_id = sources.source_id) "
                "AND NOT EXISTS (SELECT 1 FROM source_lines s WHERE s.source_id = sources.source_id)",
                (_ids_json(sorted(touched)),))]
            more = c.execute("SELECT path FROM shots WHERE source_id IN (SELECT value FROM json_each(?))",
                             (_ids_json(dead),)).fetchall()
            paths += [r[0] for r in more]
            out["shots"] += len(more)
            out["sources"] = c.execute("DELETE FROM sources WHERE source_id IN (SELECT value FROM json_each(?))",
                                       (_ids_json(dead),)).rowcount
            out["hour_lines"] = c.execute("DELETE FROM hour_lines WHERE hour_ms < ? AND hour_ms + 3600000 > ?",
                                          (rng[1], rng[0])).rowcount
            # 告诉录制进程删过了（control 线程每秒看一次）：它内存里指向删掉的条目、候选图要丢掉
            c.execute("INSERT INTO meta (key, value) VALUES ('delete_rev', '1') "
                      "ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")
        self._unlink_shots(paths)
        with self._lock:
            try:  # 把 WAL 里的旧页面也写回、截断（有读者时做不完，下次检查点会做）
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
        return out

    def set_source_name(self, source_id: int, name: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE sources SET name = ?, name_ms = ? WHERE source_id = ?",
                      ((name or "").strip() or None, self.clock(), source_id))

    def put_hour_line(self, hour_ms: int, text: str, model: str | None) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO hour_lines (hour_ms, text, model, made_ms) VALUES (?, ?, ?, ?)",
                      (int(hour_ms), text, model, self.clock()))

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        rows = self._read("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0][0] if rows else default

    def meta_put(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
