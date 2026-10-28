"""v3 处理线程：按顺序处理捕获循环的事件。读字 → 记出处 → 存字 → 留截图 → 时间线，所有写库都在这里（一个 Store）。

- FocusEvent：换了窗口 → 离开当前前台条目（visit_leave，带这一段累计的走开时间）；记下新的前台窗口和切进来的时间。
  条目等第一次拍、知道出处后才建，用切进来的时间 visit_enter。
  同一窗口只是标题变了：条目先留着，下一拍的出处变了才换（换的时间 = 标题变的时间）。
- SnapEvent：
  1. res = reader.read_text(hwnd, frame, want_ocr=…)：系统文字读完、要 OCR 之前先按 2 判一次，反正不存
     （或是本地文件、不存字）就不 OCR；
  2. 跳过（_skip_reason），什么都不写：辅助功能里的窗口名字带无痕关键词（private：Chrome 的无痕窗口标题里不写"无痕"，
     只在根下面的 Pane 名字里写"（无痕）"）；读字时窗口标题和拍的时候不一样（stale_page：拍完换了页面，图和字对不上）；
     source.url_skip_reason(...) 不为 None（浏览器内部页、新标签页、ScreenRecall 自己的网页、不录网站）；
     "带网址的软件"（默认名单 cfg.default_record_apps，加上库里出过网址出处的软件；只是数据）这一拍没读到网址
     → skip_no_url：认不出是不是内部页、自己的网页、不录网站，宁可不存（同一窗口上一拍存下的就是这个标题的网址出处时
     沿用那个网址，_known_url；查找栏拿着焦点时读不到网址）。别的软件的 Document 值不是 http(s) 时
     （app://、程序目录里的 file:///…）不当网址（_page_url），按 软件 + 标题 记；
  3. label = source.label_source(app, title, url, 网页标题)；网页没给标题（浏览器的 PDF 查看器）时用窗口标题去掉
     这个软件的尾巴（_page_name，从有网页标题的拍里学）；black = shots.is_black(frame.bgr)；
  4. upsert_source；前台：出处和当前条目的不一样（第一次拍、或同一窗口换了网址）→ 离开旧的、visit_enter(切进来时间或 t)；
     后台：visit_enter(background=True)；visit_touch；
  5. 不是本地文件、不是全黑 → record_lines；
  6. add_snap（via='black' 或 res.via）；heartbeat(last_snap_ms=t)；
  7. ShotPolicy.decide：'first' / 'fallback' / 'visit' → 编码 + 存；'visit' 在这一条已经有图、这一拍新字更多时
     替换那张（policy.replaces）。"读不出字的新画面"按这个出处上一拍的小块指纹（shots.tile_signature）比：
     回到看过的页面不算新画面；没有上一拍可比（重启后第一拍）而字全都存过时也不算，这一拍只当参照。
- GreyEvent → grey_enter / grey_leave（暂停 / 锁屏开始时后台条目也离开）；
  AwayEvent → 和当前前台条目重叠的部分累加到"这一段的走开时间"；
- CopyEvent → 这个窗口最近一次拍的出处（标题没变时；否则按 软件 + 标题 记弱标签）→ add_clip；
  那一拍按网址跳过了（内部页、自己的网页、不录网站）时复制也不记；带网址的软件没有对得上的那一拍时也不记。
- 隐私删除（主进程 delete_range，control 线程看到 meta 'delete_rev' 变了调 forget_deleted）：
  丢掉指向已删条目 / 出处 / 灰色时段的内存状态，忘掉已存的弱标签图，下一拍重新建条目。
- 后台条目（一直录的窗口）：出处变了、窗口到了前台、暂停 / 锁屏、收尾时在那一刻离开；cfg.merge_ms 没再拍到时
  在最后一次拍的时间离开。
own_origins：每 5 秒读一次 <home>/endpoint.json（app.auth.read_endpoint）的端口 → http://127.0.0.1:端口、http://localhost:端口。
日志只记次数，从不记屏幕文字、标题和网址。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Callable, Sequence

from .config import V3Config
from .types import (GREY_LOCKED, GREY_PAUSED, KIND_FILE, KIND_URL, SHOT_FALLBACK, SHOT_FIRST, SHOT_VISIT, AwayEvent,
                    CopyEvent, FocusEvent, GreyEvent, SnapEvent)

log = logging.getLogger(__name__)

ORIGINS_EVERY_MS = 5000
MAX_WINDOWS_REMEMBERED = 64
MAX_SIGNATURES = 256       # 记最近这么多个出处上一拍的小块指纹（每个约 16 KB）


def _apis():
    """text 单元的纯函数和类（懒导入；测试可以换掉这个函数）。"""
    from . import shots, source

    return SimpleNamespace(url_skip_reason=source.url_skip_reason, label_source=source.label_source,
                           clean_title=source.clean_title, url_scheme=source.url_scheme,
                           is_private_title=source.is_private_title,
                           is_black=shots.is_black, encode_jpeg=shots.encode_jpeg, shot_boxes=shots.shot_boxes,
                           uncovered_change_ratio=shots.uncovered_change_ratio, tile_signature=shots.tile_signature)


@dataclass
class _Fg:
    """当前前台窗口和它开着的条目。"""
    hwnd: int
    app: str
    title: str
    enter_t: int | None         # 下一个新条目从何时开始：切进来的时间 / 标题变的时间；用过就清掉
    visit_id: int | None = None
    source_id: int | None = None
    seg_start: int = 0          # 当前条目这一段从何时开始（算走开的重叠）
    away_ms: int = 0


@dataclass
class _Bg:
    source_id: int
    visit_id: int
    last_t: int


class Pipeline:
    """处理线程。见模块说明。"""

    def __init__(self, cfg: V3Config, events: "queue.Queue", *, store=None, reader=None, policy=None, clock=None,
                 own_origins: Callable[[], Sequence[str]] | None = None,
                 excluded_hosts: Callable[[], Sequence[str]] | None = None):
        from ..rec.clock import now_ms

        self.cfg = cfg
        self.events = events
        self.clock = clock or now_ms
        self._store = store
        self._reader = reader
        self._own_reader = reader is None
        self._policy = policy
        self._own_fn = own_origins
        self._own_cache: tuple[int, tuple[str, ...]] | None = None
        self._excluded_fn = excluded_hosts
        self._excluded: tuple[str, ...] = ()
        self._api = None
        self._fg: _Fg | None = None
        self._bg: dict[int, _Bg] = {}
        self._greys: dict[tuple[str, str | None], int] = {}
        self._last_by_hwnd: dict[int, tuple[int, str]] = {}   # 窗口 → (最近一次拍的出处, 当时的标题)
        self._url_by_hwnd: dict[int, tuple[str, str]] = {}    # 窗口 → (标题, 网址)：最近一次存下的网址出处（_known_url）
        self._sigs: OrderedDict[int, object] = OrderedDict()  # 出处 → 上一拍的小块指纹
        self._url_apps: set[str] | None = None                # 带网址的软件（进程名小写）
        self._title_tails: dict[str, str] = {}                # 软件 → 窗口标题里加在网页标题后面的尾巴（_page_name）
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._stop_snaps_at: float | None = None  # 收尾时过了这个时刻就不再读字（来不及了）
        self.counts: dict[str, int] = {}
        self.timing = {"read_ms": 0.0, "snaps": 0}

    # ---------- 懒建的部件 ----------

    @property
    def store(self):
        if self._store is None:
            from .store.writer import Store

            self._store = Store(self.cfg)
        return self._store

    @property
    def reader(self):
        if self._reader is None:
            from .read import TextReader

            self._reader = TextReader(self.cfg)
        return self._reader

    @property
    def policy(self):
        if self._policy is None:
            from .shots import ShotPolicy

            self._policy = ShotPolicy(self.cfg)
        return self._policy

    @property
    def api(self):
        if self._api is None:
            self._api = _apis()
        return self._api

    def set_excluded_hosts(self, hosts: Sequence[str]) -> None:
        """不录网站（control 线程在 rev 变了时调）；构造时给了 excluded_hosts 函数时以函数为准。"""
        self._excluded = tuple(str(h) for h in hosts or () if str(h).strip())

    def _hosts(self) -> Sequence[str]:
        return tuple(self._excluded_fn()) if self._excluded_fn is not None else self._excluded

    def _own(self, now: int) -> Sequence[str]:
        if self._own_fn is not None:
            return tuple(self._own_fn())
        if self._own_cache is None or now - self._own_cache[0] >= ORIGINS_EVERY_MS:
            origins: tuple[str, ...] = ()
            try:
                from ..app.auth import read_endpoint

                ep = read_endpoint(self.cfg.home)
                if ep is not None:
                    origins = (f"http://127.0.0.1:{ep.port}", f"http://localhost:{ep.port}")
            except Exception:  # noqa: BLE001
                log.exception("读 endpoint.json 失败")
            self._own_cache = (now, origins)
        return self._own_cache[1]

    def _count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    # ---------- 线程 ----------

    def start(self) -> None:
        if self._thread is None:
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run, name="sr-v3-pipeline", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                self._safe(None)
                continue
            self._safe(ev)

    def _safe(self, ev) -> None:
        try:
            if ev is not None:
                self.process(ev)
            else:
                self._housekeep(self.clock())
        except Exception:  # noqa: BLE001 - 一个事件出错不能让处理线程停掉
            self._count("errors")
            log.exception("处理事件出错（%s）", type(ev).__name__)

    def drain(self) -> int:
        """把队列里现有的事件都处理掉（调用方的线程里）；返回处理了几个。"""
        n = 0
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return n
            self._safe(ev)
            n += 1

    def stop(self, timeout_s: float = 3.0) -> None:
        """处理完剩下的事件（读字只在前一半时间里做，来不及的拍丢掉）；当前条目离开、候选图存下。"""
        t0 = time.monotonic()
        self._stop_snaps_at = t0 + timeout_s / 2
        self._stop_evt.set()
        th, self._thread = self._thread, None
        if th is not None:
            th.join(max(0.1, timeout_s - 0.5))
            if th.is_alive():
                log.warning("处理线程没有及时停下")
                return
        self.drain()
        with self._lock:
            now = self.clock()
            self._leave_fg(now)
            for h in list(self._bg):
                self._leave_bg(h, now)
            for (reason, app), sid in list(self._greys.items()):
                self.store.grey_leave(sid, now)
            self._greys.clear()
        if self._own_reader and self._reader is not None and hasattr(self._reader, "close"):
            try:
                self._reader.close()
            except Exception:  # noqa: BLE001
                log.exception("关读字器失败")

    # ---------- 事件 ----------

    def process(self, ev) -> None:
        with self._lock:
            if isinstance(ev, SnapEvent):
                self._snap(ev)
            elif isinstance(ev, FocusEvent):
                self._focus(ev)
            elif isinstance(ev, GreyEvent):
                self._grey(ev)
            elif isinstance(ev, AwayEvent):
                self._away(ev)
            elif isinstance(ev, CopyEvent):
                self._copy(ev)
            self._housekeep(self.clock())

    def _housekeep(self, now: int) -> None:
        with self._lock:
            for h, b in list(self._bg.items()):
                if now - b.last_t >= self.cfg.merge_ms:
                    self._leave_bg(h)

    def _focus(self, ev: FocusEvent) -> None:
        self._count("focus")
        fg = self._fg
        if fg is not None and ev.hwnd and ev.hwnd == fg.hwnd:
            # 同一窗口只是标题变了：条目先留着，下一拍出处变了才换
            fg.title = ev.title
            if fg.visit_id is not None:
                fg.enter_t = ev.t_ms
            return
        self._leave_fg(ev.t_ms)
        self._fg = _Fg(ev.hwnd, ev.app, ev.title, ev.t_ms) if ev.hwnd else None
        if ev.hwnd in self._bg:  # 后台在录的窗口到了前台：后台那段到现在结束（同一个东西的话前台合进同一条）
            self._leave_bg(ev.hwnd, ev.t_ms)

    def _leave_fg(self, t: int) -> None:
        fg = self._fg
        if fg is not None and fg.visit_id is not None:
            self.store.visit_leave(fg.visit_id, max(t, fg.seg_start), fg.away_ms)
            fg.visit_id = fg.source_id = None
            fg.away_ms = 0

    def _leave_bg(self, hwnd: int, t: int | None = None) -> None:
        """后台那段结束：知道结束时间（到了前台、暂停、换了页面、收尾）就在那时，否则（很久没再拍到）在最后一次拍。"""
        b = self._bg.pop(hwnd, None)
        if b is not None:
            self.store.visit_leave(b.visit_id, max(b.last_t, t) if t is not None else b.last_t, 0)

    def _grey(self, ev: GreyEvent) -> None:
        key = (ev.reason, ev.app)
        if ev.end:
            sid = self._greys.pop(key, None)
            if sid is not None:
                self.store.grey_leave(sid, ev.t_ms)
            return
        if ev.reason in (GREY_PAUSED, GREY_LOCKED):  # 暂停 / 锁屏：后台在录的也停了，那几条到这时结束
            for h in list(self._bg):
                self._leave_bg(h, ev.t_ms)
        if key not in self._greys:
            self._greys[key] = self.store.grey_enter(ev.reason, ev.app, ev.t_ms)
            self._count("grey_" + ev.reason)

    def _away(self, ev: AwayEvent) -> None:
        fg = self._fg
        if fg is None:
            return
        start = fg.seg_start if fg.visit_id is not None else (fg.enter_t or 0)
        overlap = ev.end_ms - max(ev.start_ms, start)
        if overlap > 0:
            fg.away_ms += int(overlap)
            self._count("away")

    def _copy(self, ev: CopyEvent) -> None:
        last = self._last_by_hwnd.get(ev.hwnd)
        if last is not None and last[1] == ev.title and last[0] is None:
            self._count("clip_skipped")  # 这个页面的拍按网址跳过了（内部页、自己的网页、不录网站）：复制也不记
            return
        if last is not None and last[1] == ev.title:
            source_id = last[0]
        elif self._needs_url(ev.app):
            self._count("clip_skipped")  # 带网址的软件：不知道是哪个网址（可能是内部页、自己的网页），不记
            return
        else:
            label = self.api.label_source(ev.app, ev.title, None)
            source_id, _ = self.store.upsert_source(label, ev.t_ms)
        visit_id = None
        fg, b = self._fg, self._bg.get(ev.hwnd)
        if fg is not None and fg.hwnd == ev.hwnd and fg.source_id == source_id:
            visit_id = fg.visit_id
        elif b is not None and b.source_id == source_id:
            visit_id = b.visit_id
        self.store.add_clip(t_ms=ev.t_ms, text=ev.text, app=ev.app, title=ev.title, source_id=source_id,
                            visit_id=visit_id)
        self._count("clips")

    def _snap(self, ev: SnapEvent) -> None:
        if self._stop_snaps_at is not None and time.monotonic() > self._stop_snaps_at:
            self._count("snap_dropped_shutdown")
            return
        fg = self._fg
        if not ev.background and (fg is None or fg.hwnd != ev.hwnd):
            self._count("snap_stale")  # 拍完、还没处理时已经切走了
            return
        api, cfg, store = self.api, self.cfg, self.store
        t = ev.t_ms

        def want_ocr(part) -> bool:  # 这一拍反正不存（跳过、本地文件）就别花一秒 OCR
            url = self._known_url(ev, self._page_url(ev.app, part.url), count=False)
            if self._skip_reason(ev, url, part.window_title, getattr(part, "window_names", ())) is not None:
                return False
            return api.label_source(ev.app, ev.title, url).kind != KIND_FILE

        t0 = time.perf_counter()
        res = self.reader.read_text(ev.hwnd, ev.frame, want_ocr=want_ocr)
        self.timing["read_ms"] += (time.perf_counter() - t0) * 1000
        self.timing["snaps"] += 1
        url = self._known_url(ev, self._page_url(ev.app, res.url))
        skip = self._skip_reason(ev, url, res.window_title, getattr(res, "window_names", ()))
        if skip is not None:
            self._count("skip_" + skip)
            if skip != "stale_page":  # 换了页面不是"这个页面要跳过"：下一拍（新页面画完停下）照常
                self._remember(ev.hwnd, None, ev.title)
                self._url_by_hwnd.pop(ev.hwnd, None)
            return
        label = api.label_source(ev.app, ev.title, url, self._page_name(ev.app, ev.title, url, res.page_title))
        if label.kind == KIND_URL:
            self._url_apps_set().add((ev.app or "").lower())
            self._url_by_hwnd.pop(ev.hwnd, None)
            self._url_by_hwnd[ev.hwnd] = (ev.title, url)
            if len(self._url_by_hwnd) > MAX_WINDOWS_REMEMBERED:
                self._url_by_hwnd.pop(next(iter(self._url_by_hwnd)))
        else:
            self._url_by_hwnd.pop(ev.hwnd, None)
        black = bool(api.is_black(ev.frame.bgr))
        source_id, has_shot = store.upsert_source(label, t)

        # 条目
        if ev.background:
            visit_id = self._bg_visit(ev.hwnd, source_id, t)
        else:
            if fg.source_id != source_id:
                start = fg.enter_t if fg.enter_t is not None else t
                if fg.visit_id is not None:
                    self.store.visit_leave(fg.visit_id, max(start, fg.seg_start), fg.away_ms)
                    fg.away_ms = 0  # 第一次拍之前的走开留着（条目从切进来算）；换出处时从零算
                fg.visit_id = store.visit_enter(source_id, start)
                fg.source_id = source_id
                fg.seg_start = start
            fg.enter_t = None
            visit_id = fg.visit_id
        store.visit_touch(visit_id, t)
        self._remember(ev.hwnd, source_id, ev.title)

        # 字
        file = label.kind == KIND_FILE
        out = None
        if not file and not black:
            out = store.record_lines(source_id, res.lines, t, visit_id=visit_id)
        new = out.new_count if out is not None else 0
        seen = len(out.seen_ids) if out is not None else 0
        ids = dict(out.ids_by_norm) if out is not None else {}
        via = "black" if black else res.via
        snap_id = store.add_snap(t_ms=t, source_id=source_id, visit_id=visit_id, reason=ev.reason, via=via,
                                 new_lines=new, seen_lines=seen)
        store.heartbeat(last_snap_ms=t)
        self._count("snap_" + via)
        self._count("lines_new", new)
        self._count("lines_seen", seen)

        # 截图
        h, w = ev.frame.bgr.shape[:2]
        lines = () if file else res.lines
        sig = api.tile_signature(ev.frame.bgr)
        prev = self._sigs.pop(source_id, None)
        self._sigs[source_id] = sig
        if len(self._sigs) > MAX_SIGNATURES:
            self._sigs.popitem(last=False)
        tiles = (prev != sig) if prev is not None and prev.shape == sig.shape else None  # None：这个出处第一次 = 整幅都新
        if prev is None and new == 0 and seen > 0:
            # 没有上一拍可比（录制进程刚重启、指纹被挤掉），而这一屏的字全都存过：多半是看过的画面，
            # 头像、表情、图标这类"像图"的地方不能当成新出来的照片（否则每次重启都多一张 fallback）。
            # 这一拍只当参照，之后真变了照样按 fallback 算。读不出字的画面（照片）第一次见照旧算新。
            uncovered = 0.0
        else:
            uncovered = api.uncovered_change_ratio(None, (h, w), lines, tiles=tiles, bgr=ev.frame.bgr)
        d = self.policy.decide(label=label, source_id=source_id, visit_id=visit_id, source_has_shot=has_shot,
                               t_ms=t, new_lines=new, has_text=bool(res.lines), uncovered_change=uncovered,
                               black=black)
        if d in (SHOT_FIRST, SHOT_FALLBACK, SHOT_VISIT):
            old = self.policy.replaces(visit_id, t) if d == SHOT_VISIT else None
            shot = self._encode(ev.frame, lines)
            sid = store.save_shot(source_id=source_id, visit_id=visit_id, snap_id=snap_id, t_ms=t, kind=d,
                                  shot=shot, ids_by_norm=ids, replaces=old)
            if sid is None:
                self._count("shot_skipped_disk")
            else:
                self._count("shot_" + d if old is None else "shot_visit_replaced")
                if d == SHOT_FALLBACK:
                    self.policy.mark_fallback(source_id, t)
                elif d == SHOT_VISIT:
                    self.policy.saved_visit(visit_id, sid, new, t)

    def _page_name(self, app: str, title: str, url: str | None, page_title: str | None) -> str | None:
        """网页自己的标题。有 Document 名字时就是它，顺便记下这个软件加在窗口标题后面的尾巴
        （窗口标题 = 网页标题 + 尾巴，如 " - Google Chrome"、" - 个人 - Microsoft Edge"）；
        http(s) 网页没有 Document 名字（浏览器的 PDF 查看器）时用窗口标题去掉学到的尾巴，学不到时 None
        （label_source 退回窗口标题）。尾巴是从这个软件自己的拍里学的数据，不按软件名写规则。"""
        clean = self.api.clean_title
        t, p = clean(title or ""), clean(page_title or "")
        key = (app or "").lower()
        if p:
            tail = t[len(p):]
            if t.startswith(p) and tail[:1].isspace() and tail.strip():
                self._title_tails[key] = tail
            return page_title
        tail = self._title_tails.get(key)
        if tail and self.api.url_scheme(url) in ("http", "https") and t.endswith(tail) and t[:-len(tail)].strip():
            return t[:-len(tail)]
        return page_title

    def _known_url(self, ev: SnapEvent, url: str | None, *, count: bool = True) -> str | None:
        """带网址的软件这一拍没读到网址（查找栏、地址栏这类小框拿着焦点时 UIA 找不到网页的 Document），
        但同一窗口上一拍存下的就是这个标题的网页：沿用那一拍的网址（还会照常过内部页 / 自己的网页 / 不录网站的判断），
        不整拍丢掉。标题变了、上一拍跳过了或不是网址出处时不沿用。"""
        if url or not self._needs_url(ev.app):
            return url
        last = self._url_by_hwnd.get(ev.hwnd)
        if last is None or last[0] != ev.title:
            return url
        if count:
            self._count("url_reused")
        return last[1]

    def _page_url(self, app: str, url: str | None) -> str | None:
        """不带网址的软件（聊天、会议这类用网页技术画界面的程序）的 Document 值是 app:// 或
        file:///…/index.html 这类程序自己的地址，不是在看的网页 / 文件：不当网址（否则会被当成浏览器内部页跳过、
        或当成本地文件不存字），按 软件 + 标题 记。http(s) 网址照收（之后这个软件也算"带网址的软件"）。"""
        if url and not self._needs_url(app) and self.api.url_scheme(url) not in ("http", "https"):
            return None
        return url

    def _skip_reason(self, ev: SnapEvent, url: str | None, window_title: str | None,
                     window_names: Sequence[str] = ()) -> str | None:
        """这一拍不存的原因，没有时 None：
        'private'：辅助功能里的窗口名字（window_names）带无痕关键词（cfg.private_title_keywords）：有的浏览器的
        无痕窗口只在这里写着"（无痕）"，窗口标题里没有，捕获循环按标题认不出来；
        'stale_page'：读字时窗口标题和拍的时候不一样（拍完到读字之间换了页面：图是旧页面、字是新页面，对不上）；
        'internal' / 'own' / 'excluded'：source.url_skip_reason；
        'no_url'：带网址的软件这一拍读不到网址（认不出是不是内部页、自己的网页、不录网站，宁可不存）。"""
        api, cfg = self.api, self.cfg
        private = getattr(api, "is_private_title", None)
        if private is not None and any(private(n, cfg.private_title_keywords) for n in window_names or ()):
            return "private"
        if window_title is not None and api.clean_title(window_title) != api.clean_title(ev.title):
            return "stale_page"
        skip = api.url_skip_reason(url, schemes=cfg.record_url_schemes, own_origins=self._own(ev.t_ms),
                                   excluded_hosts=self._hosts(), internal_prefixes=cfg.internal_url_prefixes)
        if skip is None and not url and self._needs_url(ev.app):
            skip = "no_url"
        return skip

    def _url_apps_set(self) -> set[str]:
        if self._url_apps is None:
            apps = {a.lower() for a in self.cfg.default_record_apps}
            fn = getattr(self.store, "url_apps", None)
            if fn is not None:
                apps |= {a.lower() for a in fn()}
            self._url_apps = apps
        return self._url_apps

    def _needs_url(self, app: str) -> bool:
        """这个软件的页面靠网址认（默认名单里的浏览器、库里出过网址出处的软件）：读不到网址就不存。"""
        return (app or "").lower() in self._url_apps_set()

    def is_skipped(self, hwnd: int, title: str) -> bool:
        """这个窗口的这个页面最近一拍按网址跳过了（control 线程调：心跳不写它的标题）。
        不拿处理线程的锁（读字可能要一秒）：只读一次字典。"""
        last = self._last_by_hwnd.get(hwnd)
        return last is not None and last[0] is None and last[1] == title

    def forget_deleted(self, now: int | None = None) -> None:
        """隐私删除之后：指向已删条目 / 出处 / 灰色时段的内存状态丢掉，候选图全部丢掉（可能拍在删掉的那段里）。
        还开着的条目没被删时照旧；被删了的，下一拍重新建条目。"""
        now = self.clock() if now is None else now
        with self._lock:
            fg = self._fg
            vids = {b.visit_id for b in self._bg.values()} | ({fg.visit_id} if fg and fg.visit_id else set())
            sids = {s for s, _ in self._last_by_hwnd.values() if s is not None} | set(self._sigs)
            spans = set(self._greys.values())
            alive_v, alive_s, alive_g = self.store.alive(visits=vids, sources=sids, spans=spans)
            self.policy.clear()
            if fg is not None and fg.visit_id is not None and fg.visit_id not in alive_v:
                fg.visit_id = fg.source_id = None
                fg.enter_t = None
                fg.away_ms = 0
            for h in [h for h, b in self._bg.items() if b.visit_id not in alive_v]:
                del self._bg[h]
            for h in [h for h, (s, _) in self._last_by_hwnd.items() if s is not None and s not in alive_s]:
                del self._last_by_hwnd[h]
            for s in [s for s in self._sigs if s not in alive_s]:
                del self._sigs[s]
            for (reason, app), sid in list(self._greys.items()):
                if sid not in alive_g:  # 还在进行的灰色时段被删了：从现在起重新记
                    self._greys[(reason, app)] = self.store.grey_enter(reason, app, now)
            self._count("forget_deleted")

    def _remember(self, hwnd: int, source_id: int | None, title: str) -> None:
        """这个窗口最近一次拍的出处（None = 按网址跳过了），给复制用。"""
        self._last_by_hwnd.pop(hwnd, None)
        self._last_by_hwnd[hwnd] = (source_id, title)
        if len(self._last_by_hwnd) > MAX_WINDOWS_REMEMBERED:
            self._last_by_hwnd.pop(next(iter(self._last_by_hwnd)))

    def _encode(self, frame, lines):
        api = self.api
        shot = api.encode_jpeg(frame.bgr, quality=self.cfg.jpeg_quality, max_width=self.cfg.shot_max_width)
        shot.boxes = list(api.shot_boxes(lines, shot.scale)) if lines else []
        return shot

    def _bg_visit(self, hwnd: int, source_id: int, t: int) -> int:
        b = self._bg.get(hwnd)
        if b is not None and b.source_id == source_id:
            b.last_t = max(b.last_t, t)
            return b.visit_id
        if b is not None:
            self._leave_bg(hwnd, t)
        vid = self.store.visit_enter(source_id, t, background=True)
        self._bg[hwnd] = _Bg(source_id, vid, t)
        return vid
