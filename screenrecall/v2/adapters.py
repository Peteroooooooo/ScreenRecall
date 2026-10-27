"""主进程把 v2 存储接到外壳上：MCP 的 Queries、托盘的 Controller、Agent 策略、隐私删除。

- StoreStack：主进程里的一套存储对象（写线程 Writer、只读 Queries、检索服务、控制行缓存、删除器），
  网页 API（v2.web）、MCP、托盘共用；
- StoreQueriesAdapter：实现 app.mcp_server.Queries（Hit / Piece / Summary / Frame），"对 Agent 隐藏"名单
  作为 exclude_apps / exclude_title_keywords 交给 SQL（数量限制之前就排除）；
- StoreController：实现 app.main.Controller（暂停 / 继续 / 删除最近 N 分钟 / 托盘状态），状态有短缓存；
- agent_policy()：AgentPolicy 每次调用工具时现读 control 行（1 秒缓存）。

小时 / 天概括的约定（空闲整理进程写 summaries.refs 时遵守）：
refs 是 JSON 列表，元素可以是 {"piece": id}（引用的片段）、{"app": "x.exe"} / {"apps": [...]}（涉及的应用）、
{"hidden": {"apps": [...], "title_keywords": [...]}}（生成时已排除的"对 Agent 隐藏"名单）。
当前隐藏名单非空时，只有 refs 里的 hidden 名单覆盖了当前名单的概括才给 Agent；否则当作没有概括。
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ..app import mcp_server as ms
from ..app import tray
from ..store import delete as store_delete
from ..store.fts import docs
from ..store.queries import Hidden, Queries
from ..store.vectors import VectorStore
from ..store.writer import Writer
from ..timeutil import utc_ms
from .searchsvc import SearchService, image_vectors_dir, text_vectors_dir

log = logging.getLogger("screenrecall.v2.adapters")
PENDING_VECTOR_DELETES = "vector_deletes_pending.json"


# ---------------- 小工具（网页 API 也用） ----------------

def effective_state(ctrl: dict, now_ms: int) -> tuple[str, int | None]:
    """control 行 → ("recording" | "paused" | "stopped", pause_until_ms)。暂停到点了按没暂停算。"""
    until = ctrl.get("pause_until_ms")
    if ctrl.get("desired_state") == "paused" and (until is None or int(until) > now_ms):
        return "paused", None if until is None else int(until)
    if not recorder_running(ctrl):
        return "stopped", None
    return "recording", None


def recorder_running(ctrl: dict) -> bool:
    """心跳新鲜（15 秒内）且录制进程没有报告自己已停止（正常退出时最后一次心跳写 stopped）。"""
    return bool(ctrl.get("recorder_alive")) and ctrl.get("recorder_state") != "stopped"


def hidden_lists(ctrl: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    h = ctrl.get("hidden_from_agents") or {}
    return tuple(x for x in h.get("apps", ()) if x), tuple(x for x in h.get("title_keywords", ()) if x)


def latest_frames(conn, line_ids: Sequence[int]) -> dict[int, str]:
    """每行最近的一张（status = ok）证据帧。"""
    ids = sorted({int(x) for x in line_ids if x is not None})
    if not ids:
        return {}
    out: dict[int, str] = {}
    for lid, fid in conn.execute(
        "SELECT lf.line_id, lf.frame_id FROM line_in_frame lf JOIN frames f ON f.frame_id = lf.frame_id "
        "WHERE f.status = 'ok' AND lf.line_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
        "ORDER BY f.captured_ms, f.frame_no", (json.dumps(ids),)):
        out[int(lid)] = fid
    return out


def piece_of(conn, segment_id: int | None, t_ms: int | None) -> tuple[int, str | None] | None:
    """内容源在 t_ms 时所属的片段（piece_id, task_label）：优先时间上包含 t 的，否则时间最近的。"""
    if segment_id is None:
        return None
    t = int(t_ms or 0)
    r = conn.execute(
        "SELECT p.piece_id, p.task_label FROM piece_segments ps JOIN pieces p ON p.piece_id = ps.piece_id "
        "WHERE ps.segment_id = ? ORDER BY (CASE WHEN ? BETWEEN p.start_ms AND p.end_ms THEN 0 ELSE 1 END), "
        "MIN(ABS(p.start_ms - ?), ABS(p.end_ms - ?)) LIMIT 1", (int(segment_id), t, t, t)).fetchone()
    return None if r is None else (int(r[0]), r[1])


# ---------------- control 行缓存 ----------------

class ControlCache:
    """Queries.control() 的短缓存（托盘每 5 秒、Agent 每次调用工具、网页轮询都要读）。"""

    def __init__(self, queries: Queries, ttl_s: float = 1.0, clock: Callable[[], float] = time.monotonic):
        self.queries, self.ttl_s, self._clock = queries, ttl_s, clock
        self._value: dict | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._value is None or now - self._at >= self.ttl_s:
                self._value = self.queries.control()
                self._at = now
            return dict(self._value)

    def invalidate(self) -> None:
        with self._lock:
            self._value = None


def agent_policy(control: ControlCache, **kw: Any) -> ms.AgentPolicy:
    def enabled() -> bool:
        return bool(control.get().get("agent_enabled"))

    def exclusion() -> ms.Exclusion:
        apps, kws = hidden_lists(control.get())
        return ms.Exclusion(apps, kws)

    return ms.AgentPolicy(enabled=enabled, exclusion=exclusion, **kw)


# ---------------- 隐私删除 ----------------

class Deleter:
    """真删：数据库（经主进程写线程，delete.delete_data）+ 证据图 + 文字 / 图片向量。

    向量在数据库提交之后删；向量库被占用（例如空闲整理进程正在压缩）删不掉时记下来，下次删除或 retry() 时补删。
    """

    def __init__(self, writer: Writer, data_dir: Path | str, *, keep_recent_segments_ms: int = 30 * 60_000,
                 on_deleted: Callable[[], None] | None = None, clock: Callable[[], int] = utc_ms,
                 vector_retries: int = 3, retry_sleep_s: float = 0.5):
        self.writer, self.data_dir = writer, Path(data_dir)
        self.keep_recent_segments_ms = keep_recent_segments_ms
        self.on_deleted, self.clock = on_deleted, clock
        self.vector_retries, self.retry_sleep_s = vector_retries, retry_sleep_s
        self._pending: dict[str, set[int]] = {"text": set(), "image": set()}
        self._lock = threading.Lock()  # 删除一次一个
        self._load_pending()

    # 没删掉的向量 id 记在数据目录里（只有 id，没有内容）：主进程在补删成功前退出，下次启动接着删
    @property
    def pending_path(self) -> Path:
        return self.data_dir / PENDING_VECTOR_DELETES

    def _load_pending(self) -> None:
        try:
            raw = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("读取待补删向量清单失败", exc_info=True)
            return
        if isinstance(raw, dict):
            for kind in self._pending:
                self._pending[kind].update(int(x) for x in raw.get(kind, ()) if isinstance(x, int))

    def _save_pending(self) -> None:
        try:
            if any(self._pending.values()):
                from ..app.paths import write_private_file

                data = {k: sorted(v) for k, v in self._pending.items()}
                write_private_file(self.pending_path, json.dumps(data).encode("utf-8"))
            else:
                self.pending_path.unlink(missing_ok=True)
        except OSError:
            log.warning("保存待补删向量清单失败", exc_info=True)

    def delete(self, *, time_from: int | None = None, time_to: int | None = None, app: str | None = None,
               timeout_s: float = 600.0) -> dict:
        with self._lock:
            now = self.clock()
            fut = self.writer.run_raw(lambda c: store_delete.delete_data(
                c, self.data_dir, time_from=time_from, time_to=time_to, app=app or None, now_ms=now,
                keep_recent_segments_ms=self.keep_recent_segments_ms))
            rep = fut.result(timeout_s)
            out = rep.to_dict()
            self._pending["text"].update(rep.line_ids)
            self._pending["image"].update(rep.frame_nos)
            out["text_vectors"], out["image_vectors"] = self._flush_vectors()
            self._save_pending()
            out["vectors_pending"] = sum(len(v) for v in self._pending.values())
        if self.on_deleted:
            self.on_deleted()
        return out

    def retry(self) -> int:
        """补删上次没删掉的向量；返回还剩多少。"""
        with self._lock:
            if any(self._pending.values()):
                self._flush_vectors()
                self._save_pending()
            return sum(len(v) for v in self._pending.values())

    def _flush_vectors(self) -> tuple[int, int]:
        counts = []
        for kind, root in (("text", text_vectors_dir(self.data_dir)), ("image", image_vectors_dir(self.data_dir))):
            ids = self._pending[kind]
            n = 0
            if ids:
                manifest = root / "manifest.json"
                if not manifest.is_file():
                    ids.clear()  # 没有向量库：没有要删的
                else:
                    for attempt in range(self.vector_retries):
                        try:
                            dim = int(json.loads(manifest.read_text(encoding="utf-8"))["dim"])
                            vs = VectorStore(root, dim)
                            try:
                                n = vs.delete(sorted(ids))
                            finally:
                                vs.close()
                            ids.clear()
                            break
                        except Exception as exc:  # noqa: BLE001 - 稍后重试，数据库那边已经删了
                            log.warning("删除%s向量失败（第 %d 次）：%s", kind, attempt + 1, type(exc).__name__)
                            time.sleep(self.retry_sleep_s)
            counts.append(n)
        return counts[0], counts[1]


# ---------------- 第一次使用 ----------------

CONTROL_SEEDED = "control_seeded"        # meta：主进程已写过第一次的默认值（开始录制）
RECORDER_DEFAULTS = "recorder_defaults_ms"  # meta：录制进程已填过默认排除规则（v2/recorder._recover）


def _seed_control(conn, now: int, default_procs: list, default_kws: list) -> bool:
    """（写线程里）第一次使用：desired_state 改为 recording、空的排除名单填默认值，记 control_seeded。

    新库的期望状态默认是 paused；不在这里改成 recording，新装的程序永远不会开始录。
    录制进程先在这个库上跑过（单独运行、测试）也算第一次：它只填排除名单（记 recorder_defaults_ms、
    request_seq + 1），从不改期望状态。
    这个标记出现之前建的库：request_seq 只算录制进程那一次（或 0）才当第一次；更多说明主进程或页面写过，
    只补记标记，不改用户的选择。"""
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (CONTROL_SEEDED,)).fetchone() is not None:
        return False
    r = conn.execute("SELECT request_seq, excluded_processes, excluded_title_keywords FROM control WHERE id = 1"
                     ).fetchone()
    seq = int(r[0] or 0)
    by_recorder = conn.execute("SELECT 1 FROM meta WHERE key = ?", (RECORDER_DEFAULTS,)).fetchone() is not None
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (CONTROL_SEEDED, str(int(now))))
    if seq > (1 if by_recorder else 0):
        return False  # 旧库，已经有人写过
    procs = json.loads(r[1] or "[]") or list(default_procs)
    kws = json.loads(r[2] or "[]") or list(default_kws)
    conn.execute("UPDATE control SET desired_state = 'recording', pause_until_ms = NULL, excluded_processes = ?, "
                 "excluded_title_keywords = ?, request_seq = request_seq + 1, updated_ms = ? WHERE id = 1",
                 (json.dumps(procs, ensure_ascii=False), json.dumps(kws, ensure_ascii=False), int(now)))
    return True


# ---------------- 主进程的一套存储对象 ----------------

class StoreStack:
    def __init__(self, cfg, *, search: SearchService | None = None, clock: Callable[[], int] = utc_ms):
        self.cfg = cfg
        self.data_dir = Path(cfg.data_dir)
        self.clock = clock
        self.writer = Writer(self.data_dir, cfg.writer, clock=clock)
        self.queries = Queries(self.data_dir, clock=clock)
        enc = cfg.encoder
        self.search = search or SearchService(self.data_dir, idle_unload_s=enc.idle_unload_s, threads=enc.threads,
                                              batch_size=enc.batch_size, clock=clock)
        self.control = ControlCache(self.queries)
        self.deleter = Deleter(self.writer, self.data_dir,
                               keep_recent_segments_ms=cfg.writer.segment_reuse_ms,
                               on_deleted=self.control.invalidate, clock=clock,
                               # 向量库的文件锁一次最多等 30 秒：请求里只试一次（删除页面不用等 90 秒），
                               # 没删掉的记在待补删清单里，主进程每 2 秒补删一次
                               vector_retries=1)

    def seed_control(self) -> bool:
        """第一次用这个数据目录：开始录制，空的排除名单写入默认规则。只做一次（meta control_seeded），
        之后用户暂停了，重启也保持暂停。返回这次是否写了。"""
        done = self.writer.call(_seed_control, self.clock(), list(self.cfg.default_excluded_processes),
                                list(self.cfg.default_excluded_title_keywords)).result(10)
        self.control.invalidate()
        return bool(done)

    def set_control(self, **fields: Any) -> None:
        self.writer.control_set(**fields).result(10)
        self.control.invalidate()

    def close(self) -> None:
        try:
            self.writer.close()
        finally:
            self.search.close()
            self.queries.close()


# ---------------- MCP 的 Queries ----------------

def _hit_time(x: dict) -> int:
    return int(x.get("last_seen_ms") or 0)


@dataclass
class StoreQueriesAdapter:
    """app.mcp_server.Queries 的实现。"""
    queries: Queries
    search_service: SearchService
    max_image_edge: int = ms.MAX_IMAGE_EDGE
    summary_chars: int = 300

    @staticmethod
    def _ex(exclude: ms.Exclusion) -> dict:
        return {"exclude_apps": tuple(exclude.apps), "exclude_title_keywords": tuple(exclude.title_keywords)}

    def search(self, query, *, start_ms, end_ms, app, task, limit, exclude) -> list[ms.Hit]:
        res = self.search_service.search(query, time_from=start_ms, time_to=end_ms, app=app, task=task,
                                         limit=max(1, int(limit)), per_group=3, **self._ex(exclude))
        conn = self.queries.conn()
        rows = [(g, ln) for g in res["groups"] for ln in g["lines"]]
        rows.sort(key=lambda r: -_hit_time(r[1]))
        rows = rows[:max(1, int(limit))]
        frames = latest_frames(conn, [ln["line_id"] for _, ln in rows])
        pieces: dict[tuple[int, int], tuple[int, str | None] | None] = {}
        hidden = Hidden(exclude.apps, exclude.title_keywords)
        out = []
        for g, ln in rows:
            key = (int(g["segment_id"]), _hit_time(ln))
            if key not in pieces:
                p = piece_of(conn, g["segment_id"], _hit_time(ln))
                if p is not None and hidden:
                    # 所属片段命中隐藏名单（片段标题 / 任务名可能就是要藏的内容）：不报片段号和任务名
                    row = conn.execute("SELECT app, title FROM pieces WHERE piece_id = ?", (p[0],)).fetchone()
                    if row is None or hidden.match(row["app"], row["title"]):
                        p = None
                pieces[key] = p
            p = pieces[key]
            out.append(ms.Hit(line_id=int(ln["line_id"]), text=ln["text"], app=g["app"], title=g["title"],
                              seen_ms=_hit_time(ln), frame_id=frames.get(int(ln["line_id"])),
                              first_seen_ms=ln.get("first_seen_ms"), piece_id=None if p is None else str(p[0]),
                              task=None if p is None else p[1], score=g.get("score")))
        return out

    def timeline(self, *, start_ms, end_ms, app, limit, exclude) -> list[ms.Piece]:
        tl = self.queries.timeline(start_ms, end_ms, app, **self._ex(exclude))
        items = [x for x in tl["items"] if x["kind"] == "piece"][:max(1, int(limit))]
        ids = [int(x["id"]) for x in items]
        withheld = _pieces_touching_hidden(self.queries.conn(), ids, Hidden(exclude.apps, exclude.title_keywords))
        texts = self._piece_summaries([i for i in ids if i not in withheld])
        return [ms.Piece(piece_id=str(x["id"]), app=x["app"] or "", title=x["title"] or "", start_ms=x["start_ms"],
                         end_ms=x["end_ms"], task=x["task_label"], summary=texts.get(int(x["id"])))
                for x in items]

    def _piece_summaries(self, ids: Sequence[int]) -> dict[int, str]:
        if not ids:
            return {}
        rows = self.queries.conn().execute(
            "SELECT key, text FROM summaries WHERE level = 'piece' AND status = 'ok' AND text != '' "
            "AND key IN (SELECT CAST(value AS TEXT) FROM json_each(?))", (json.dumps([str(i) for i in ids]),))
        return {int(k): t[:self.summary_chars] for k, t in rows}

    def summary(self, level, key, *, exclude) -> ms.Summary | None:
        conn = self.queries.conn()
        if level == "piece":
            try:
                pid = int(key)
            except (TypeError, ValueError):
                return None
            s = self.queries.summary("piece", str(pid), **self._ex(exclude))
            p = conn.execute("SELECT app, title, start_ms, end_ms FROM pieces WHERE piece_id = ?", (pid,)).fetchone()
            if s is None or p is None or s["status"] != "ok" or not s["text"]:
                return None
            if _pieces_touching_hidden(conn, [pid], Hidden(exclude.apps, exclude.title_keywords)):
                return None
            return ms.Summary("piece", str(pid), s["text"], p["start_ms"], p["end_ms"],
                              tuple(a for a in (p["app"],) if a), p["title"])
        if level not in ("day", "hour"):
            return None
        s = self.queries.summary(level, key)
        if s is None or s["status"] != "ok" or not s["text"]:
            return None
        refs = s.get("refs") or []
        hidden = Hidden(exclude.apps, exclude.title_keywords)
        if hidden and not _generated_without(refs, hidden):
            return None
        apps = _summary_apps(conn, refs)
        start, end = s.get("start_ms"), s.get("end_ms")
        if start is None or end is None:
            span = store_delete.period_span(level, key)
            if span:
                start, end = span
        return ms.Summary(level, key, s["text"], start, end, tuple(apps))

    def get_frame(self, frame_id, *, exclude) -> ms.Frame | None:
        fr = self.queries.frame(frame_id, **self._ex(exclude))
        if fr is None or fr["status"] != "ok":
            return None
        path = Path(fr["path"])
        try:
            data = path.read_bytes()
        except OSError:
            return None
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
        w0, h0 = im.size
        scale = min(1.0, self.max_image_edge / max(w0, h0)) if max(w0, h0) else 1.0
        if scale < 1.0:
            im = im.resize((max(1, round(w0 * scale)), max(1, round(h0 * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=1)
        lines = []
        for ln in fr["lines"]:
            x0, y0, x1, y1 = (float(v) * scale for v in ln["rect"])
            lines.append(ms.LineRect(int(ln["line_id"]), ln["text"], round(x0), round(y0),
                                     max(1, round(x1 - x0)), max(1, round(y1 - y0))))
        app, title = fr["app"], fr["title"]
        if (not app or title is None) and fr.get("segment_id") is not None:
            seg = self.queries.conn().execute("SELECT app, title FROM segments WHERE segment_id = ?",
                                              (fr["segment_id"],)).fetchone()
            if seg is not None:
                app, title = app or seg["app"], title if title is not None else seg["title"]
        return ms.Frame(fr["frame_id"], buf.getvalue(), im.width, im.height, app or "", title or "",
                        int(fr["captured_ms"]), tuple(lines))


def _pieces_touching_hidden(conn, piece_ids: Sequence[int], hidden: Hidden) -> set[int]:
    """片段里有命中隐藏名单的内容源：这些片段的概括不给 Agent。

    片段本身（应用 + 标题）没命中时，它关联的某个内容源仍可能命中（例如同一应用里标题含隐藏关键词的那一页）；
    片段概括是按整段内容写的，没法把那一部分拆出去，所以整条不给。"""
    if not hidden or not piece_ids:
        return set()
    rows = conn.execute(
        "SELECT ps.piece_id, s.app, s.title FROM piece_segments ps JOIN segments s ON s.segment_id = ps.segment_id "
        "WHERE ps.piece_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
        (json.dumps([int(i) for i in piece_ids]),))
    return {int(pid) for pid, app, title in rows if hidden.match(app, title)}


def _generated_without(refs: list, hidden: Hidden) -> bool:
    """概括生成时排除的名单是否覆盖当前的隐藏名单。"""
    for r in refs:
        if isinstance(r, dict) and isinstance(r.get("hidden"), dict):
            h = r["hidden"]
            apps = {docs.fold(a) for a in h.get("apps", ()) if isinstance(a, str) and a}
            kws = {docs.fold(k) for k in h.get("title_keywords", ()) if isinstance(k, str) and k}
            return hidden.apps <= apps and set(hidden.keywords) <= kws
    return False


def _summary_apps(conn, refs: list) -> list[str]:
    apps: list[str] = []
    pieces: list[int] = []
    for r in refs:
        if not isinstance(r, dict):
            continue
        if isinstance(r.get("app"), str):
            apps.append(r["app"])
        if isinstance(r.get("apps"), list):
            apps.extend(a for a in r["apps"] if isinstance(a, str))
        for k in ("piece", "piece_id"):
            try:
                if r.get(k) is not None:
                    pieces.append(int(r[k]))
            except (TypeError, ValueError):
                pass
    if pieces:
        apps.extend(a for (a,) in conn.execute(
            "SELECT DISTINCT app FROM pieces WHERE app IS NOT NULL AND piece_id IN "
            "(SELECT CAST(value AS INTEGER) FROM json_each(?))", (json.dumps(pieces),)))
    return list(dict.fromkeys(a for a in apps if a))


# ---------------- 托盘的 Controller ----------------

@dataclass
class StoreController:
    """app.main.Controller：写 control 行的期望状态；删除是真删。status() 有缓存，托盘每 5 秒调一次。"""
    stack: StoreStack
    cache_s: float = 2.0
    _status: tray.TrayStatus | None = field(default=None, init=False, repr=False)
    _status_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def pause_until(self, until: datetime | None) -> None:
        ms_ = None if until is None else int(until.timestamp() * 1000)
        self.stack.set_control(desired_state="paused", pause_until_ms=ms_)
        self._invalidate()

    def resume(self) -> None:
        self.stack.set_control(desired_state="recording", pause_until_ms=None)
        self._invalidate()

    def delete_recent(self, minutes: int) -> None:
        now = self.stack.clock()
        rep = self.stack.deleter.delete(time_from=now - int(minutes) * 60_000)
        log.info("已删除最近 %d 分钟：%d 行文字、%d 张证据图", minutes, rep.get("lines", 0), rep.get("frames", 0))
        self._invalidate()

    def tick(self) -> None:
        """定时调用：暂停到点了把期望状态改回录制（录制进程只看 desired_state 也不会一直停着）；补删向量。"""
        ctrl = self.stack.control.get()
        until = ctrl.get("pause_until_ms")
        if ctrl.get("desired_state") == "paused" and until is not None and int(until) <= self.stack.clock():
            self.stack.set_control(desired_state="recording", pause_until_ms=None)
            self._invalidate()
        self.stack.deleter.retry()

    def _invalidate(self) -> None:
        with self._lock:
            self._status = None

    def status(self) -> tray.TrayStatus:
        with self._lock:
            now = time.monotonic()
            if self._status is not None and now - self._status_at < self.cache_s:
                return self._status
        try:
            ctrl = self.stack.control.get()
        except Exception:  # noqa: BLE001 - 托盘不能因为读库失败而崩
            log.warning("读取录制状态失败", exc_info=True)
            return tray.TrayStatus("读取状态失败")
        state, until = effective_state(ctrl, self.stack.clock())
        if state == "paused":
            text = "已暂停" + (f"到 {datetime.fromtimestamp(until / 1000):%m-%d %H:%M}" if until else "")
            st = tray.TrayStatus(text, paused=True)
        elif state == "stopped":
            st = tray.TrayStatus("录制进程没有运行")
        else:
            proc = ctrl.get("current_process")
            st = tray.TrayStatus(f"正在录制：{proc}" if proc else "正在录制")
        with self._lock:
            self._status, self._status_at = st, time.monotonic()
        return st
