"""本机网页界面的 JSON API（替代 Streamlit 页面 app.py）：python -m screenrecall.webapi [--data-dir D] [--port 8600]

只做"把已有模块的结果交给前端"，不重写检索、生命周期、采集控制的逻辑：搜索走 retrieval.run，证据浏览走
search.frame_detail + evidence.context，收藏 / 删除走 lifecycle（删除保留"先看计划、再带上计划确认"两步），
录制控制只写 control 表的请求字段（后台负责执行与回写）。前端是 web/ 下的 React 单页，构建产物 web/dist 由本服务托管。

安全：只绑定 127.0.0.1，没有登录鉴权（方案 6.3：只录受控测试内容）。为防止别的网页借浏览器向本机发请求，
所有请求都核对 Host（防 DNS rebinding）；会改数据的请求（非 GET）还必须带自定义头 X-ScreenRecall: 1，且 Origin
（如有）必须是本服务自己——跨站页面不能在不触发预检的情况下设置自定义头，本服务也不开放 CORS。
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import re
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import classify, control, db, evidence, i18n, importer, lifecycle, models, retrieval, retry, rules, search, \
    storage, timeutil
from .config import Config, load_config

WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
FRAME_ID = re.compile(r"^[0-9a-f]{32}$")
RANK_BASIS = {
    "time": "最新匹配时间",
    "bm25": "全文相关性",
    "terms": "命中片段数",
    "rrf": "词法与语义排名融合（RRF）",
    "image": "画面描述与整图的相似度",
}
STAGE_NAME = {"ocr": "文字识别（OCR）", "text_embed": "语义向量", "image_embed": "全图向量"}
SOURCE_LABELS = {"manual": "补拍", "import": "导入", "live": "自动"}
NO_AUTH_NOTICE = "页面只在本机访问（127.0.0.1），没有登录鉴权。请只录制受控的测试内容。"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_FILES = 500


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


# ---------------- 序列化 ----------------

def _plain(o: Any) -> Any:
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: _plain(getattr(o, f.name)) for f in dataclasses.fields(o)}
    if isinstance(o, sqlite3.Row):
        return {k: _plain(o[k]) for k in o.keys()}
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (set, frozenset)):
        return sorted(_plain(v) for v in o)
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, Path):
        return str(o)
    return o


def _json(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(_plain(data), status_code=status)


# ---------------- 应用状态 ----------------

class State:
    """进程级共享：配置、查询编码器（按需加载、只加载一次）、缩略图缓存。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._engines: dict[str, tuple] = {}
        self._engine_lock = threading.Lock()
        self._missing: tuple[float, list[str]] = (0.0, [])
        self._thumbs: OrderedDict[tuple, bytes] = OrderedDict()
        self._thumb_lock = threading.Lock()

    def conn(self) -> sqlite3.Connection:
        if not self.cfg.db_path.exists():
            raise ApiError(503, f"还没有找到数据库：{self.cfg.db_path}。请先运行后台：python -m screenrecall.daemon")
        return db.connect(self.cfg.db_path)

    def missing_models(self) -> list[str]:
        t, val = self._missing
        if time.monotonic() - t > 60:
            val = models.available(self.cfg.models)
            self._missing = (time.monotonic(), val)
        return val

    def engines(self, mode: str) -> retrieval.Engines:
        """与 app.py 的 build_engines 相同：只加载当前模式需要的编码器；不可用时写明原因，由 retrieval 决定退回。"""
        mc = self.cfg.models
        eng = retrieval.Engines()
        if not mc.enabled:
            eng.unavailable_reason[mode] = "配置中已关闭向量模型（models.enabled = false）"
            return eng
        needed = (mc.text.model_id,) if mode == retrieval.TEXT else (mc.clip_text.model_id, mc.image.model_id)
        absent = [m for m in self.missing_models() if m in needed]
        if absent:
            eng.unavailable_reason[mode] = "模型未下载（" + "、".join(absent) + "），请运行 python -m screenrecall.models --download"
            return eng
        try:
            with self._engine_lock:
                if mode not in self._engines:
                    if mode == retrieval.TEXT:
                        self._engines[mode] = (models.StText(mc.text, mc), retrieval.TextVectorIndex(mc.text.model_id))
                    else:
                        self._engines[mode] = (models.StText(mc.clip_text, mc),
                                               retrieval.ImageVectorIndex(models.image_model_id(mc)))
            enc, idx = self._engines[mode]
        except Exception as exc:  # noqa: BLE001 - 加载失败要在页面说明，不中断
            eng.unavailable_reason[mode] = f"模型加载失败：{type(exc).__name__}: {exc}"
            return eng
        if mode == retrieval.TEXT:
            eng.text_encoder, eng.text_index = enc.encode, idx
        else:
            eng.clip_text_encoder, eng.image_index = enc.encode, idx
        return eng

    def thumb(self, path: Path, width: int) -> bytes:
        key = (str(path), path.stat().st_mtime_ns, width)
        with self._thumb_lock:
            if key in self._thumbs:
                self._thumbs.move_to_end(key)
                return self._thumbs[key]
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((width, width * 4))
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=82, method=4)
        data = buf.getvalue()
        with self._thumb_lock:
            self._thumbs[key] = data
            while len(self._thumbs) > 600:
                self._thumbs.popitem(last=False)
        return data


# ---------------- 小工具 ----------------

def _date(s: Any) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s))
    except ValueError:
        raise ApiError(400, f"日期格式不对：{s}（应为 YYYY-MM-DD）") from None


def _local_ms(d: str, t: str) -> int:
    """日期 + 时间按本机时区换成毫秒（与 app.py 的范围删除、导入一致：本机 naive 时间）。"""
    try:
        return int(datetime.combine(date.fromisoformat(d), datetime.strptime(t, "%H:%M").time()).timestamp() * 1000)
    except (ValueError, OSError, OverflowError):  # Windows 上 1970 年前的本地时间会报 OSError
        raise ApiError(400, f"时间不合法：{d} {t}") from None


def _check_fid(fid: str) -> str:
    if not FRAME_ID.match(fid or ""):
        raise ApiError(404, "画面编号不合法")
    return fid


def _strs(v: Any) -> tuple[str, ...]:
    if not v:
        return ()
    if not isinstance(v, list):
        raise ApiError(400, "参数应为列表")
    return tuple(str(x) for x in v)


def _quads(conn: sqlite3.Connection, fid: str, line_nos: list[int]) -> dict[int, Any]:
    if not line_nos:
        return {}
    rows = conn.execute("SELECT line_no, quad FROM ocr_lines WHERE frame_id = ? AND line_no IN "
                        "(SELECT value FROM json_each(?))", (fid, json.dumps(sorted(set(line_nos))))).fetchall()
    return {r["line_no"]: json.loads(r["quad"]) for r in rows}


def _frame_extras(conn: sqlite3.Connection, fids: list[str]) -> tuple[set[str], dict, set[str]]:
    if not fids:
        return set(), {}, set()
    j = json.dumps(fids)
    unknown = importer.time_unknown_frames(conn, fids)
    pred = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT frame_id, label, score FROM frame_labels WHERE frame_id IN (SELECT value FROM json_each(?))", (j,))}
    favs = {r[0] for r in conn.execute(
        "SELECT DISTINCT frame_id FROM favorites WHERE frame_id IN (SELECT value FROM json_each(?))", (j,))}
    return unknown, pred, favs


# ---------------- 接口实现（同步函数，在线程池里跑；每次请求自己开关数据库连接） ----------------

def api_meta(st: State, _: dict) -> dict:
    out = {"tz_label": timeutil.tz_label(), "rules": rules.describe(st.cfg.rules), "notice": NO_AUTH_NOTICE,
           "retention": {"image_days": st.cfg.retention.image_days, "record_days": st.cfg.retention.record_days},
           "data_dir": str(st.cfg.data_dir), "schema_required": db.SCHEMA_VERSION}
    if not st.cfg.db_path.exists():
        return out | {"db_ok": False, "db_problem": f"还没有找到数据库：{st.cfg.db_path}。请先运行后台："
                                                     "python -m screenrecall.daemon", "apps": [], "labels": []}
    conn = st.conn()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        ver = int(row[0]) if row else 0
        if ver < db.SCHEMA_VERSION:
            return out | {"db_ok": False, "apps": [], "labels": [],
                          "db_problem": f"数据库结构是 v{ver}，代码需要 v{db.SCHEMA_VERSION}。请重启后台完成升级。"}
        return out | {"db_ok": True, "apps": search.known_apps(conn), "labels": classify.known_labels(conn)}
    finally:
        conn.close()


def api_status(st: State, _: dict) -> dict:
    conn = st.conn()
    try:
        ctrl = control.read(conn)
        stats = db.index_stats(conn)
        maint = lifecycle.last_maintenance(conn)
        n_fav = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
    finally:
        conn.close()
    now = timeutil.utc_ms()
    keys = ("desired_state", "actual_state", "state_reason", "actual_mode", "mode_until_ms", "mode_reason",
            "capture_note", "snapshot_result", "snapshot_at_ms", "hotkey_status", "last_capture_ms", "heartbeat_ms",
            "target_title", "target_process")
    return {"now_ms": now, "alive": control.daemon_alive(ctrl, now_ms=now), "pending": control.is_pending(ctrl),
            "control": {k: ctrl[k] for k in keys}, "index": stats, "maintenance": maint, "favorites": n_fav,
            "retention": {"image_days": st.cfg.retention.image_days, "record_days": st.cfg.retention.record_days}}


def api_windows(st: State, _: dict) -> dict:
    try:
        from . import winapi
        wins = winapi.list_windows()
    except (ImportError, OSError) as exc:
        return {"windows": [], "problem": f"无法列出窗口：{exc}"}
    out = []
    for w in wins:
        reason = rules.check(w.process, w.title, st.cfg.rules)
        out.append({"hwnd": w.hwnd, "title": w.title, "process": w.process, "width": w.width, "height": w.height,
                    "allowed": reason is None, "reason": reason})
    return {"windows": out}


def api_control(st: State, body: dict) -> dict:
    action = body.get("action")
    conn = st.conn()
    try:
        ctrl = control.read(conn)
        alive = control.daemon_alive(ctrl)
        recording = ctrl["actual_state"] == "recording"
        if action in ("start", "pause") and not alive:
            raise ApiError(409, "后台未运行，请求没有发送。")
        if action == "start":
            try:
                hwnd = int(body.get("hwnd"))
            except (TypeError, ValueError):
                raise ApiError(400, "请先选择要录制的窗口") from None
            from . import winapi
            w = next((x for x in winapi.list_windows() if x.hwnd == hwnd), None)
            if w is None:
                raise ApiError(409, "这个窗口已经关闭或不可录制，请刷新窗口列表")
            reason = rules.check(w.process, w.title, st.cfg.rules)
            if reason:
                raise ApiError(409, f"{reason}，不能录制。")
            control.send_request(conn, "recording", hwnd=w.hwnd, process=w.process, title=w.title)
            return {"message": "请求已发送，等待后台确认"}
        if action == "pause":
            control.send_request(conn, "paused")
            return {"message": "请求已发送，等待后台确认"}
        if action in ("debug", "normal"):
            if not (alive and recording):
                raise ApiError(409, "未在录制，模式没有切换")
            control.send_mode_request(conn, action)
            return {"message": "请求已发送，等待后台确认"}
        if action == "snapshot":
            if not (alive and recording):
                raise ApiError(409, "未在录制，补拍没有发送。")
            control.request_snapshot(conn)
            return {"message": "补拍请求已发送"}
        raise ApiError(400, f"未知操作：{action}")
    finally:
        conn.close()


def api_search(st: State, body: dict) -> dict:
    raw = str(body.get("q") or "")
    mode = body.get("mode") or retrieval.TEXT
    if mode not in (retrieval.TEXT, retrieval.IMAGE):
        raise ApiError(400, "mode 应为 text 或 image")
    limit = int(body.get("limit") or 10)
    if limit not in (10, 20, 50):
        raise ApiError(400, "数量只能是 10、20 或 50")
    d0, d1 = _date(body.get("date_from")), _date(body.get("date_to"))
    start_ms = end_ms = None
    if d0 or d1:
        a, b = sorted((d0 or d1, d1 or d0))
        start_ms, end_ms = timeutil.local_dates_to_utc_range(a, b)
    filters = search.Filters(start_ms, end_ms, _strs(body.get("apps")), _strs(body.get("labels")))
    case_sensitive = bool(body.get("case_sensitive"))
    parsed = search.parse_query(raw)
    engines = st.engines(mode) if parsed.terms and not parsed.error else retrieval.Engines()
    conn = st.conn()
    try:
        resp = retrieval.run(conn, parsed, filters, mode=mode, engines=engines, case_sensitive=case_sensitive,
                             limit=limit, rrf_k=st.cfg.search.rrf_k, branch_top=st.cfg.search.branch_top,
                             relaxed=bool(body.get("relaxed")))
        fids = [r.frame_id for r in resp.results]
        unknown, pred, favs = _frame_extras(conn, fids)
        results = []
        for r in resp.results:
            lex = [] if mode == retrieval.IMAGE else list(r.matched_lines)
            sem = [i for i in r.semantic_lines if i not in lex]
            quads = _quads(conn, r.frame_id, lex + sem)
            boxes = [{"kind": "lex", "quad": quads[i]} for i in lex if i in quads] + \
                    [{"kind": "sem", "quad": quads[i]} for i in sem if i in quads]
            label = pred.get(r.frame_id)
            results.append(_plain(r) | {
                "time_unknown": r.frame_id in unknown, "favorited": r.frame_id in favs, "boxes": boxes,
                "label": {"label": label[0], "score": label[1]} if label else None,
                "image_ok": r.image_status == "ok" and storage.resolve(st.cfg.data_dir, r.image_path).exists()})
    finally:
        conn.close()
    return {"query": {"raw": raw, "quoted": parsed.quoted, "terms": parsed.terms, "error": parsed.error},
            "mode": resp.mode, "total": resp.total, "rank_basis": resp.rank_basis,
            "rank_basis_label": RANK_BASIS.get(resp.rank_basis, resp.rank_basis), "message": resp.message,
            "can_relax": resp.can_relax, "relaxed": resp.relaxed, "relax_rule": search.RELAX_RULE,
            "notes": resp.notes, "results": results}


def _step(s: evidence.Step | None) -> dict | None:
    return None if s is None else _plain(s) | {"source_label": SOURCE_LABELS.get(s.source_type, s.source_type)}


def api_detail(st: State, fid: str, body: dict) -> dict:
    _check_fid(fid)
    mode = body.get("mode") or retrieval.TEXT
    conn = st.conn()
    try:
        d = search.frame_detail(conn, fid)
        if d is None:
            raise ApiError(404, "这条记录已不存在。")
        occ_id = body.get("occurrence_id")
        occ = next((o for o in d.occurrences if o.occurrence_id == occ_id), d.occurrences[0])
        parsed = search.parse_query(str(body.get("q") or ""))
        hit = [] if mode == retrieval.IMAGE or parsed.error else search.match_lines(
            d.text, d.text_fold, [ln["span"] for ln in d.lines], parsed.quoted + parsed.terms,
            bool(body.get("case_sensitive")), bool(body.get("relaxed")))
        sem = sorted(i for i in (body.get("semantic_lines") or []) if isinstance(i, int)
                     and i not in hit and 0 <= i < len(d.lines))
        favs = lifecycle.favorite_ids(conn, [o.occurrence_id for o in d.occurrences])
        ctx = evidence.context(conn, occ.occurrence_id)
        _, pred, _ = _frame_extras(conn, [fid])
    finally:
        conn.close()
    path = storage.resolve(st.cfg.data_dir, d.image_path)
    context = None
    if ctx is not None:
        tl, i = ctx.timeline, ctx.index
        lo, hi = max(0, i - 6), min(len(tl.steps), i + 7)
        context = {
            "session": _plain(tl.session) | {"is_import": tl.session.is_import, "end_label": tl.session.end_label},
            "index": i, "total": len(tl.steps), "prev": _step(ctx.prev), "current": _step(ctx.current),
            "next": _step(ctx.next), "gaps_before": evidence.describe_gaps(ctx.gaps_before),
            "gaps_after": evidence.describe_gaps(ctx.gaps_after),
            "strip": [_step(s) | {"pos": k, "gaps_before": evidence.describe_gaps(tl.between[k - 1]) if 0 < k <= len(
                tl.between) else ""} for k, s in enumerate(tl.steps[lo:hi], start=lo)],
        }
    label = pred.get(fid)
    return {
        "frame_id": d.frame_id, "width": d.width, "height": d.height, "image_status": d.image_status,
        "image_ok": d.image_status == "ok" and path.exists(), "ocr_status": d.ocr_status,
        "capture_meta": d.capture_meta, "time_known": importer.is_time_known(d.capture_meta), "text": d.text,
        "lines": [{"line_no": ln["line_no"], "text": ln["text"], "quad": ln["quad"]} for ln in d.lines],
        "lex_lines": hit, "sem_lines": sem, "mode": mode,
        "occurrence": _plain(occ) | {"favorited": occ.occurrence_id in favs,
                                     "source_label": SOURCE_LABELS.get(occ.source_type, occ.source_type)},
        "occurrences": [_plain(o) | {"favorited": o.occurrence_id in favs,
                                     "source_label": SOURCE_LABELS.get(o.source_type, o.source_type)}
                        for o in d.occurrences],
        "label": {"label": label[0], "score": label[1]} if label else None,
        "context": context,
    }


def api_favorite(st: State, oid: str, body: dict) -> dict:
    conn = st.conn()
    try:
        if body.get("on", True):
            if not lifecycle.add_favorite(conn, oid):
                raise ApiError(404, "这条记录已不存在，无法收藏。")
            return {"favorited": True, "message": "已收藏"}
        lifecycle.remove_favorite(conn, oid)
        return {"favorited": False, "message": "已取消收藏"}
    finally:
        conn.close()


def api_occurrence_plan(st: State, oid: str, _: dict) -> dict:
    conn = st.conn()
    try:
        row = conn.execute("SELECT frame_id, first_seen_ms FROM occurrences WHERE occurrence_id = ?", (oid,)).fetchone()
        if row is None:
            raise ApiError(404, "这条记录已不存在。")
        others = conn.execute("SELECT COUNT(*) FROM occurrences WHERE frame_id = ? AND occurrence_id != ?",
                              (row["frame_id"], oid)).fetchone()[0]
        fav = bool(lifecycle.favorite_ids(conn, [oid]))
    finally:
        conn.close()
    return {"occurrence_id": oid, "first_seen_ms": row["first_seen_ms"], "favorited": fav, "other_occurrences": others}


def api_occurrence_delete(st: State, oid: str, _: dict) -> dict:
    conn = st.conn()
    try:
        res = lifecycle.delete_occurrence(conn, st.cfg.data_dir, oid)
    finally:
        conn.close()
    msg = f"已删除 {res.occurrences_removed} 次出现记录" + (f"、{res.favorites_removed} 个收藏" if res.favorites_removed else "")
    msg += "，画面内容已一并删除。" if res.frames_purged else "，画面内容仍被其他记录引用，已保留。"
    return {"result": res, "message": msg}


def _frame_plan(conn, fid: str) -> dict:
    plan = lifecycle.plan_frame_delete(conn, fid)
    if plan is None:
        raise ApiError(404, "这幅画面已被删除。")
    return {"frame_id": fid, "favorites": sorted(plan.favorites), "image_status": plan.image_status,
            "occurrences": [_plain(o) | {"favorited": o["occurrence_id"] in plan.favorites,
                                         "source_label": SOURCE_LABELS.get(o["source_type"], o["source_type"])}
                            for o in plan.occurrences]}


def api_frame_plan(st: State, fid: str, _: dict) -> dict:
    _check_fid(fid)
    conn = st.conn()
    try:
        return _frame_plan(conn, fid)
    finally:
        conn.close()


def api_frame_delete(st: State, fid: str, body: dict) -> dict:
    _check_fid(fid)
    shown = set(_strs(body.get("confirmed_favorites")))
    conn = st.conn()
    try:
        try:
            res = lifecycle.delete_frame(conn, st.cfg.data_dir, fid, confirmed_favorites=shown)
        except lifecycle.PlanChanged as exc:
            raise ApiError(409, f"{exc}。删除范围已更新。", plan=_frame_plan(conn, fid)) from None
    finally:
        conn.close()
    return {"result": res, "message": f"已删除这幅画面的 {res.occurrences_removed} 次出现记录"
                                      + (f"和 {res.favorites_removed} 个收藏" if res.favorites_removed else "") + "。"}


def api_favorites(st: State, _: dict) -> dict:
    conn = st.conn()
    try:
        rows = lifecycle.list_favorites(conn)
        size = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT frame_id, width, height FROM frames WHERE frame_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted({r["frame_id"] for r in rows})),))}
    finally:
        conn.close()
    return {"favorites": [_plain(r) | {"image_ok": r["image_status"] == "ok", "width": size[r["frame_id"]][0],
                                       "height": size[r["frame_id"]][1]} for r in rows]}


def _range_args(body: dict) -> tuple[int, int, tuple[str, ...], bool]:
    d0, d1 = body.get("date_from"), body.get("date_to") or body.get("date_from")
    if not d0:
        raise ApiError(400, "请先选择日期")
    start = _local_ms(d0, body.get("time_from") or "00:00")
    end = _local_ms(d1, body.get("time_to") or "23:59")
    if end <= start:
        raise ApiError(400, "结束时间必须晚于开始时间。")
    return start, end, _strs(body.get("apps")), bool(body.get("include_favorites"))


def api_range_preview(st: State, body: dict) -> dict:
    start, end, apps, inc = _range_args(body)
    conn = st.conn()
    try:
        plan = lifecycle.plan_range_delete(conn, start, end, apps, include_favorites=inc)
    finally:
        conn.close()
    return {"plan": plan}


def api_range_delete(st: State, body: dict) -> dict:
    start, end, apps, inc = _range_args(body)
    conn = st.conn()
    try:
        try:
            res = lifecycle.delete_range(conn, st.cfg.data_dir, start, end, apps, include_favorites=inc,
                                         confirmed_favorites=frozenset(_strs(body.get("confirmed_favorites"))))
        except lifecycle.PlanChanged as exc:
            raise ApiError(409, f"{exc}。请重新预览。") from None
    finally:
        conn.close()
    return {"result": res, "message": f"已按范围删除 {res.occurrences_removed} 次出现记录、{res.frames_purged} 幅画面"
                                      + (f"、{res.favorites_removed} 个收藏" if res.favorites_removed else "") + "。"}


def api_failed(st: State, _: dict) -> dict:
    conn = st.conn()
    try:
        summ = retry.summary(conn)
        jobs = retry.failed_jobs(conn, limit=8)
    finally:
        conn.close()
    return {"summary": {s: {"name": STAGE_NAME.get(s, s), "retryable": v["retryable"],
                            "not_retryable": v["not_retryable"], "errors": v["errors"].most_common(3)}
                        for s, v in summ.items()},
            "jobs": [_plain(j) | {"stage_name": STAGE_NAME.get(j.stage, j.stage)} for j in jobs]}


def api_retry(st: State, _: dict) -> dict:
    conn = st.conn()
    try:
        res = retry.retry_failed(conn)
    finally:
        conn.close()
    skipped = "、".join(f"{k} {v}" for k, v in res.not_retryable.items())
    return {"requeued": res.total, "message": f"已改回待处理 {res.total} 条，后台会自动重新处理。"
                                              + (f"未改：{skipped}。" if skipped else "")}


def api_import(st: State, files: list[importer.ImportFile], label: str, captured_ms: int | None) -> dict:
    if not files:
        raise ApiError(400, "请先选择截图文件")
    files = sorted(files, key=lambda f: f.name)  # 与 app.py 一致：按文件名顺序处理
    conn = st.conn()
    try:
        res = importer.import_batch(conn, st.cfg, files, label=label or None, captured_at_ms=captured_ms)
    finally:
        conn.close()
    return {"batch": res, "ok": res.count(importer.OK), "failed": res.count(importer.FAILED),
            "skipped": res.count(importer.SKIPPED)}


# ---------------- 路由与安全 ----------------

def _lang(request: Request) -> str:
    """界面语言：前端每个请求都带 X-Lang（zh / en）；英文时由 i18n 翻译系统提示。"""
    return "en" if request.headers.get("x-lang", "").lower().startswith("en") else "zh"


def _wrap(st: State, fn: Callable, *path_params: str) -> Callable:
    async def endpoint(request: Request) -> Response:
        lang = _lang(request)
        try:
            body: dict = {}
            if request.method in ("POST", "PUT", "DELETE"):
                raw = await request.body()
                if raw:
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ApiError(400, "请求体应为 JSON 对象")
            else:
                body = dict(request.query_params)
            args = [request.path_params[p] for p in path_params]
            return _json(i18n.localize(_plain(await run_in_threadpool(fn, st, *args, body)), lang))
        except ApiError as exc:
            return _json(i18n.localize(_plain({"error": exc.message} | exc.extra), lang), exc.status)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json(i18n.localize({"error": "请求体不是合法的 UTF-8 JSON"}, lang), 400)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc):
                return _json(i18n.localize({"error": "数据库正忙（后台正在写入），请稍后重试"}, lang), 503)
            raise
    return endpoint


def _image_route(st: State, thumb: bool) -> Callable:
    def load(fid: str, width: int) -> Response:
        _check_fid(fid)
        conn = st.conn()
        try:
            row = conn.execute("SELECT image_path, image_status FROM frames WHERE frame_id = ?", (fid,)).fetchone()
        finally:
            conn.close()
        if row is None or row["image_status"] != "ok":
            raise ApiError(404, "原图已不可用")
        path = storage.resolve(st.cfg.data_dir, row["image_path"])
        if not path.exists():
            raise ApiError(404, "原图文件缺失")
        headers = {"Cache-Control": "private, max-age=3600"}
        if thumb:
            return Response(st.thumb(path, width), media_type="image/webp", headers=headers)
        return FileResponse(path, media_type="image/webp", headers=headers,
                            filename=f"{fid}.webp" if width < 0 else None)

    async def endpoint(request: Request) -> Response:
        try:
            w = int(request.query_params.get("w", "480"))
            w = max(120, min(1600, w))
            if request.query_params.get("download"):
                w = -1
            return await run_in_threadpool(load, request.path_params["fid"], w)
        except ApiError as exc:
            return _json({"error": exc.message}, exc.status)
        except ValueError:
            return _json({"error": "参数不合法"}, 400)
    return endpoint


def _import_route(st: State) -> Callable:
    async def endpoint(request: Request) -> Response:
        try:
            form = await request.form(max_files=MAX_UPLOAD_FILES, max_part_size=MAX_UPLOAD_BYTES)
            files = []
            for up in form.getlist("files"):
                if not hasattr(up, "read"):
                    continue
                data = await up.read()
                files.append(importer.ImportFile(name=Path(up.filename or "upload").name, data=data))
            d, t = form.get("captured_date"), form.get("captured_time")
            captured = _local_ms(str(d), str(t or "00:00")) if d else None
            res = await run_in_threadpool(api_import, st, files, str(form.get("label") or "").strip(), captured)
            return _json(i18n.localize(_plain(res), _lang(request)))
        except ApiError as exc:
            return _json(i18n.localize({"error": exc.message}, _lang(request)), exc.status)
    return endpoint


class LocalOnly:
    """核对 Host / Origin，并要求会改数据的请求带 X-ScreenRecall 头（见模块说明）。"""

    def __init__(self, app, port: int, extra_origins: tuple[str, ...] = ()):
        self.app = app
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts} | set(extra_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        problem = None
        if headers.get("host") not in self.hosts:
            problem = "只接受本机地址访问"
        elif scope["method"] not in ("GET", "HEAD", "OPTIONS") and scope["path"].startswith("/api/"):
            origin = headers.get("origin")
            if headers.get("x-screenrecall") != "1":
                problem = "缺少 X-ScreenRecall 请求头"
            elif origin and origin not in self.origins:
                problem = "不接受其他网页发来的修改请求"
        if problem:
            return await JSONResponse({"error": problem}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(cfg: Config | None = None, *, port: int = 8600, extra_origins: tuple[str, ...] = (),
               dist: Path = WEB_DIST) -> LocalOnly:
    st = State(cfg or load_config())
    w = lambda fn, *p: _wrap(st, fn, *p)  # noqa: E731
    routes = [
        Route("/api/meta", w(api_meta)),
        Route("/api/status", w(api_status)),
        Route("/api/windows", w(api_windows)),
        Route("/api/control", w(api_control), methods=["POST"]),
        Route("/api/search", w(api_search), methods=["POST"]),
        Route("/api/frames/{fid}/detail", w(api_detail, "fid"), methods=["POST"]),
        Route("/api/frames/{fid}/image", _image_route(st, thumb=False)),
        Route("/api/frames/{fid}/thumb", _image_route(st, thumb=True)),
        Route("/api/frames/{fid}/delete-plan", w(api_frame_plan, "fid")),
        Route("/api/frames/{fid}/delete", w(api_frame_delete, "fid"), methods=["POST"]),
        Route("/api/occurrences/{oid}/favorite", w(api_favorite, "oid"), methods=["POST"]),
        Route("/api/occurrences/{oid}/delete-plan", w(api_occurrence_plan, "oid")),
        Route("/api/occurrences/{oid}/delete", w(api_occurrence_delete, "oid"), methods=["POST"]),
        Route("/api/favorites", w(api_favorites)),
        Route("/api/range/preview", w(api_range_preview), methods=["POST"]),
        Route("/api/range/delete", w(api_range_delete), methods=["POST"]),
        Route("/api/failed", w(api_failed)),
        Route("/api/failed/retry", w(api_retry), methods=["POST"]),
        Route("/api/import", _import_route(st), methods=["POST"]),
    ]
    if dist.exists():
        async def index(_: Request) -> Response:
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})
        routes += [Mount("/assets", StaticFiles(directory=dist / "assets")), Route("/", index)]
    app = Starlette(routes=routes)
    app.state.sr = st
    return LocalOnly(app, port, extra_origins)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(prog="python -m screenrecall.webapi", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", help="运行数据目录（覆盖配置与环境变量）")
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--dev-origin", action="append", default=[], help="开发时允许的前端地址，如 http://127.0.0.1:5173")
    a = ap.parse_args(argv)
    cfg = load_config(a.data_dir)
    if not WEB_DIST.exists():
        print(f"注意：没有找到前端构建产物 {WEB_DIST}，只提供 /api。先在 web/ 下运行 npm run build。")
    print(f"ScreenRecall：http://127.0.0.1:{a.port}  数据目录 {cfg.data_dir}  （{NO_AUTH_NOTICE}）")
    uvicorn.run(create_app(cfg, port=a.port, extra_origins=tuple(a.dev_origin)), host="127.0.0.1", port=a.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
