"""网页 API v2：/api/v2/*（挂在主进程的 AuthGuard 里面，Cookie 或 Bearer 令牌才能进）。

- 读：store.queries.Queries（只读连接）；写：主进程的写线程 Writer（control_set、job_add）；
  隐私删除走 adapters.Deleter（delete.delete_data + 向量）；
- 会改东西的 POST 还要带 X-ScreenRecall: 1 请求头（别的网页发不出这个头）；
- 时间都是 UTC 毫秒整数，页面按本地时间显示；出错返回 {"error": "<中文说明>"}；列表都有上限；
- 给页面用的接口不套"对 Agent 隐藏"名单（那是给 Agent 的），排除规则在录制时就生效了。
接口约定见任务卡的"Web API contract v2"。
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from ..app import paths
from ..app import tray as tray_mod
from ..store.fts import lexical
from ..store.queries import _block_roots
from ..store.writer import _job_add
from ..timeutil import local_dates_to_utc_range
from . import config as v2config
from .adapters import StoreStack, effective_state, latest_frames, piece_of, recorder_running
from .rec_sink import EXCLUDED_TITLE, NOT_RUNNING_DETAIL, STOPPED_DETAIL
from .work_common import private_rules
from .work_llm import PIECE_JOB

log = logging.getLogger("screenrecall.v2.web")

MAX_SEARCH_GROUPS = 50
MAX_DAY_ITEMS = 2000
MAX_PIECE_LINES = 5000
MAX_PINNED = 2
MAX_LIST_ITEMS = 200
MAX_ITEM_CHARS = 200
MAX_THUMB_CACHE = 256
STORAGE_CACHE_S = 30.0
LLM_TEST_PROMPT = "Reply with the single word OK."
_FRAME_ID = re.compile(r"^[0-9a-f]{32}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class V2Context:
    """网页 API 要用的东西。除 stack / home 外都有默认值，测试按需替换。"""
    stack: StoreStack
    home: Path
    idle_running: Callable[[], bool] = lambda: False
    rotate_token: Callable[[], str] | None = None          # 换令牌，返回新的 Set-Cookie 值
    autostart: Any = None                                   # 有 is_enabled() / enable() / disable() 的对象
    list_windows: Callable[[], list] | None = None
    llm_env_files: Sequence[Path] | None = None             # None = 仓库 .env + home/.env
    llm_environ: dict[str, str] | None = None               # None = os.environ
    llm_transport: Any = None
    on_settings: Callable[[dict], None] | None = None       # 设置改了之后（如空闲阈值立即生效）
    _thumbs: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)
    _thumb_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _storage: tuple[float, dict] | None = field(default=None, init=False, repr=False)

    @property
    def data_dir(self) -> Path:
        return self.stack.data_dir

    @property
    def queries(self):
        return self.stack.queries

    def env_files(self) -> list[Path]:
        if self.llm_env_files is not None:
            return list(self.llm_env_files)
        from ..app.llm import REPO_ROOT
        return [REPO_ROOT / ".env", self.home / ".env"]

    def get_autostart(self):
        if self.autostart is None:
            from ..app import autostart
            self.autostart = autostart
        return self.autostart

    def forget_caches(self) -> None:
        with self._thumb_lock:
            self._thumbs.clear()
        self._storage = None


# ---------------- 参数 ----------------

def _int(v: Any, name: str, *, lo: int | None = None, hi: int | None = None, default: int | None = None) -> int | None:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise ApiError(400, f"{name} 应为整数")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} 应为整数") from None
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ApiError(400, f"{name} 超出范围（{lo} 到 {hi}）")
    return n


def _str(v: Any, name: str, *, cap: int = MAX_ITEM_CHARS, allow_empty: bool = False) -> str:
    if not isinstance(v, str):
        raise ApiError(400, f"{name} 应为文字")
    s = v.strip()
    if not s and not allow_empty:
        raise ApiError(400, f"{name} 不能为空")
    if len(s) > cap or any(c in s for c in "\r\n\x00"):
        raise ApiError(400, f"{name} 太长或含有换行")
    return s


def _str_list(v: Any, name: str) -> list[str]:
    if not isinstance(v, list):
        raise ApiError(400, f"{name} 应为文字列表")
    if len(v) > MAX_LIST_ITEMS:
        raise ApiError(400, f"{name} 最多 {MAX_LIST_ITEMS} 项")
    out = [_str(x, name) for x in v if not (isinstance(x, str) and not x.strip())]
    return list(dict.fromkeys(out))


def _parse_day(s: str | None) -> date:
    if not s:
        return datetime.now().date()
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ApiError(400, "date 应为 YYYY-MM-DD") from None


# ---------------- 检索结果里的高亮 ----------------

def _fold_map(text: str) -> tuple[str, list[int]]:
    """逐字 NFKC + casefold、去空白（与 docs.norm 一致），并记下每个折叠后的字来自原文第几个字。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).casefold():
            if not c.isspace():
                chars.append(c)
                idx.append(i)
    return "".join(chars), idx


def match_spans(text: str, terms: Sequence[str], *, tails: bool = True) -> list[list[int]]:
    """各检索词（已 norm）在原文里的位置 [start, end)，按字符计；大小写、全半角、空白都不敏感。
    tails=True（字面命中的行）：折到下一行的命中（词的开头在本行末尾）标出本行里的那一截。
    只有语义分支命中的行不是字面命中，tails=False，不然行尾碰巧是词的第一个字也会被标出来。"""
    folded, idx = _fold_map(text)
    spans: list[list[int]] = []
    for t in terms:
        if not t:
            continue
        start, found = 0, False
        while True:
            j = folded.find(t, start)
            if j < 0:
                break
            spans.append([idx[j], idx[j + len(t) - 1] + 1])
            found = True
            start = j + 1
        if not found and tails:
            for k in range(min(len(t) - 1, len(folded)), 0, -1):
                if folded.endswith(t[:k]):
                    spans.append([idx[len(folded) - k], len(text)])
                    break
    spans.sort()
    merged: list[list[int]] = []
    for s in spans:
        if merged and s[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(list(s))
    return merged


# ---------------- 各接口 ----------------

def api_status(ctx: V2Context, _req: dict) -> dict:
    stack = ctx.stack
    ctrl = stack.control.get()
    now = stack.clock()
    state, until = effective_state(ctrl, now)
    current = None
    running = recorder_running(ctrl)
    if running and state == "recording" and ctrl.get("current_process"):
        title = ctrl.get("current_title")
        if private_rules({"excluded_processes": ctrl.get("excluded_processes"),
                          "excluded_title_keywords": ctrl.get("excluded_title_keywords")}).match(
                ctrl["current_process"], title):
            title = None  # 命中排除规则的窗口：不显示标题
        current = {"app": ctrl["current_process"], "title": title, "excluded": False}
    elif running and state == "recording" and ctrl.get("current_title") == EXCLUDED_TITLE:
        # 前台是排除的窗口：录制进程只写了占位标题（不写进程名和标题）
        current = {"app": None, "title": None, "excluded": True}
    conn = ctx.queries.conn()
    pending = int(conn.execute("SELECT COUNT(*) FROM ocr_regions WHERE status IN ('pending', 'running')")
                  .fetchone()[0])
    rev = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    # recorder_alive：心跳新鲜（15 秒内）。正常停下时心跳还新鲜、recording = stopped，页面显示"没在录制"；
    # 心跳断了才是"录制程序没在运行"
    return {"recording": state, "pause_until_ms": until, "recorder_alive": bool(ctrl.get("recorder_alive")),
            "current": current, "pending_ocr": pending,
            "idle_work": "running" if ctx.idle_running() else "idle",
            "agent_enabled": bool(ctrl.get("agent_enabled")), "revision": int(rev[0]) if rev else 0}


def api_pause(ctx: V2Context, req: dict) -> dict:
    body = req["body"]
    if body.get("until") is not None:
        if body["until"] != "tomorrow":
            raise ApiError(400, "until 只能是 tomorrow")
        until_ms = int(tray_mod.tomorrow_start().timestamp() * 1000)
    else:
        minutes = _int(body.get("minutes"), "minutes", lo=1, hi=7 * 24 * 60)
        if minutes is None:
            raise ApiError(400, "请给出暂停多少分钟（minutes），或 until: tomorrow")
        until_ms = ctx.stack.clock() + minutes * 60_000
    ctx.stack.set_control(desired_state="paused", pause_until_ms=until_ms)
    return api_status(ctx, req)


def api_resume(ctx: V2Context, req: dict) -> dict:
    ctx.stack.set_control(desired_state="recording", pause_until_ms=None)
    return api_status(ctx, req)


def _summary_out(s: dict | None) -> dict | None:
    if s is None:
        return None
    return {"text": s["text"], "status": s["status"], "model": s["model"]}


def _first_line(text: str | None, cap: int = 120) -> str | None:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln[:cap] + ("…" if len(ln) > cap else "")
    return None


def api_day(ctx: V2Context, req: dict) -> dict:
    d = _parse_day(req["query"].get("date"))
    lo, hi = local_dates_to_utc_range(d, d)
    q = ctx.queries
    conn = q.conn()
    ov = q.day_overview(d)
    tl = q.timeline(lo, hi)
    now = ctx.stack.clock()
    pieces = [x for x in tl["items"] if x["kind"] == "piece"]
    ids = [int(p["id"]) for p in pieces]
    summaries: dict[int, str] = {}
    pending: set[int] = set()
    if ids:
        j = json.dumps(ids)
        summaries = {int(k): t for k, t in conn.execute(
            "SELECT key, text FROM summaries WHERE level = 'piece' AND text != '' "
            "AND key IN (SELECT CAST(value AS TEXT) FROM json_each(?))", (json.dumps([str(i) for i in ids]),))}
        pending = {int(r[0]) for r in conn.execute(
            "SELECT DISTINCT p.piece_id FROM pieces p JOIN piece_segments ps ON ps.piece_id = p.piece_id "
            "JOIN frames f ON f.segment_id = ps.segment_id JOIN ocr_regions r ON r.frame_id = f.frame_id "
            "WHERE r.status IN ('pending', 'running') AND f.captured_ms BETWEEN p.start_ms AND p.end_ms "
            "AND p.piece_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))", (j,))}
    items: list[dict] = [{
        "kind": "piece", "piece_id": int(p["id"]), "start_ms": p["start_ms"], "end_ms": p["end_ms"],
        "app": p["app"], "title": p["title"], "task": p["task_label"],
        "summary_line": _first_line(summaries.get(int(p["id"]))), "pending_ocr": int(p["id"]) in pending,
    } for p in pieces]
    items += _day_gaps(conn, tl["items"], lo, hi, now)
    items.sort(key=lambda x: (x["start_ms"], 0 if x["kind"] == "piece" else 1), reverse=True)
    apps = [{"app": a["process"], "ms": int(a["ms"])} for a in ov["apps"] if a["process"]]
    return {"date": d.isoformat(),
            "overview": {"active_ms": sum(a["ms"] for a in apps), "apps": apps, "lines_new": ov["lines_new"],
                         "frames": ov["frames"]},
            "summary": _summary_out(ov["summary"]), "items": items[:MAX_DAY_ITEMS]}


def gap_reason(reason: str, detail: str | None) -> str | None:
    """库里的缺口（录制进程写的 reason + detail）→ 页面上的原因；None = 不算"没有记录"的时段。
    locked / paused / stopped（正常退出或没在运行）是整台电脑的；minimized / cloaked / excluded 是某个窗口的。"""
    if reason == "closed":
        return "stopped" if detail == STOPPED_DETAIL else None  # 别的 closed：窗口关了，不是空档
    if reason == "error":
        return "stopped" if detail == NOT_RUNNING_DETAIL else "error"
    if reason == "minimised":
        return "cloaked" if detail == "cloaked" else "minimized"
    return reason if reason in ("locked", "paused", "excluded") else None


WINDOW_GAP_REASONS = ("minimized", "cloaked", "excluded")
MIN_WINDOW_GAP_MS = 60_000


def _subtract(start: int, end: int, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """[start, end) 去掉 spans（已排序、已合并）覆盖的部分。"""
    out, cur = [], start
    for a, b in spans:
        if b <= cur:
            continue
        if a >= end:
            break
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
        if cur >= end:
            break
    if cur < end:
        out.append((cur, end))
    return out


def _recorded_spans(conn, lo: int, hi: int, now: int) -> list[tuple[int, int]]:
    """这一天里前台有可录窗口（没被排除）的时段，合并好。"""
    spans: list[tuple[int, int]] = []
    for a, b in conn.execute(
            "SELECT MAX(start_ms, ?), MIN(COALESCE(end_ms, ?), ?) FROM activity WHERE excluded = 0 "
            "AND COALESCE(end_ms, ?) > ? AND start_ms < ? ORDER BY start_ms", (lo, now, hi, now, lo, hi)):
        a, b = int(a), int(b)
        if b <= a:
            continue
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    return spans


def _day_gaps(conn, tl_items: list[dict], lo: int, hi: int, now: int) -> list[dict]:
    """"没有记录"的时段：同一原因、时间重叠的合并（锁屏时每个窗口各记一条）。
    某个窗口的缺口（固定的窗口最小化了、排除的窗口）只算前台没有在录别的窗口的那部分：用户在别的应用里
    工作的时间是录了的，不能显示成整天的空档；剩下不到一分钟的不显示。"""
    procs = {int(r[0]): r[1] for r in conn.execute(
        "SELECT gap_id, process FROM gaps WHERE COALESCE(end_ms, ?) > ? AND start_ms < ?", (now, lo, hi))}
    recorded: list[tuple[int, int]] | None = None
    raw = []
    for g in tl_items:
        if g["kind"] != "gap":
            continue
        reason = gap_reason(g["reason"], g.get("detail"))
        if reason is None:
            continue
        start = max(int(g["start_ms"]), lo)
        end = min(int(g["end_ms"]) if g["end_ms"] is not None else now, hi)
        if end <= start:
            continue
        parts = [(start, end)]
        if reason in WINDOW_GAP_REASONS:
            if recorded is None:
                recorded = _recorded_spans(conn, lo, hi, now)
            parts = [(a, b) for a, b in _subtract(start, end, recorded) if b - a >= MIN_WINDOW_GAP_MS]
        note = procs.get(int(g["id"])) if reason == "excluded" else None
        raw += [{"kind": "gap", "start_ms": a, "end_ms": b, "reason": reason, "note": note} for a, b in parts]
    raw.sort(key=lambda x: (x["reason"], x["start_ms"]))
    out: list[dict] = []
    for g in raw:
        last = out[-1] if out else None
        if last and last["reason"] == g["reason"] and g["start_ms"] <= last["end_ms"] + 1000:
            last["end_ms"] = max(last["end_ms"], g["end_ms"])
            if last["note"] != g["note"]:
                last["note"] = None
        else:
            out.append(dict(g))
    return out


def api_piece(ctx: V2Context, req: dict) -> dict:
    pid = _int(req["path"]["piece_id"], "piece_id", lo=1)
    q = ctx.queries
    p = q.piece(pid, max_lines=MAX_PIECE_LINES)
    if p is None:
        raise ApiError(404, "找不到这个片段：可能已被删除")
    conn = q.conn()
    shown = [s["segment_id"] for s in p["segments"]]
    line_ids = [ln["line_id"] for s in p["segments"] for b in s["blocks"] for ln in b["lines"]]
    frames_of = latest_frames(conn, line_ids)
    roots: dict[int, tuple[int, int, str, int]] = {}
    for sid in shown:
        roots.update(_block_roots(conn, sid))
    regions = conn.execute(
        "SELECT r.region_id, r.frame_id, r.y0, COALESCE(r.block_id, f.block_id) AS block_id, "
        "COALESCE(r.pos_y, f.pos_y) AS pos_y FROM ocr_regions r JOIN frames f ON f.frame_id = r.frame_id "
        "WHERE r.status IN ('pending', 'running') AND f.captured_ms BETWEEN ? AND ? AND f.segment_id IN "
        "(SELECT CAST(value AS INTEGER) FROM json_each(?)) ORDER BY r.region_id",
        (p["start_ms"], p["end_ms"], json.dumps(shown))).fetchall()
    blocks: list[dict] = []
    by_id: dict[int | None, dict] = {}
    for s in p["segments"]:
        for b in s["blocks"]:
            bid = None if b["block_id"] == -1 else int(b["block_id"])
            entries: list[tuple[float, int, dict]] = []
            for ln in b["lines"]:
                entries.append((float(ln["y0"]), 0, {
                    "kind": "line", "line_id": ln["line_id"], "text": ln["text"], "source": ln["source"],
                    "first_seen_ms": ln["first_seen_ms"], "frame_id": frames_of.get(int(ln["line_id"]))}))
            for sk in b["skims"]:
                ys = [y for y in (sk["y_from"], sk["y_to"]) if y is not None]
                y = float(min(ys)) if ys else float("inf")
                dist = abs(int(sk["y_to"]) - int(sk["y_from"])) if len(ys) == 2 else 0
                entries.append((y, 1, {"kind": "skim", "start_ms": sk["start_ms"], "end_ms": sk["end_ms"],
                                       "distance_px": dist}))
            blk = {"block_id": bid, "items": entries}
            blocks.append(blk)
            by_id[bid] = blk
    for r in regions:
        bid, off = None, 0
        if r["block_id"] is not None:
            bid, off = roots.get(int(r["block_id"]), (int(r["block_id"]), 0, "none", 0))[:2]
        blk = by_id.get(bid)
        if blk is None:
            blk = by_id[bid] = {"block_id": bid, "items": []}
            blocks.append(blk)
        y = float(r["pos_y"] + r["y0"] + off) if r["pos_y"] is not None else float("inf")
        blk["items"].append((y, 2, {"kind": "pending", "region_id": r["region_id"], "frame_id": r["frame_id"]}))
    for blk in blocks:
        blk["items"] = [e[2] for e in sorted(blk["items"], key=lambda e: (e[0], e[1]))]
    real_blocks = [b for b in blocks if b["block_id"] is not None and b["items"]]
    return {"piece": {"piece_id": p["piece_id"], "start_ms": p["start_ms"], "end_ms": p["end_ms"], "app": p["app"],
                      "title": p["title"], "task": p["task_label"]},
            "summary": _summary_out(p["summary"]), "summary_state": summary_state(ctx, p, p["summary"]),
            "blocks": [b for b in blocks if b["items"]],
            "unjoined": len(real_blocks) > 1,
            "frames": [{"frame_id": f["frame_id"], "captured_ms": f["captured_ms"], "width": f["width"],
                        "height": f["height"]} for f in p["frames"]]}


def summary_state(ctx: V2Context, piece: dict, summary: dict | None) -> str:
    """片段概括现在的情况（页面据此说明）：
    ok 已写好；stale 内容被删过、要重写；queued 点过"重新生成"、等电脑空闲时写；private 命中对 Agent 隐藏
    或录制排除规则，不发给大模型；failed 大模型多次失败；no_text 片段里没有字可写；waiting 还没写
    （片段还在进行，或等电脑空闲时整理）。"""
    if private_rules(ctx.stack.control.get()).match(piece.get("app"), piece.get("title")):
        return "private"
    job = ctx.queries.conn().execute("SELECT status FROM jobs WHERE kind = ? AND target = ?",
                                     (PIECE_JOB, str(piece["piece_id"]))).fetchone()
    js = job[0] if job else None
    if js in ("pending", "running"):
        return "queued"
    if summary is not None and summary.get("text"):
        return "stale" if summary.get("status") == "stale" else "ok"
    if js == "failed":
        return "failed"
    if js == "done":
        return "no_text"
    return "waiting"


def api_summarize(ctx: V2Context, req: dict) -> dict:
    """重新生成：概括标为 stale + 登记空闲整理进程的片段概括待办（同一个约定：kind = PIECE_JOB，
    target = 片段编号）。整理进程只在电脑空闲时运行，所以只是排队；页面说"空闲时重写"。"""
    pid = _int(req["path"]["piece_id"], "piece_id", lo=1)
    p = ctx.queries.conn().execute("SELECT piece_id, app, title FROM pieces WHERE piece_id = ?", (pid,)).fetchone()
    if p is None:
        raise ApiError(404, "找不到这个片段：可能已被删除")
    if private_rules(ctx.stack.control.get()).match(p["app"], p["title"]):
        raise ApiError(409, "这个应用或窗口在“对 Agent 隐藏”或排除名单里，不发给大模型")
    ctx.stack.writer.call(_queue_summary, pid, ctx.stack.clock()).result(10)
    return {"queued": True, "summary_state": "queued"}


def _queue_summary(conn, pid: int, now: int) -> None:
    conn.execute("UPDATE summaries SET status = 'stale' WHERE level = 'piece' AND key = ?", (str(pid),))
    _job_add(conn, PIECE_JOB, str(pid), 0, now)


def api_search(ctx: V2Context, req: dict) -> dict:
    qp = req["query"]
    query = (qp.get("q") or "").strip()
    if not query:
        raise ApiError(400, "请输入要搜索的文字")
    if len(query) > 500:
        raise ApiError(400, "搜索的文字太长了（最多 500 字）")
    plan = lexical.plan(query)
    if plan.error:
        raise ApiError(400, plan.error)
    t_from = _int(qp.get("from"), "from")
    t_to = _int(qp.get("to"), "to")
    if t_from is not None and t_to is not None and t_to <= t_from:
        raise ApiError(400, "结束时间必须晚于开始时间")
    limit = _int(qp.get("limit"), "limit", lo=1, hi=MAX_SEARCH_GROUPS, default=20)
    res = ctx.stack.search.search(query, time_from=t_from, time_to=t_to, app=qp.get("app") or None,
                                  task=qp.get("task") or None, limit=limit, per_group=3)
    terms = [t.text for t in plan.terms]
    conn = ctx.queries.conn()
    seg_ids = [int(g["segment_id"]) for g in res["groups"]]
    first_seen = {int(r[0]): int(r[1]) for r in conn.execute(
        "SELECT segment_id, first_seen_ms FROM segments WHERE segment_id IN "
        "(SELECT CAST(value AS INTEGER) FROM json_each(?))", (json.dumps(seg_ids),))} if seg_ids else {}
    frames_of = latest_frames(conn, [ln["line_id"] for g in res["groups"] for ln in g["lines"]])
    groups = []
    for g in res["groups"]:
        lines = sorted(g["lines"], key=lambda ln: -int(ln["last_seen_ms"]))
        last = max([int(g["last_seen_ms"])] + [int(ln["last_seen_ms"]) for ln in lines])
        first = first_seen.get(int(g["segment_id"]), min([int(ln["first_seen_ms"]) for ln in lines] or [last]))
        p = piece_of(conn, g["segment_id"], last)
        literal = "lexical" in g.get("sources", ("lexical",))
        groups.append({
            "segment_id": int(g["segment_id"]), "app": g["app"], "title": g["title"],
            "first_seen_ms": first, "last_seen_ms": last,
            "hits_total": int(g["hits"]) if g.get("hits") else len(lines),
            "piece_id": None if p is None else p[0],
            "hits": [{"line_id": int(ln["line_id"]), "text": ln["text"], "spans": match_spans(ln["text"], terms, tails=literal),
                      "last_seen_ms": int(ln["last_seen_ms"]), "frame_id": frames_of.get(int(ln["line_id"]))}
                     for ln in lines],
        })
    groups.sort(key=lambda g: (-g["last_seen_ms"], -g["segment_id"]))
    total = max(int(res.get("total_groups") or 0), len(groups))
    return {"query": query, "total_groups": total, "groups": groups, "semantic": bool(res.get("semantic"))}


def _frame_or_404(ctx: V2Context, frame_id: str) -> dict:
    if not _FRAME_ID.match(frame_id or ""):
        raise ApiError(404, "找不到这张画面")
    fr = ctx.queries.frame(frame_id)
    if fr is None or fr["status"] != "ok":
        raise ApiError(404, "找不到这张画面：可能已被删除")
    return fr


def _frame_path(ctx: V2Context, fr: dict) -> Path:
    root = ctx.data_dir.resolve()
    p = (root / fr["rel_path"]).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise ApiError(404, "画面文件不见了")
    return p


def api_frame(ctx: V2Context, req: dict) -> dict:
    fr = _frame_or_404(ctx, req["path"]["frame_id"])
    conn = ctx.queries.conn()
    app, title = fr["app"], fr["title"]
    if (not app or title is None) and fr["segment_id"] is not None:
        seg = conn.execute("SELECT app, title FROM segments WHERE segment_id = ?", (fr["segment_id"],)).fetchone()
        if seg is not None:
            app, title = app or seg["app"], title if title is not None else seg["title"]
    p = piece_of(conn, fr["segment_id"], fr["captured_ms"])
    return {"frame_id": fr["frame_id"], "captured_ms": fr["captured_ms"], "width": fr["width"],
            "height": fr["height"], "app": app, "title": title, "piece_id": None if p is None else p[0],
            "lines": [{"line_id": ln["line_id"], "text": ln["text"], "x0": ln["rect"][0], "y0": ln["rect"][1],
                       "x1": ln["rect"][2], "y1": ln["rect"][3]} for ln in fr["lines"]]}


# no-store：证据图不能留在浏览器的磁盘缓存里，不然"删除"之后图片还在（隐私删除要真删）
IMAGE_CACHE = "private, no-store"


def api_frame_image(ctx: V2Context, req: dict) -> Response:
    fr = _frame_or_404(ctx, req["path"]["frame_id"])
    return FileResponse(_frame_path(ctx, fr), media_type="image/webp", headers={"Cache-Control": IMAGE_CACHE})


def api_frame_thumb(ctx: V2Context, req: dict) -> Response:
    fr = _frame_or_404(ctx, req["path"]["frame_id"])
    w = _int(req["query"].get("w"), "w", lo=32, hi=1280, default=320)
    key = (fr["frame_id"], w)
    with ctx._thumb_lock:
        data = ctx._thumbs.get(key)
        if data is not None:
            ctx._thumbs.move_to_end(key)
    if data is None:
        from PIL import Image

        with Image.open(_frame_path(ctx, fr)) as im:
            im = im.convert("RGB")
            if im.width > w:
                im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
        data = buf.getvalue()
        with ctx._thumb_lock:
            ctx._thumbs[key] = data
            while len(ctx._thumbs) > MAX_THUMB_CACHE:
                ctx._thumbs.popitem(last=False)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": IMAGE_CACHE})


def api_apps(ctx: V2Context, req: dict) -> dict:
    qp = req["query"]
    rows = ctx.queries.apps(_int(qp.get("from"), "from"), _int(qp.get("to"), "to"))
    return {"apps": [{"app": r["app"], "n": r["segments"]} for r in rows[:500]]}


# ---------------- 设置 ----------------

def _llm_raw(ctx: V2Context) -> dict[str, str]:
    from ..app.llm import ENV_KEYS, read_env_file

    merged: dict[str, str] = {}
    for f in ctx.env_files():
        merged.update({k: v for k, v in read_env_file(Path(f)).items() if k in ENV_KEYS and v})
    env = os.environ if ctx.llm_environ is None else ctx.llm_environ
    merged.update({k: env[k] for k in ENV_KEYS if env.get(k)})
    return merged


def _llm_info(ctx: V2Context) -> dict:
    from ..app.llm import LlmClient, LlmError

    raw = _llm_raw(ctx)
    try:
        LlmClient(raw.get("LLM_BASE_URL", ""), raw.get("LLM_API_KEY", ""), raw.get("LLM_MODEL", ""))
        configured = True
    except LlmError:
        configured = False
    host = urlsplit(raw["LLM_BASE_URL"]).hostname if raw.get("LLM_BASE_URL") else None
    return {"configured": configured, "model": raw.get("LLM_MODEL") or None, "host": host}


def api_settings(ctx: V2Context, _req: dict) -> dict:
    ctrl = ctx.stack.control.get()
    cfg = ctx.stack.cfg
    us = v2config.read_user_settings(ctx.data_dir)
    hidden = ctrl.get("hidden_from_agents") or {}
    try:
        auto = bool(ctx.get_autostart().is_enabled())
    except Exception:  # noqa: BLE001 - 读注册表失败只当没开
        log.warning("读取开机自启状态失败", exc_info=True)
        auto = False
    pinned = [p for p in (ctrl.get("pinned_windows") or []) if isinstance(p, dict)]
    return {
        # hwnd 原样带回（页面改名单时整份写回，不带的话下次保存就丢了；录制进程优先按它找窗口，标题会变）；
        # found：现在能不能找到这个窗口（同 rec_control.resolve_pins 的规则；列不出窗口时为 null）
        "pinned": [{"process": p.get("process") or "", "title": p.get("title") or "",
                    "hwnd": p.get("hwnd") if isinstance(p.get("hwnd"), int) else None, "found": f}
                   for p, f in zip(pinned, _pins_found(ctx, pinned))],
        "excluded_processes": list(ctrl.get("excluded_processes") or []),
        "excluded_title_keywords": list(ctrl.get("excluded_title_keywords") or []),
        "hidden_from_agents": {"apps": list(hidden.get("apps") or []),
                               "title_keywords": list(hidden.get("title_keywords") or [])},
        "agent_enabled": bool(ctrl.get("agent_enabled")),
        "idle_minutes": us.get("idle_minutes", cfg.idle.threshold_s / 60.0),
        "ocr_model": us.get("ocr_model", cfg.ocr.model),
        "autostart": auto,
        "llm": _llm_info(ctx),
        "data_dir": str(ctx.data_dir),
    }


def _pins_found(ctx: V2Context, pinned: list[dict]) -> list[bool | None]:
    if not pinned:
        return []
    from .rec_control import _pins, resolve_pins

    try:
        wins = [(int(w.hwnd), w.process or "", w.title or "") for w in _window_list(ctx)()]
    except Exception:  # noqa: BLE001 - 列不出窗口：不知道
        log.warning("列出窗口失败", exc_info=True)
        return [None] * len(pinned)
    return [h is not None for h in resolve_pins(_pins(pinned), wins)]


def _window_list(ctx: V2Context) -> Callable[[], list]:
    if ctx.list_windows is not None:
        return ctx.list_windows
    from .. import winapi
    return winapi.list_windows


_SETTING_KEYS = {"pinned", "excluded_processes", "excluded_title_keywords", "hidden_from_agents", "agent_enabled",
                 "idle_minutes", "ocr_model", "autostart"}


def api_settings_post(ctx: V2Context, req: dict) -> dict:
    body = req["body"]
    unknown = sorted(set(body) - _SETTING_KEYS)
    if unknown:
        raise ApiError(400, f"不认识的设置：{'、'.join(unknown)}")
    control: dict[str, Any] = {}
    user: dict[str, Any] = {}
    if "pinned" in body:
        if not isinstance(body["pinned"], list) or len(body["pinned"]) > MAX_PINNED:
            raise ApiError(400, f"最多固定 {MAX_PINNED} 个窗口")
        pinned = []
        for p in body["pinned"]:
            if not isinstance(p, dict):
                raise ApiError(400, "pinned 的每一项应为 {process, title}")
            item: dict[str, Any] = {"process": _str(p.get("process"), "process", cap=260),
                                    "title": _str(p.get("title", ""), "title", cap=500, allow_empty=True)}
            if p.get("hwnd") is not None:
                item["hwnd"] = _int(p["hwnd"], "hwnd", lo=0)
            pinned.append(item)
        control["pinned_windows"] = pinned
    for k in ("excluded_processes", "excluded_title_keywords"):
        if k in body:
            control[k] = _str_list(body[k], k)
    if "hidden_from_agents" in body:
        h = body["hidden_from_agents"]
        if not isinstance(h, dict) or set(h) - {"apps", "title_keywords"}:
            raise ApiError(400, "hidden_from_agents 应为 {apps, title_keywords}")
        cur = ctx.stack.control.get().get("hidden_from_agents") or {}
        control["hidden_from_agents"] = {
            "apps": _str_list(h["apps"], "apps") if "apps" in h else list(cur.get("apps") or []),
            "title_keywords": (_str_list(h["title_keywords"], "title_keywords") if "title_keywords" in h
                               else list(cur.get("title_keywords") or [])),
        }
    if "agent_enabled" in body:
        if not isinstance(body["agent_enabled"], bool):
            raise ApiError(400, "agent_enabled 应为 true / false")
        control["agent_enabled"] = body["agent_enabled"]
    if "idle_minutes" in body:
        v = body["idle_minutes"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 24 * 60:
            raise ApiError(400, "idle_minutes 应在 1 到 1440 之间")
        user["idle_minutes"] = float(v)
    if "ocr_model" in body:
        if body["ocr_model"] not in v2config.OCR_MODELS:
            raise ApiError(400, "ocr_model 只能是 tiny 或 small")
        user["ocr_model"] = body["ocr_model"]
    if "autostart" in body and not isinstance(body["autostart"], bool):
        raise ApiError(400, "autostart 应为 true / false")
    # 全部校验通过才开始写
    if control:
        ctx.stack.set_control(**control)
    if user:
        v2config.write_user_settings(ctx.data_dir, user)
    if "autostart" in body:
        a = ctx.get_autostart()
        try:
            if body["autostart"]:
                a.enable(_autostart_command(ctx))
            else:
                a.disable()
        except OSError as exc:
            raise ApiError(500, f"修改开机自启失败：{type(exc).__name__}") from None
    if ctx.on_settings:
        ctx.on_settings({**control, **user, **({"autostart": body["autostart"]} if "autostart" in body else {})})
    return api_settings(ctx, req)


def _autostart_command(ctx: V2Context) -> str | None:
    """默认数据目录时用默认命令；自定义数据目录把 --data-dir 带上。"""
    from ..app import autostart

    if ctx.data_dir.resolve() == (paths.app_home() / "data").resolve():
        return None
    return autostart.default_command(args=("--autostart", "--data-dir", str(ctx.data_dir)))


def api_settings_llm(ctx: V2Context, req: dict) -> dict:
    from ..app.llm import read_env_file

    body = req["body"]
    base = _str(body.get("base_url"), "base_url", cap=500)
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ApiError(400, "base_url 应为 http(s):// 开头的地址")
    model = _str(body.get("model"), "model", cap=200)
    key = body.get("api_key")
    if key is not None and key != "":
        key = _str(key, "api_key", cap=2000)
        if any(c.isspace() for c in key):
            raise ApiError(400, "api_key 不能含空白字符")
    else:
        key = None  # 没给 = 保留原来的
    path = ctx.home / ".env"
    paths.ensure_private_dir(ctx.home)
    old = read_env_file(path)
    values = {"LLM_BASE_URL": base, "LLM_MODEL": model}
    if key is not None:
        values["LLM_API_KEY"] = key
    elif old.get("LLM_API_KEY"):
        values["LLM_API_KEY"] = old["LLM_API_KEY"]
    keep = {k: v for k, v in old.items() if k not in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")}
    # 值已校验过没有换行；read_env_file 不认行内注释，原样写入即可
    text = "".join(f"{k}={v}\n" for k, v in {**keep, **values}.items())
    paths.write_private_file(path, text.encode("utf-8"))
    return api_settings(ctx, req)


def api_settings_llm_test(ctx: V2Context, _req: dict) -> dict:
    from ..app.llm import LlmClient, LlmError

    raw = _llm_raw(ctx)
    t0 = time.monotonic()
    try:
        client = LlmClient(raw.get("LLM_BASE_URL", ""), raw.get("LLM_API_KEY", ""), raw.get("LLM_MODEL", ""),
                           max_retries=1, backoff_s=0.5, transport=ctx.llm_transport)
        reply = client.chat([{"role": "user", "content": LLM_TEST_PROMPT}], max_tokens=16, temperature=0,
                            reasoning_effort="none", timeout=30.0)
    except LlmError as exc:
        return {"ok": False, "model": raw.get("LLM_MODEL") or None,
                "latency_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)}
    return {"ok": True, "model": reply.model, "latency_ms": int((time.monotonic() - t0) * 1000)}


def api_windows(ctx: V2Context, _req: dict) -> dict:
    try:
        wins = _window_list(ctx)()
    except Exception as exc:  # noqa: BLE001
        log.warning("列出窗口失败", exc_info=True)
        raise ApiError(503, f"无法列出窗口：{type(exc).__name__}") from None
    out = [{"hwnd": int(w.hwnd), "process": w.process, "title": w.title} for w in wins if (w.title or "").strip()]
    return {"windows": out[:500]}


def _dir_bytes(root: Path) -> int:
    total = 0
    stack = [root]
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


def api_storage(ctx: V2Context, _req: dict) -> dict:
    cached = ctx._storage
    if cached is not None and time.monotonic() - cached[0] < STORAGE_CACHE_S:
        return dict(cached[1])
    d = ctx.data_dir
    db = ctx.stack.writer.db_path
    db_bytes = sum(p.stat().st_size for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm")) if p.exists())
    conn = ctx.queries.conn()
    frames = int(conn.execute("SELECT COUNT(*) FROM frames WHERE status = 'ok'").fetchone()[0])
    lines = int(conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0])
    olds = [conn.execute(sql).fetchone()[0] for sql in (
        "SELECT MIN(first_seen_ms) FROM lines", "SELECT MIN(captured_ms) FROM frames WHERE status = 'ok'",
        "SELECT MIN(start_ms) FROM activity")]
    olds = [int(x) for x in olds if x is not None]
    # vectors_bytes：整个 vectors 目录（文字 + 图片向量）
    out = {"data_dir": str(d), "db_bytes": db_bytes, "frames_bytes": _dir_bytes(d / "frames"),
           "vectors_bytes": _dir_bytes(d / "vectors"), "frames": frames, "lines": lines,
           "oldest_ms": min(olds) if olds else None}
    ctx._storage = (time.monotonic(), out)
    return dict(out)


def api_delete(ctx: V2Context, req: dict) -> dict:
    body = req["body"]
    keys = set(body)
    if keys - {"recent_minutes", "from_ms", "to_ms", "app"}:
        raise ApiError(400, "不认识的参数：" + "、".join(sorted(keys - {"recent_minutes", "from_ms", "to_ms", "app"})))
    app = _str(body["app"], "app", cap=260) if body.get("app") is not None else None
    now = ctx.stack.clock()
    if "recent_minutes" in body:
        if keys - {"recent_minutes"}:
            raise ApiError(400, "recent_minutes 不能和其他条件一起用")
        m = _int(body["recent_minutes"], "recent_minutes", lo=1, hi=366 * 24 * 60)
        t_from, t_to = now - m * 60_000, None
    elif "from_ms" in body or "to_ms" in body:
        t_from = _int(body.get("from_ms"), "from_ms")
        t_to = _int(body.get("to_ms"), "to_ms")
        if t_from is None or t_to is None:
            raise ApiError(400, "请同时给出 from_ms 和 to_ms")
        if t_to <= t_from:
            raise ApiError(400, "结束时间必须晚于开始时间")
    elif app:
        t_from = t_to = None
    else:
        raise ApiError(400, "请给出要删除的范围：recent_minutes、from_ms + to_ms 或 app")
    rep = ctx.stack.deleter.delete(time_from=t_from, time_to=t_to, app=app)
    ctx.forget_caches()
    return {"deleted": rep}


def api_reset_token(ctx: V2Context, _req: dict) -> Response:
    if ctx.rotate_token is None:
        raise ApiError(503, "这个版本不支持重置访问令牌")
    cookie = ctx.rotate_token()
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store", "Set-Cookie": cookie})


# ---------------- 路由 ----------------

def _endpoint(ctx: V2Context, fn: Callable[[V2Context, dict], Any], *, mutation: bool = False):
    async def handler(request: Request) -> Response:
        if mutation and request.headers.get("x-screenrecall") != "1":
            return _error(403, "缺少 X-ScreenRecall 请求头")
        body: dict = {}
        if mutation:
            raw = await request.body()
            if raw.strip():
                try:
                    body = json.loads(raw)
                except ValueError:
                    return _error(400, "请求内容不是有效的 JSON")
                if not isinstance(body, dict):
                    return _error(400, "请求内容应为 JSON 对象")
        req = {"query": dict(request.query_params), "path": dict(request.path_params), "body": body}
        try:
            out = await run_in_threadpool(fn, ctx, req)
        except ApiError as exc:
            return _error(exc.status, exc.message)
        except Exception:  # noqa: BLE001
            log.exception("接口 %s 出错", request.url.path)
            return _error(500, "服务内部出错，请稍后再试（详情见日志）")
        if isinstance(out, Response):
            return out
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    return handler


def _error(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status, headers={"Cache-Control": "no-store"})


def routes(ctx: V2Context) -> list[BaseRoute]:
    """挂到 server.build_app(extra_routes=...) 上的路由（都在 /api/v2 下）。"""
    e = lambda fn, **kw: _endpoint(ctx, fn, **kw)  # noqa: E731

    async def not_found(_request: Request) -> Response:
        return _error(404, "找不到这个接口")

    r = [
        Route("/status", e(api_status), methods=["GET"]),
        Route("/pause", e(api_pause, mutation=True), methods=["POST"]),
        Route("/resume", e(api_resume, mutation=True), methods=["POST"]),
        Route("/day", e(api_day), methods=["GET"]),
        Route("/pieces/{piece_id}", e(api_piece), methods=["GET"]),
        Route("/pieces/{piece_id}/summarize", e(api_summarize, mutation=True), methods=["POST"]),
        Route("/search", e(api_search), methods=["GET"]),
        Route("/frames/{frame_id}", e(api_frame), methods=["GET"]),
        Route("/frames/{frame_id}/image", e(api_frame_image), methods=["GET"]),
        Route("/frames/{frame_id}/thumb", e(api_frame_thumb), methods=["GET"]),
        Route("/apps", e(api_apps), methods=["GET"]),
        Route("/settings", e(api_settings), methods=["GET"]),
        Route("/settings", e(api_settings_post, mutation=True), methods=["POST"]),
        Route("/settings/llm", e(api_settings_llm, mutation=True), methods=["POST"]),
        Route("/settings/llm/test", e(api_settings_llm_test, mutation=True), methods=["POST"]),
        Route("/windows", e(api_windows), methods=["GET"]),
        Route("/storage", e(api_storage), methods=["GET"]),
        Route("/delete", e(api_delete, mutation=True), methods=["POST"]),
        Route("/agent/reset-token", e(api_reset_token, mutation=True), methods=["POST"]),
        Route("/{rest:path}", not_found),
    ]
    return [Mount("/api/v2", routes=r)]
