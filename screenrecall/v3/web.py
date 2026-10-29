"""网页 API v3：/api/v3/*（挂在主进程的 AuthGuard 里面，Cookie 或 Bearer 令牌才能进，这里不再做认证）。

- 读：v3.store.Queries（只读）；写：v3.store.Store（暂停、名单、不录规则、设置、删除）。两个都线程安全，
  每个接口都放到线程池里跑；
- GET 不改数据；会改东西的 POST 要带 X-ScreenRecall: 1 请求头（别的网页发不出这个头），body 是 JSON 对象；
- 参数错 400 {"error": 中文}；找不到 404；出错 500 只说"详情见日志"；所有响应 Cache-Control: no-store；
- 时间都是 UTC 毫秒整数，页面按本地时间显示。接口和 JSON 形状见 V3_CONTRACT.md §3.6。
"""
from __future__ import annotations

import dataclasses
import io
import json
import logging
import os
import re
import threading
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

from ..timeutil import local_dates_to_utc_range, utc_ms
from .config import TIERS, V3Config

log = logging.getLogger("screenrecall.v3.web")

PAUSE_MIN, PAUSE_MAX = 5, 60          # 暂停 5–60 分钟（方案：网页可以暂停 5–60 分钟）
MAX_QUERY_CHARS = 500
MAX_SEARCH_LIMIT = 100
MAX_COPIED_LIMIT = 500
MAX_SOURCE_LINES = 500
MAX_PROCESS_CHARS = 260               # Windows 路径上限，进程名不会更长
MAX_EXCLUDE_CHARS = 200
THUMB_DEFAULT_W, THUMB_MIN_W, THUMB_MAX_W = 240, 32, 480
MAX_THUMB_CACHE = 200
MAX_DELETE_MINUTES = 366 * 24 * 60
# no-store：截图不能留在浏览器的磁盘缓存里，不然"删除"之后图片还在（隐私删除要真删）
IMAGE_CACHE = "private, no-store"
ICON_CACHE = "private, max-age=86400"  # 软件图标不是屏幕内容，可以缓存一天
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")
# 设置页展示的"能调的数字"：V3Config 里的整数 / 小数字段；端口不算（被占用时自动换，不是规则里的数字）
_NOT_NUMBERS = {"port"}
_SETTING_KEYS = {"agent_enabled", "autostart"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class V3Context:
    """网页 API 要用的东西。cfg / store / queries / home 之外都有默认值，测试按需替换。"""
    cfg: V3Config
    store: Any                   # v3.store.Store：写（暂停、名单、不录规则、设置）
    queries: Any                 # v3.store.Queries：读
    home: Path
    rotate_token: Callable[[], str] | None = None   # 换令牌，返回新的 Set-Cookie 值
    autostart: Any = None                           # is_enabled() / enable(cmd) / disable()；None = app.autostart
    semantic: Callable[[str, int], list[tuple[int, float]]] | None = None
    open_in_explorer: Callable[[Path], None] | None = None   # None = explorer.exe /select,<路径>
    clock: Callable[[], int] = utc_ms
    running_apps: Callable[[], list[dict]] | None = None     # None = winapi.running_apps（不含本进程）
    installed: Callable[[str], bool] | None = None           # None = winapi.app_installed（App Paths 里登记过）
    agents: Any = None                                       # quick_status() / install_all()；None = app.agents
    icons: Any = None                                        # info(process, running) / running_paths()；None = appicons.AppIcons(winapi)
    # 约定之外加的两个可选项（默认行为和 app.llm 一样）：测试用临时 .env，不碰仓库里真的 .env
    llm_env_files: Sequence[Path] | None = None     # None = 仓库 .env + home/.env
    llm_environ: dict[str, str] | None = None       # None = os.environ
    _thumbs: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)
    _thumb_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

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

    def get_icons(self):
        if self.icons is None:
            from .appicons import AppIcons
            try:
                from .. import winapi
            except ImportError:
                winapi = None
            self.icons = AppIcons(winapi)
        return self.icons

    def forget_thumbs(self) -> None:
        with self._thumb_lock:
            self._thumbs.clear()


# ---------------- 参数 ----------------

def _int(v: Any, name: str, *, lo: int | None = None, hi: int | None = None, default: int | None = None) -> int | None:
    if v is None or v == "":
        return default
    if isinstance(v, bool) or isinstance(v, float) and not v.is_integer():
        raise ApiError(400, f"{name} 应为整数")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} 应为整数") from None
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        lo_s = "" if lo is None else lo
        hi_s = "" if hi is None else hi
        raise ApiError(400, f"{name} 超出范围（{lo_s} 到 {hi_s}）")
    return n


def _str(v: Any, name: str, *, cap: int) -> str:
    if not isinstance(v, str):
        raise ApiError(400, f"{name} 应为文字")
    s = v.strip()
    if not s:
        raise ApiError(400, f"{name} 不能为空")
    if len(s) > cap or any(c in s for c in "\r\n\x00"):
        raise ApiError(400, f"{name} 太长或含有换行")
    return s


def _range(qp: dict, lo_key: str = "from_ms", hi_key: str = "to_ms") -> tuple[int | None, int | None]:
    a = _int(qp.get(lo_key), lo_key, lo=0)
    b = _int(qp.get(hi_key), hi_key, lo=0)
    if a is not None and b is not None and b <= a:
        raise ApiError(400, "结束时间必须晚于开始时间")
    return a, b


def _parse_day(s: str | None) -> date:
    if not s:
        return datetime.now().date()
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ApiError(400, "day 应为 YYYY-MM-DD") from None


def _found(x: Any, what: str) -> Any:
    if x is None:
        raise ApiError(404, f"找不到这个{what}：可能已被删除")
    return x


# ---------------- 搜索结果里的高亮（照 v2 的 match_spans） ----------------

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


def match_spans(text: str, terms: Sequence[str | tuple[str, bool]], *, tails: bool = True) -> list[list[int]]:
    """各检索词（已 norm）在原文里的位置 [start, end)，按字符（码点）计；大小写、全半角、空白都不敏感。
    词可以是 (norm, word_prefix)：word_prefix（字母数字的短词，如 a、to）只认词首，
    不然 "a" 会把 rearranged 里的每个 a 都标出来。
    tails=True（字面命中的行）：折到下一行的命中（词的开头在本行末尾）标出本行里的那一截；
    只按意思搜到的行不是字面命中，tails=False。"""
    folded, idx = _fold_map(text)
    spans: list[list[int]] = []
    for item in terms:
        t, word_start = (item, False) if isinstance(item, str) else item
        if not t:
            continue
        start, found = 0, False
        while True:
            j = folded.find(t, start)
            if j < 0:
                break
            start = j + 1
            if word_start and idx[j] > 0 and text[idx[j] - 1].isalnum():
                continue
            spans.append([idx[j], idx[j + len(t) - 1] + 1])
            found = True
        if not found and tails:
            for k in range(min(len(t) - 1, len(folded)), 0, -1):
                p = len(folded) - k
                if word_start and idx[p] > 0 and text[idx[p] - 1].isalnum():
                    continue
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


def _terms(q: str, res: dict) -> list[str | tuple[str, bool]]:
    """高亮用的词：优先用 lexical.plan（带"只认词首"），取不到时退回 Queries.search 返回的 terms。"""
    try:
        from ..store.fts import lexical
        plan = lexical.plan(q)
        if not plan.error:
            return [(t.text, bool(t.word_prefix)) for t in plan.terms]
    except Exception:  # noqa: BLE001 - lexical 的实现由 store 单元维护，取不到就用结果里的
        log.debug("lexical.plan 不可用", exc_info=True)
    out: list[str | tuple[str, bool]] = []
    for t in res.get("terms") or []:
        if isinstance(t, str):
            out.append(t)
        elif isinstance(t, dict) and isinstance(t.get("text"), str):
            out.append((t["text"], bool(t.get("word_prefix"))))
        elif isinstance(t, (list, tuple)) and t and isinstance(t[0], str):
            out.append((t[0], bool(t[1]) if len(t) > 1 else False))
    return out


# ---------------- 各接口 ----------------

def api_status(ctx: V3Context, _req: dict) -> dict:
    return ctx.queries.status(ctx.clock())


def api_pause(ctx: V3Context, req: dict) -> dict:
    minutes = _int(req["body"].get("minutes"), "minutes", lo=PAUSE_MIN, hi=PAUSE_MAX)
    if minutes is None:
        raise ApiError(400, f"请给出暂停多少分钟（minutes，{PAUSE_MIN} 到 {PAUSE_MAX}）")
    now = ctx.clock()
    ctx.store.set_control(paused_until_ms=now + minutes * 60_000)
    return ctx.queries.status(now)


def api_resume(ctx: V3Context, _req: dict) -> dict:
    ctx.store.set_control(paused_until_ms=None)
    return ctx.queries.status(ctx.clock())


_NO_APP_KEYS = frozenset({"lines", "hits", "boxes", "hours"})  # 这些列表里没有软件，不用翻


def _with_app_looks(ctx: V3Context, out):
    """返回里每个带 app（进程名）的对象加上 app_name（exe 里写的软件名，取不到 = null）和
    app_icon（有没有图标可取，appIconUrl）。时间线、条目、查找、复制过的都用，页面不用再显示进程名。"""
    icons = ctx.get_icons()
    running: dict[str, str] | None = None
    looks: dict[str, tuple[str | None, bool]] = {}

    def look(app: str) -> tuple[str | None, bool]:
        nonlocal running
        low = app.lower()
        if low not in looks:
            if running is None:
                running = icons.running_paths()
            info = icons.info(app, running)
            looks[low] = ((info or {}).get("name"), bool(info and info.get("png")))
        return looks[low]

    def walk(x) -> None:
        if isinstance(x, dict):
            app = x.get("app")
            if isinstance(app, str) and app:
                x["app_name"], x["app_icon"] = look(app)
            for k, v in x.items():
                if k not in _NO_APP_KEYS and isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(out)
    return out


def api_timeline(ctx: V3Context, req: dict) -> dict:
    qp = req["query"]
    now = ctx.clock()
    if qp.get("from_ms") or qp.get("to_ms"):
        lo, hi = _range(qp)
        if lo is None or hi is None:
            raise ApiError(400, "请同时给出 from_ms 和 to_ms")
    else:
        d = _parse_day(qp.get("day"))
        lo, hi = local_dates_to_utc_range(d, d)
    return _with_app_looks(ctx, ctx.queries.timeline(lo, hi, now_ms=now))


def api_entry(ctx: V3Context, req: dict) -> dict:
    vid = _int(req["path"]["visit_id"], "visit_id", lo=1)
    return _with_app_looks(ctx, _found(ctx.queries.entry(vid), "条目"))


def api_source(ctx: V3Context, req: dict) -> dict:
    sid = _int(req["path"]["source_id"], "source_id", lo=1)
    qp = req["query"]
    offset = _int(qp.get("offset"), "offset", lo=0, hi=1_000_000, default=0)
    limit = _int(qp.get("limit"), "limit", lo=1, hi=MAX_SOURCE_LINES, default=200)
    return _with_app_looks(ctx, _found(ctx.queries.source(sid, offset=offset, limit=limit), "出处"))


def api_search(ctx: V3Context, req: dict) -> dict:
    qp = req["query"]
    q = (qp.get("q") or "").strip()
    if not q:
        raise ApiError(400, "请输入要搜索的文字")
    if len(q) > MAX_QUERY_CHARS:
        raise ApiError(400, f"搜索的文字太长了（最多 {MAX_QUERY_CHARS} 字）")
    lo, hi = _range(qp)
    limit = _int(qp.get("limit"), "limit", lo=1, hi=MAX_SEARCH_LIMIT, default=30)
    app = (qp.get("app") or "").strip() or None
    res = ctx.queries.search(q, start_ms=lo, end_ms=hi, app=app, limit=limit, semantic=ctx.semantic)
    terms = _terms(q, res)
    for r in res.get("results") or []:
        for h in r.get("hits") or []:
            h["spans"] = match_spans(h.get("text") or "", terms, tails=not h.get("semantic"))
    return _with_app_looks(ctx, res)


def api_copied(ctx: V3Context, req: dict) -> dict:
    qp = req["query"]
    q = (qp.get("q") or "").strip() or None
    if q is not None and len(q) > MAX_QUERY_CHARS:
        raise ApiError(400, f"搜索的文字太长了（最多 {MAX_QUERY_CHARS} 字）")
    lo, hi = _range(qp)
    limit = _int(qp.get("limit"), "limit", lo=1, hi=MAX_COPIED_LIMIT, default=100)
    return _with_app_looks(ctx, {"items": ctx.queries.copied(q=q, start_ms=lo, end_ms=hi, limit=limit)})


def _shot_id(req: dict) -> int:
    return _int(req["path"]["shot_id"], "shot_id", lo=1)


def _shot_path(ctx: V3Context, shot_id: int) -> Path:
    p = ctx.queries.shot_file(shot_id)
    if p is None or not Path(p).is_file():
        raise ApiError(404, "找不到这张截图：可能已被删除（截图留 30 天，字一直留着）")
    return Path(p)


def api_shot(ctx: V3Context, req: dict) -> dict:
    return _found(ctx.queries.shot(_shot_id(req)), "截图")


def api_shot_image(ctx: V3Context, req: dict) -> Response:
    return FileResponse(_shot_path(ctx, _shot_id(req)), media_type="image/jpeg", headers={"Cache-Control": IMAGE_CACHE})


def api_shot_thumb(ctx: V3Context, req: dict) -> Response:
    sid = _shot_id(req)
    w = _int(req["query"].get("w"), "w", lo=THUMB_MIN_W, hi=THUMB_MAX_W, default=THUMB_DEFAULT_W)
    # 先确认这张图还在（托盘 / 空闲整理进程删的图也不能从缓存里再出来）；文件名是随机的，按路径缓存
    p = _shot_path(ctx, sid)
    key = (sid, w, str(p))
    with ctx._thumb_lock:
        data = ctx._thumbs.get(key)
        if data is not None:
            ctx._thumbs.move_to_end(key)
    if data is None:
        from PIL import Image

        with Image.open(p) as im:
            im = im.convert("RGB")
            if im.width > w:
                im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
        with ctx._thumb_lock:
            ctx._thumbs[key] = data
            while len(ctx._thumbs) > MAX_THUMB_CACHE:
                ctx._thumbs.popitem(last=False)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": IMAGE_CACHE})


def _installed_fn(ctx: V3Context) -> Callable[[str], bool]:
    if ctx.installed is not None:
        return ctx.installed
    try:
        from .. import winapi
    except ImportError:
        return lambda _p: True
    return getattr(winapi, "app_installed", None) or (lambda _p: True)


def api_apps(ctx: V3Context, _req: dict) -> dict:
    """名单 + 每个软件 installed：默认名单里的浏览器这台电脑上装了没有（没装的页面上收起来）；
    用户自己加的软件不判断（很多软件不在 App Paths 里登记），一律 True。"""
    out = ctx.queries.apps()
    defaults = {a.lower() for a in ctx.cfg.default_record_apps}
    found = _installed_fn(ctx)
    icons = ctx.get_icons()
    running = icons.running_paths()
    for a in out["apps"]:
        a["installed"] = True
        if a["process"].lower() in defaults:
            try:
                a["installed"] = bool(found(a["process"]))
            except Exception:  # noqa: BLE001
                pass
        _add_look(a, icons.info(a["process"], running))
    for u in out.get("unlisted", []):
        _add_look(u, icons.info(u.get("app") or "", running))
    return out


def _add_look(item: dict, info: dict | None) -> None:
    """名单页显示用：name = exe 的"文件说明"（没有 = null，页面用进程名），icon = 有没有图标可取。"""
    if not item.get("name"):
        item["name"] = (info or {}).get("name")
    item["icon"] = bool(info and info.get("png"))


def api_app_icon(ctx: V3Context, req: dict) -> Response:
    process = _str(req["query"].get("process"), "process", cap=MAX_PROCESS_CHARS)
    info = ctx.get_icons().info(process)
    if not info or not info.get("png"):
        raise ApiError(404, "这个软件取不到图标")
    return Response(info["png"], media_type="image/png", headers={"Cache-Control": ICON_CACHE})


def _agents(ctx: V3Context):
    if ctx.agents is not None:
        return ctx.agents
    from ..app import agents

    return agents


def api_agents(ctx: V3Context, _req: dict) -> dict:
    return _agents(ctx).quick_status()


def api_agents_install(ctx: V3Context, _req: dict) -> dict:
    mod = _agents(ctx)
    return {"messages": mod.install_all(), **mod.quick_status()}


def api_apps_running(ctx: V3Context, _req: dict) -> dict:
    """现在开着窗口的软件，带它在名单里的档（不在名单 = null）。"""
    if ctx.running_apps is not None:
        found = ctx.running_apps()
    else:
        try:
            from .. import winapi
        except ImportError:
            found = []
        else:
            found = winapi.running_apps(frozenset({os.getpid()}))
    tiers = {a["process"].lower(): a["tier"] for a in ctx.queries.apps()["apps"]}
    icons = ctx.get_icons()
    running = icons.running_paths()
    out = []
    for a in found:
        item = {**a, "tier": tiers.get(a["process"].lower())}
        _add_look(item, icons.info(a["process"], running))
        out.append(item)
    return {"running": out}


def api_apps_post(ctx: V3Context, req: dict) -> dict:
    body = req["body"]
    process = _str(body.get("process"), "process", cap=MAX_PROCESS_CHARS)
    if any(c in process for c in '\\/:*?"<>|'):
        raise ApiError(400, "process 应为进程名（如 msedge.exe），不是路径")
    process = process.strip()
    if process and "." not in process:  # 录制进程拿到的进程名都带扩展名："Notepad" 永远对不上 notepad.exe
        process += ".exe"
    tier = body.get("tier")
    if tier not in TIERS:
        raise ApiError(400, "tier 只能是 record（录）、always（一直录）或 never（不录）")
    ctx.store.set_app_tier(process, tier)
    return api_apps(ctx, {})


def _norm_host(v: str) -> str:
    """不录网站：只要主机名。贴进来整条网址也行（取主机名），小写，去掉末尾的点。"""
    s = v.strip().lower()
    if "://" in s:
        try:
            s = urlsplit(s).hostname or ""
        except ValueError:
            s = ""
    else:
        s = s.split("/", 1)[0].split(":", 1)[0]
    s = s.rstrip(".")
    if not s or not _HOST.match(s):
        raise ApiError(400, "网站应为主机名，如 example.com")
    return s


def api_excludes_post(ctx: V3Context, req: dict) -> dict:
    body = req["body"]
    kind = body.get("kind")
    if kind not in ("host", "title"):
        raise ApiError(400, "kind 只能是 host（网站）或 title（窗口标题关键词）")
    value = _str(body.get("value"), "value", cap=MAX_EXCLUDE_CHARS)
    if kind == "host":
        value = _norm_host(value)
    ctx.store.add_exclude(kind, value)
    return api_apps(ctx, {})


def api_excludes_remove(ctx: V3Context, req: dict) -> dict:
    eid = _int(req["body"].get("exclude_id"), "exclude_id", lo=1)
    if eid is None:
        raise ApiError(400, "请给出 exclude_id")
    ctx.store.remove_exclude(eid)
    return api_apps(ctx, {})


def _open_in_explorer(path: Path) -> None:
    import subprocess

    # 参数数组、不经过 shell；/select 只是在资源管理器里选中它，不打开（不执行）文件
    subprocess.Popen(["explorer.exe", f"/select,{path}"], close_fds=True)


def api_reveal(ctx: V3Context, req: dict) -> dict:
    sid = _int(req["path"]["source_id"], "source_id", lo=1)
    src = _found(ctx.queries.source(sid, offset=0, limit=1), "出处")["source"]
    if src.get("kind") != "file" or not src.get("path"):
        raise ApiError(400, "只有本地文件能在文件夹中显示")
    p = Path(src["path"])
    if not p.is_absolute() or not p.is_file():
        raise ApiError(404, "这个文件已经不在原来的位置了")
    try:
        (ctx.open_in_explorer or _open_in_explorer)(p)
    except OSError as exc:
        raise ApiError(500, f"打不开资源管理器：{type(exc).__name__}") from None
    return {"ok": True}


# ---------------- 设置 ----------------

def _llm_info(ctx: V3Context) -> dict:
    """只说配没配、模型名；从不返回密钥和接口地址。"""
    from ..app.llm import ENV_KEYS, LlmClient, LlmError, read_env_file

    raw: dict[str, str] = {}
    for f in ctx.env_files():
        raw.update({k: v for k, v in read_env_file(Path(f)).items() if k in ENV_KEYS and v})
    env = os.environ if ctx.llm_environ is None else ctx.llm_environ
    raw.update({k: env[k] for k in ENV_KEYS if env.get(k)})
    try:
        LlmClient(raw.get("LLM_BASE_URL", ""), raw.get("LLM_API_KEY", ""), raw.get("LLM_MODEL", ""))
        configured = True
    except LlmError:
        configured = False
    return {"configured": configured, "model": raw.get("LLM_MODEL") or None}


def config_numbers(cfg: V3Config) -> dict[str, int | float]:
    """V3Config 里所有能调的数字（键名和 config.default.toml 的 [v3] 一样）。"""
    out: dict[str, int | float] = {}
    for f in dataclasses.fields(cfg):
        v = getattr(cfg, f.name)
        if f.name in _NOT_NUMBERS or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out[f.name] = v
    return out


def api_settings(ctx: V3Context, _req: dict) -> dict:
    try:
        auto = bool(ctx.get_autostart().is_enabled())
    except Exception:  # noqa: BLE001 - 读注册表失败只当没开
        log.warning("读取开机自启状态失败", exc_info=True)
        auto = False
    ctrl = ctx.queries.control() or {}
    return {"numbers": config_numbers(ctx.cfg), "data_dir": str(ctx.cfg.data_dir), "llm": _llm_info(ctx),
            "agent_enabled": bool(ctrl.get("agent_enabled", 1)), "autostart": auto}


def _autostart_command(ctx: V3Context) -> str | None:
    """默认位置时用默认命令；应用目录或数据目录不是默认的，把 --home / --data-dir 带上（注册表里的命令没有环境变量）。"""
    from ..app import autostart

    extra = autostart.location_args(ctx.home, ctx.cfg.data_dir)
    if not extra:
        return None
    return autostart.default_command(args=("--autostart", *extra))


def api_settings_post(ctx: V3Context, req: dict) -> dict:
    body = req["body"]
    unknown = sorted(set(body) - _SETTING_KEYS)
    if unknown:
        raise ApiError(400, f"不认识的设置：{'、'.join(unknown)}")
    for k in _SETTING_KEYS & set(body):
        if not isinstance(body[k], bool):
            raise ApiError(400, f"{k} 应为 true / false")
    if "agent_enabled" in body:
        ctx.store.set_control(agent_enabled=1 if body["agent_enabled"] else 0)
    if "autostart" in body:
        a = ctx.get_autostart()
        try:
            if body["autostart"]:
                a.enable(_autostart_command(ctx))
            else:
                a.disable()
        except OSError as exc:
            raise ApiError(500, f"修改开机自启失败：{type(exc).__name__}") from None
    return api_settings(ctx, req)


def api_delete(ctx: V3Context, req: dict) -> dict:
    body = req["body"]
    keys = set(body)
    if keys - {"minutes", "from_ms", "to_ms"}:
        raise ApiError(400, "不认识的参数：" + "、".join(sorted(keys - {"minutes", "from_ms", "to_ms"})))
    now = ctx.clock()
    if "minutes" in body:
        if keys - {"minutes"}:
            raise ApiError(400, "minutes 不能和 from_ms / to_ms 一起用")
        m = _int(body["minutes"], "minutes", lo=1, hi=MAX_DELETE_MINUTES)
        if m is None:
            raise ApiError(400, "请给出要删除最近多少分钟（minutes）")
        lo, hi = now - m * 60_000, now + 1      # 包括这一刻
    elif "from_ms" in body or "to_ms" in body:
        lo, hi = _range(body)
        if lo is None or hi is None:
            raise ApiError(400, "请同时给出 from_ms 和 to_ms")
    else:
        raise ApiError(400, "请给出要删除的范围：minutes，或 from_ms + to_ms")
    counts = ctx.store.delete_range(lo, hi)
    ctx.forget_thumbs()
    return counts


def api_reset_token(ctx: V3Context, _req: dict) -> Response:
    if ctx.rotate_token is None:
        raise ApiError(503, "这个版本不支持重置访问令牌")
    cookie = ctx.rotate_token()
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store", "Set-Cookie": cookie})


# ---------------- 路由 ----------------

def _endpoint(ctx: V3Context, fn: Callable[[V3Context, dict], Any], *, mutation: bool = False):
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


def routes(ctx: V3Context) -> list[BaseRoute]:
    """挂到 server.build_app(extra_routes=...) 上的路由（都在 /api/v3 下）。"""
    e = lambda fn, **kw: _endpoint(ctx, fn, **kw)  # noqa: E731

    async def not_found(_request: Request) -> Response:
        return _error(404, "找不到这个接口")

    r = [
        Route("/status", e(api_status), methods=["GET"]),
        Route("/pause", e(api_pause, mutation=True), methods=["POST"]),
        Route("/resume", e(api_resume, mutation=True), methods=["POST"]),
        Route("/timeline", e(api_timeline), methods=["GET"]),
        Route("/entries/{visit_id}", e(api_entry), methods=["GET"]),
        Route("/sources/{source_id}", e(api_source), methods=["GET"]),
        Route("/sources/{source_id}/reveal", e(api_reveal, mutation=True), methods=["POST"]),
        Route("/search", e(api_search), methods=["GET"]),
        Route("/copied", e(api_copied), methods=["GET"]),
        Route("/shots/{shot_id}", e(api_shot), methods=["GET"]),
        Route("/shots/{shot_id}/image", e(api_shot_image), methods=["GET"]),
        Route("/shots/{shot_id}/thumb", e(api_shot_thumb), methods=["GET"]),
        Route("/apps", e(api_apps), methods=["GET"]),
        Route("/apps", e(api_apps_post, mutation=True), methods=["POST"]),
        Route("/apps/running", e(api_apps_running), methods=["GET"]),
        Route("/apps/icon", e(api_app_icon), methods=["GET"]),
        Route("/agents", e(api_agents), methods=["GET"]),
        Route("/agents/install", e(api_agents_install, mutation=True), methods=["POST"]),
        Route("/excludes", e(api_excludes_post, mutation=True), methods=["POST"]),
        Route("/excludes/remove", e(api_excludes_remove, mutation=True), methods=["POST"]),
        Route("/settings", e(api_settings), methods=["GET"]),
        Route("/settings", e(api_settings_post, mutation=True), methods=["POST"]),
        Route("/delete", e(api_delete, mutation=True), methods=["POST"]),
        Route("/agent/reset-token", e(api_reset_token, mutation=True), methods=["POST"]),
        Route("/{rest:path}", not_found),
    ]
    return [Mount("/api/v3", routes=r)]
