"""v3 capture 单元：Pipeline（假的 Store / 读字器 / 留图策略 / text 单元函数）、ControlLoop、录制进程的命令行和组装。"""

from __future__ import annotations

import queue
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from screenrecall.rec.frames import Frame
from screenrecall.rec.inputmon import FakeInput
from screenrecall.rec.windows import FakeWindows
from screenrecall.v3 import loop as loop_mod
from screenrecall.v3 import pipeline as pipeline_mod
from screenrecall.v3 import recorder as rec_mod
from screenrecall.v3.config import build
from screenrecall.v3.control import ControlLoop
from screenrecall.v3.pipeline import Pipeline
from screenrecall.v3.types import (KIND_APP, KIND_FILE, KIND_URL, PAUSE_FOREVER_MS, AwayEvent, CopyEvent,
                                   EncodedShot, FocusEvent, GreyEvent, LinesOutcome, SnapEvent, SourceLabel,
                                   TextLine, TextResult)


# ---------------- 假的 text 单元函数 ----------------

def _label(app, title, url, page_title=None):
    title = page_title or title
    if url and url.startswith("http"):
        return SourceLabel(KIND_URL, url, app, title, url=url)
    if url and url.startswith("file:///"):
        return SourceLabel(KIND_FILE, url[8:].lower(), app, title, url=url, path=url[8:])
    return SourceLabel(KIND_APP, f"{app.lower()}\t{title}", app, title)


def _skip(url, *, schemes, own_origins, excluded_hosts, internal_prefixes=()):
    if not url:
        return None
    if url.split(":", 1)[0] not in schemes or any(url.startswith(p) for p in internal_prefixes):
        return "internal"
    if any(url.startswith(o) for o in own_origins):
        return "own"
    host = url.split("/")[2] if "//" in url else ""
    if any(host == h or host.endswith("." + h) for h in excluded_hosts):
        return "excluded"
    return None


FAKE_API = SimpleNamespace(
    url_skip_reason=_skip, label_source=_label, is_black=lambda bgr: int(np.asarray(bgr).max()) < 10,
    clean_title=lambda s: (s or "").strip(), url_scheme=lambda u: (u or "").split(":", 1)[0].lower(),
    encode_jpeg=lambda bgr, quality, max_width: EncodedShot(b"jpg", bgr.shape[1], bgr.shape[0], 1.0),
    shot_boxes=lambda lines, scale: [(ln.text, ln.box) for ln in lines],
    uncovered_change_ratio=lambda changed, shape, lines, tiles=None, bgr=None: 1.0 if not lines else 0.0,
    tile_signature=lambda bgr: np.zeros((2, 2), dtype=np.uint64))


class FakeStore:
    """记下所有调用；按 key 分配 source_id，visit 编号递增。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.sources: dict[str, int] = {}
        self.has_shot: set[int] = set()
        self.norms: dict[str, int] = {}
        self.ctl = {"paused_until_ms": None, "rev": 1}
        self.tiers = {"msedge.exe": "record"}
        self.ex: list[dict] = []
        self.meta: dict[str, str] = {}
        self.learned_url_apps: set[str] = set()
        self.deleted: set[int] = set()          # 隐私删除删掉的条目 / 灰色时段编号
        self.deleted_sources: set[int] = set()
        self._n = 0

    def _id(self) -> int:
        self._n += 1
        return self._n

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def of(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    # 录制进程启动时
    def recover(self, now_ms):
        self.calls.append(("recover", now_ms))
        return {"crashed": False}

    def init_defaults(self, apps):
        self.calls.append(("init_defaults", tuple(apps)))
        return True

    def close(self):
        self.calls.append(("close",))

    # 出处 / 时间线
    def upsert_source(self, label, t_ms):
        sid = self.sources.setdefault(label.key, len(self.sources) + 1)
        self.calls.append(("upsert_source", label.kind, sid, t_ms))
        return sid, sid in self.has_shot

    def visit_enter(self, source_id, t_ms, *, background=False):
        vid = self._id()
        self.calls.append(("visit_enter", source_id, t_ms, background, vid))
        return vid

    def visit_touch(self, visit_id, t_ms):
        self.calls.append(("visit_touch", visit_id, t_ms))

    def visit_leave(self, visit_id, t_ms, away_ms=0):
        self.calls.append(("visit_leave", visit_id, t_ms, away_ms))

    def grey_enter(self, reason, app, t_ms):
        sid = self._id()
        self.calls.append(("grey_enter", reason, app, t_ms, sid))
        return sid

    def grey_leave(self, span_id, t_ms):
        self.calls.append(("grey_leave", span_id, t_ms))

    def record_lines(self, source_id, lines, t_ms, *, visit_id=None):
        new, seen, ids = [], [], {}
        for ln in lines:
            if ln.text in self.norms:
                seen.append(self.norms[ln.text])
            else:
                self.norms[ln.text] = len(self.norms) + 100
                new.append(self.norms[ln.text])
            ids[ln.text] = self.norms[ln.text]
        self.calls.append(("record_lines", source_id, len(lines), visit_id))
        return LinesOutcome(tuple(new), tuple(seen), ids)

    def add_snap(self, *, t_ms, source_id, visit_id, reason, via, new_lines, seen_lines):
        snap_id = self._id()
        self.calls.append(("add_snap", source_id, visit_id, reason, via, new_lines, seen_lines, snap_id))
        return snap_id

    def save_shot(self, *, source_id, visit_id, snap_id, t_ms, kind, shot, ids_by_norm, replaces=None):
        shot_id = self._id()
        if kind == "first":
            self.has_shot.add(source_id)
        self.calls.append(("save_shot", source_id, visit_id, snap_id, kind, dict(ids_by_norm), len(shot.boxes),
                           replaces, shot_id))
        return shot_id

    def add_clip(self, *, t_ms, text, app, title, source_id, visit_id):
        self.calls.append(("add_clip", text, source_id, visit_id))
        return self._id()

    # 控制
    def control(self):
        return dict(self.ctl)

    def heartbeat(self, **fields):
        self.calls.append(("heartbeat", fields))

    def app_tiers(self):
        self.calls.append(("app_tiers",))
        return dict(self.tiers)

    def excludes(self):
        return list(self.ex)

    def meta_get(self, key, default=None):
        return self.meta.get(key, default)

    def meta_put(self, key, value):
        self.meta[key] = value

    def url_apps(self):
        return set(self.learned_url_apps)

    def alive(self, *, visits=(), sources=(), spans=()):
        self.calls.append(("alive", set(visits), set(sources), set(spans)))
        return ({v for v in visits if v not in self.deleted}, {s for s in sources if s not in self.deleted_sources},
                {g for g in spans if g not in self.deleted})


class FakePolicy:
    """和约定里 ShotPolicy 的规则一样的最小实现（不依赖 text 单元）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.slots: dict[int, tuple[int, int, int]] = {}   # visit_id -> (shot_id, new_lines, since)
        self.fallback_at: dict[int, int] = {}

    def _live(self, visit_id, t_ms):
        s = self.slots.get(visit_id)
        return s if s is not None and t_ms - s[2] < self.cfg.weak_visit_ms else None

    def decide(self, *, label, source_id, visit_id, source_has_shot, t_ms, new_lines, has_text,
               uncovered_change, black):
        if black:
            return "none"
        if label.strong and not source_has_shot:
            return "first"
        if new_lines == 0 and uncovered_change >= self.cfg.fallback_min_area:
            last = self.fallback_at.get(source_id)
            if last is None or t_ms - last >= self.cfg.fallback_shot_ms:
                return "fallback"
        if not label.strong and new_lines > 0:
            s = self._live(visit_id, t_ms)
            if s is None or new_lines > s[1]:
                return "visit"
        return "none"

    def replaces(self, visit_id, t_ms):
        s = self._live(visit_id, t_ms)
        return s[0] if s else None

    def saved_visit(self, visit_id, shot_id, new_lines, t_ms):
        s = self._live(visit_id, t_ms)
        self.slots[visit_id] = (shot_id, new_lines, s[2] if s else t_ms)

    def mark_fallback(self, source_id, t_ms):
        self.fallback_at[source_id] = t_ms

    def clear(self):
        self.slots.clear()


class FakeReader:
    """result 原样交出；want_ocr 记下最后一次问的结果（ocr_asked），模拟"字不够、要 OCR 之前问一下"。"""

    def __init__(self):
        self.result = TextResult((), "none")
        self.reads = 0
        self.ocr_asked: bool | None = None
        self.use_uia: list[bool] = []

    def read_text(self, hwnd, frame, *, want_ocr=None, use_uia=True):
        self.reads += 1
        self.use_uia.append(use_uia)
        if want_ocr is not None and len(self.result.lines) < 3:
            self.ocr_asked = bool(want_ocr(self.result))
        return self.result


def lines(*texts: str) -> tuple[TextLine, ...]:
    return tuple(TextLine(t, (10, 20 + i * 30, 300, 24), "uia") for i, t in enumerate(texts))


def frame(black: bool = False) -> Frame:
    arr = np.zeros((120, 200, 3), dtype=np.uint8) if black else np.full((120, 200, 3), 240, dtype=np.uint8)
    return Frame.from_bgr(1, 0, arr)


def snap(t: int, hwnd: int = 1, *, app: str = "msedge.exe", title: str = "文章", reason: str = "still",
         bg: bool = False, black: bool = False) -> SnapEvent:
    return SnapEvent(hwnd, 1000 + hwnd, app, title, t, reason, frame(black), bg, None)


@pytest.fixture
def cfg(tmp_path):
    return build({}, data_dir=tmp_path, home=tmp_path)


@pytest.fixture
def P(cfg, monkeypatch):
    monkeypatch.setattr(pipeline_mod, "_apis", lambda: FAKE_API)
    store, reader, now = FakeStore(), FakeReader(), [70_000]
    p = Pipeline(cfg, queue.Queue(), store=store, reader=reader, policy=FakePolicy(cfg), clock=lambda: now[0],
                 own_origins=lambda: ["http://127.0.0.1:8611"])
    p.store_, p.reader_, p.now_ = store, reader, now
    return p


# ---------------- Pipeline ----------------

@pytest.mark.parametrize("url", ["edge://settings/", "about:blank", "http://127.0.0.1:8611/search?q=x",
                                 "https://mail.secret.example/inbox"])
def test_url_skip_writes_nothing(P, url):
    P.set_excluded_hosts(["secret.example"])
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("一行字"), "uia", url=url)
    P.process(snap(3000))
    assert P.store_.calls == []
    P.process(CopyEvent(3100, 1, "msedge.exe", "文章", "复制"))  # 这个页面跳过了：复制也不记
    assert P.store_.calls == []


def test_url_snap_records_and_first_shot(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("第一行", "第二行"), "uia", url="https://blog.example.com/a")
    P.process(snap(3500))
    s = P.store_
    assert s.names() == ["upsert_source", "visit_enter", "visit_touch", "record_lines", "add_snap", "heartbeat",
                         "save_shot"]
    assert s.of("visit_enter")[0][2] == 1000  # 条目从切进来的时间开始
    assert s.of("heartbeat")[0][1] == {"last_snap_ms": 3500}
    save = s.of("save_shot")[0]
    assert save[4] == "first" and save[5] == {"第一行": 100, "第二行": 101} and save[6] == 2
    assert s.of("add_snap")[0][3:7] == ("still", "uia", 2, 0)
    s.calls.clear()
    P.process(snap(6000))  # 同一个网址再拍：字都见过，没有新图
    assert "save_shot" not in s.names() and "visit_enter" not in s.names()
    assert s.of("add_snap")[0][5:7] == (0, 2)


def test_file_label_stores_no_text(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "x.pdf"))
    P.reader_.result = TextResult(lines("PDF 里的字"), "uia", url="file:///C:/docs/x.pdf")
    P.process(snap(3000, title="x.pdf"))
    s = P.store_
    assert "record_lines" not in s.names()
    save = s.of("save_shot")[0]
    assert save[4] == "first" and save[5] == {} and save[6] == 0  # 只留 1 张图，不登记字框


def test_black_frame_title_only(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "视频"))
    P.reader_.result = TextResult((), "none", url="https://video.example.com/v/1")
    P.process(snap(3000, title="视频", black=True))
    s = P.store_
    assert "record_lines" not in s.names() and "save_shot" not in s.names()
    assert s.of("add_snap")[0][4] == "black"
    assert "visit_enter" in s.names()


def test_url_change_in_same_window_leaves_and_enters(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章 A"))
    P.reader_.result = TextResult(lines("A 的字"), "uia", url="https://a.example.com/")
    P.process(snap(3000, title="文章 A"))
    P.process(FocusEvent(5000, 1, 1001, "msedge.exe", "文章 B"))  # 同一窗口只是标题变了：条目先留着
    assert "visit_leave" not in P.store_.names()
    P.reader_.result = TextResult(lines("B 的字"), "uia", url="https://b.example.com/")
    P.process(snap(7000, title="文章 B"))
    s = P.store_
    enters = s.of("visit_enter")
    assert s.of("visit_leave") == [("visit_leave", enters[0][4], 5000, 0)]
    assert enters[1][1:3] == (2, 5000)


def test_same_url_title_change_keeps_visit(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "(1) 收件箱"))
    P.reader_.result = TextResult(lines("一封信"), "uia", url="https://mail.example.com/")
    P.process(snap(3000, title="(1) 收件箱"))
    P.process(FocusEvent(4000, 1, 1001, "msedge.exe", "(2) 收件箱"))
    P.process(snap(6000, title="(2) 收件箱"))
    assert len(P.store_.of("visit_enter")) == 1 and P.store_.of("visit_leave") == []


def test_weak_shot_saved_now_then_replaced_by_more_new_lines(P):
    """弱标签：第一次有新字当场存（搜索结果马上有图）；同一条里新字更多的那拍替换它，新字少的不动；切走不再补存。"""
    P.process(FocusEvent(1000, 1, 1001, "WeChat.exe", "微信"))
    P.reader_.result = TextResult(lines("小王：明天组会"), "ocr")
    P.process(snap(3000, app="WeChat.exe", title="微信"))
    s = P.store_
    first = s.of("save_shot")[0]
    assert first[4] == "visit" and first[7] is None
    P.reader_.result = TextResult(lines("小王：明天组会", "小李：好的", "小张：收到"), "ocr")
    P.process(snap(6000, app="WeChat.exe", title="微信"))
    second_snap = s.of("add_snap")[1][7]
    save = s.of("save_shot")[1]
    assert save[3:5] == (second_snap, "visit") and save[7] == first[8]  # 换掉第一张
    assert set(save[5]) == {"小王：明天组会", "小李：好的", "小张：收到"}
    P.reader_.result = TextResult(lines("小王：明天组会", "小赵：我也去"), "ocr")
    P.process(snap(8000, app="WeChat.exe", title="微信"))  # 只有 1 行新字：不换
    P.process(FocusEvent(9000, 2, 1002, "msedge.exe", "文章"))
    vid = s.of("visit_enter")[0][4]
    assert s.of("visit_leave") == [("visit_leave", vid, 9000, 0)]
    assert len(s.of("save_shot")) == 2 and P.counts["shot_visit_replaced"] == 1


def test_weak_no_new_lines_no_shot(P):
    P.process(FocusEvent(1000, 1, 1001, "WeChat.exe", "微信"))
    P.reader_.result = TextResult(lines("见过的"), "uia")
    P.store_.norms["见过的"] = 7
    P.process(snap(3000, app="WeChat.exe", title="微信"))
    P.process(FocusEvent(4000))
    assert "save_shot" not in P.store_.names()


def test_fallback_shot_for_unreadable_new_picture(P):
    P.process(FocusEvent(1000, 1, 1001, "Photos.exe", "照片"))
    P.reader_.result = TextResult((), "none")
    P.process(snap(3000, app="Photos.exe", title="照片"))
    save = P.store_.of("save_shot")
    assert len(save) == 1 and save[0][4] == "fallback"
    P.process(snap(30_000, app="Photos.exe", title="照片"))  # 一分钟内同一个东西最多一张
    assert len(P.store_.of("save_shot")) == 1
    P.process(snap(64_000, app="Photos.exe", title="照片"))
    assert len(P.store_.of("save_shot")) == 2


def test_no_fallback_on_first_sight_when_all_text_seen(P, monkeypatch):
    """重启后第一拍（没有上一拍的指纹可比）、字全都存过：头像这类像图的地方不当新照片；之后真变了照样 fallback。"""
    seen_area = []

    def ratio(changed, shape, lines, tiles=None, bgr=None):
        seen_area.append(tiles)
        return 0.0 if tiles is not None and not np.asarray(tiles).any() else 0.5

    sigs = iter([np.zeros((2, 2), dtype=np.uint64), np.zeros((2, 2), dtype=np.uint64),
                 np.ones((2, 2), dtype=np.uint64)])
    monkeypatch.setattr(P, "_api", SimpleNamespace(**{**vars(FAKE_API), "uncovered_change_ratio": ratio,
                                                     "tile_signature": lambda bgr: next(sigs)}))
    P.store_.norms["见过的"] = 7
    P.reader_.result = TextResult(lines("见过的"), "ocr")
    P.process(FocusEvent(1000, 1, 1001, "WeChat.exe", "微信"))
    P.process(snap(3000, app="WeChat.exe", title="微信"))
    assert P.store_.of("save_shot") == [] and seen_area == []   # 只当参照，不算面积
    P.process(snap(5000, app="WeChat.exe", title="微信"))       # 同一屏：没变
    assert P.store_.of("save_shot") == []
    P.process(snap(7000, app="WeChat.exe", title="微信"))       # 字还是见过的，但出了新图
    save = P.store_.of("save_shot")
    assert len(save) == 1 and save[0][4] == "fallback"


def test_copy_attributed_to_last_source(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("一行字"), "uia", url="https://blog.example.com/a")
    P.process(snap(3000, reason="copy"))
    P.process(CopyEvent(3000, 1, "msedge.exe", "文章", "一行字"))
    s = P.store_
    vid = s.of("visit_enter")[0][4]
    assert s.of("add_clip") == [("add_clip", "一行字", 1, vid)]


def test_copy_without_snap_uses_weak_label(P):
    P.process(FocusEvent(1000, 1, 1001, "notepad.exe", "新文档"))
    P.process(CopyEvent(1500, 1, "notepad.exe", "新文档", "复制的"))
    s = P.store_
    assert s.of("upsert_source")[0][1] == KIND_APP
    assert s.of("add_clip") == [("add_clip", "复制的", 1, None)]


def test_url_app_without_url_stores_nothing(P):
    """浏览器（默认名单里的）这一拍读不到网址（UIA 超时、内部页没给值）：认不出是不是内部页 / 自己的网页，什么都不存；
    没有对得上的那一拍时复制也不记。"""
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "edge://version"))
    P.reader_.result = TextResult(lines("内部页的字"), "ocr", url=None, status="ok")
    P.process(snap(3000, title="edge://version"))
    assert P.store_.calls == [] and P.counts["skip_no_url"] == 1
    assert P.is_skipped(1, "edge://version")
    P.process(CopyEvent(3100, 1, "msedge.exe", "edge://version", "复制"))
    P.process(CopyEvent(3200, 1, "msedge.exe", "换了标题", "复制"))
    assert P.store_.calls == []


def test_url_missing_once_mid_visit_reuses_last_url(P):
    """同一窗口、同一标题的网页，中间一拍没读到网址（查找栏拿着焦点）：沿用上一拍的网址照常存，不整拍丢掉；
    标题变了就不沿用（还是 skip_no_url）。"""
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("第一行"), "uia", url="https://blog.example.com/a")
    P.process(snap(3000))
    P.reader_.result = TextResult(lines("第一行", "粘贴的新一行"), "uia", url=None)
    P.process(snap(6000))
    s = P.store_
    assert P.counts.get("skip_no_url", 0) == 0 and P.counts["url_reused"] == 1
    assert len(s.of("add_snap")) == 2 and len(s.of("visit_enter")) == 1  # 同一出处、同一条目
    assert len({c[1:3] for c in s.of("upsert_source")}) == 1
    P.process(FocusEvent(7000, 1, 1001, "msedge.exe", "别的页"))
    P.process(snap(9000, title="别的页"))
    assert P.counts["skip_no_url"] == 1 and len(s.of("add_snap")) == 2


def test_reused_url_still_checked_against_excluded_hosts(P):
    """沿用的网址照样过不录网站的判断。"""
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("第一行"), "uia", url="https://blog.example.com/a")
    P.process(snap(3000))
    P.set_excluded_hosts(["blog.example.com"])
    P.reader_.result = TextResult(lines("第二行"), "uia", url=None)
    P.process(snap(6000))
    assert P.counts["skip_excluded"] == 1 and len(P.store_.of("add_snap")) == 1


def test_app_that_gave_urls_needs_url_from_then_on(P):
    """不在默认名单里的软件出过网址出处以后，也按"读不到网址就不存"处理；库里出过网址的软件启动时就认。"""
    P.process(FocusEvent(1000, 1, 1001, "newbrowser.exe", "页面"))
    P.reader_.result = TextResult(lines("一行字"), "uia", url="https://a.example.com/")
    P.process(snap(3000, app="newbrowser.exe", title="页面"))
    P.process(FocusEvent(5000, 1, 1001, "newbrowser.exe", "另一页"))
    P.store_.calls.clear()
    P.reader_.result = TextResult(lines("另一行"), "ocr", url=None)
    P.process(snap(6000, app="newbrowser.exe", title="另一页"))
    assert P.store_.calls == []
    P.store_.learned_url_apps = {"OtherBrowser.exe"}
    P._url_apps = None
    assert P._needs_url("otherbrowser.exe") and not P._needs_url("notepad.exe")


def test_app_whose_system_text_times_out_is_read_by_ocr_only(P):
    """读系统文字超时的软件（读一次就卡住它）：以后只截图认字，重启也记得；浏览器不记（网址要靠它）。"""
    from screenrecall.v3.read import ReadResult

    P.process(FocusEvent(1000, 1, 1001, "Code.exe", "a.py - Visual Studio Code"))
    P.reader_.result = ReadResult(lines("一行字"), "ocr", uia_timed_out=True)
    P.process(snap(3000, app="Code.exe", title="a.py - Visual Studio Code"))
    P.process(snap(6000, app="Code.exe", title="a.py - Visual Studio Code"))
    assert P.reader_.use_uia == [True, False]
    assert P.store_.meta[pipeline_mod.SLOW_UIA_KEY] == '["code.exe"]'
    P._slow_uia = None
    assert P._slow_uia_set() == {"code.exe"}
    P.process(FocusEvent(7000, 2, 1002, "msedge.exe", "文章"))
    P.reader_.result = ReadResult(lines("正文"), "ocr", url="https://a.example.com/", uia_timed_out=True)
    P.process(snap(9000, hwnd=2))
    P.process(snap(12000, hwnd=2))
    assert P.reader_.use_uia[-2:] == [True, True]


def test_forget_deleted_reenters_visit_and_drops_candidates(P):
    P.process(FocusEvent(1000, 1, 1001, "WeChat.exe", "微信"))
    P.process(GreyEvent(1000, "unlisted", "Code.exe"))
    P.reader_.result = TextResult(lines("小王：明天组会"), "ocr")
    P.process(snap(3000, app="WeChat.exe", title="微信"))
    s = P.store_
    vid = s.of("visit_enter")[0][4]
    span = s.of("grey_enter")[0][4]
    s.deleted |= {vid, span}
    P.forget_deleted(4000)
    assert P._fg.visit_id is None and P._policy.slots == {}
    assert s.of("grey_enter")[-1][1:4] == ("unlisted", "Code.exe", 4000)  # 还在进行的灰字从现在起重记
    P.reader_.result = TextResult(lines("小李：好的"), "ocr")
    P.process(snap(6000, app="WeChat.exe", title="微信"))
    assert len(s.of("visit_enter")) == 2 and s.of("visit_enter")[1][2] == 6000
    assert not any(c[1] == vid for c in s.of("visit_leave"))  # 删掉的那条不再写
    P.process(FocusEvent(7000))
    # 删之前那张属于删掉的条目；删之后新条目另存一张，不去替换删掉的那张
    assert [c[2] for c in s.of("save_shot")] == [vid, s.of("visit_enter")[1][4]]
    assert s.of("save_shot")[1][7] is None


def test_away_subtracted_on_leave(P, cfg):
    P.process(FocusEvent(0, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("一行字"), "uia", url="https://blog.example.com/a")
    P.process(snap(2000))
    P.process(AwayEvent(10_000, 10_000 + cfg.away_ms + 60_000))
    P.process(FocusEvent(500_000))
    vid = P.store_.of("visit_enter")[0][4]
    assert P.store_.of("visit_leave") == [("visit_leave", vid, 500_000, cfg.away_ms + 60_000)]


def test_grey_events(P):
    P.process(GreyEvent(1000, "unlisted", "Code.exe"))
    P.process(GreyEvent(1000, "unlisted", "Code.exe"))  # 重复的开始不再建
    P.process(GreyEvent(5000, "unlisted", "Code.exe", end=True))
    s = P.store_
    sid = s.of("grey_enter")[0][4]
    assert s.of("grey_enter") == [("grey_enter", "unlisted", "Code.exe", 1000, sid)]
    assert s.of("grey_leave") == [("grey_leave", sid, 5000)]


def test_background_snap_and_expiry(P, cfg):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult(lines("PPT 第一页"), "ocr")
    P.process(snap(3000, hwnd=9, app="zoom.exe", title="会议", bg=True))
    s = P.store_
    enter = s.of("visit_enter")[0]
    assert enter[2:4] == (3000, True)
    P.process(snap(5000, hwnd=9, app="zoom.exe", title="会议", bg=True))
    assert len(s.of("visit_enter")) == 1
    P._housekeep(5000 + cfg.merge_ms)  # 很久没再拍到：在最后一次拍的时间离开
    assert s.of("visit_leave") == [("visit_leave", enter[4], 5000, 0)]
    assert [c[4] for c in s.of("save_shot")] == ["visit"]


def test_background_visits_leave_when_paused_or_brought_to_front(P):
    """暂停开始：后台条目在那一刻离开（不再一直开着、把暂停的时间算进去）；窗口到了前台：后台那段到切过来时结束。"""
    P.reader_.result = TextResult(lines("PPT 第一页"), "ocr")
    P.process(snap(3000, hwnd=9, app="zoom.exe", title="会议", bg=True))
    P.process(snap(3000, hwnd=8, app="zoom.exe", title="会议 2", bg=True))
    s = P.store_
    v9, v8 = s.of("visit_enter")[0][4], s.of("visit_enter")[1][4]
    P.process(GreyEvent(20_000, "paused"))
    assert sorted(s.of("visit_leave")) == sorted([("visit_leave", v9, 20_000, 0), ("visit_leave", v8, 20_000, 0)])
    assert P._bg == {}
    P.process(GreyEvent(30_000, "paused", end=True))
    P.process(snap(31_000, hwnd=9, app="zoom.exe", title="会议", bg=True))
    v9b = s.of("visit_enter")[-1][4]
    P.process(FocusEvent(40_000, 9, 1009, "zoom.exe", "会议"))
    assert s.of("visit_leave")[-1] == ("visit_leave", v9b, 40_000, 0)


def test_stale_page_snap_dropped_without_marking_page_skipped(P):
    """拍完到读字之间换了页面（读字时窗口标题不一样）：图是旧页面、字是新页面，这一拍不存，也不 OCR；
    页面本身不算"要跳过"（复制、心跳照常）。"""
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.reader_.result = TextResult((), "none", url="https://b.example.com/", window_title="另一篇")
    P.process(snap(3000))
    assert P.store_.calls == [] and P.counts["skip_stale_page"] == 1
    assert P.reader_.ocr_asked is False and not P.is_skipped(1, "文章")
    P.reader_.result = TextResult(lines("一行字"), "uia", url="https://b.example.com/", window_title="文章")
    P.process(snap(6000))
    assert "record_lines" in P.store_.names()


def test_no_ocr_for_snaps_that_will_not_be_stored(P):
    """字不够、本该 OCR：这一拍要按网址跳过（内部页、读不到网址）或是本地文件（不存字）时不 OCR；普通网页照常。"""
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    for url, want in [("edge://settings", False), (None, False), ("file:///C:/x.pdf", False),
                      ("https://a.example.com/", True), ("https://ntp.msn.com/edge/ntp?x=1", False)]:
        P.reader_.result = TextResult((), "none", url=url)
        P.process(snap(3000))
        assert P.reader_.ocr_asked is want, url


def test_new_tab_page_is_internal(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "新建标签页"))
    P.reader_.result = TextResult(lines("新闻"), "uia", url="https://ntp.msn.com/edge/ntp?locale=zh-CN")
    P.process(snap(3000, title="新建标签页"))
    assert P.store_.calls == [] and P.counts["skip_internal"] == 1


@pytest.mark.parametrize("url", ["app://./index.html", "file:///C:/Program%20Files/Chat/resources/index.html"])
def test_app_internal_document_url_is_not_a_page(P, url):
    """不带网址的软件（聊天软件用网页技术画界面）的 Document 值是程序自己的地址：不当网址（不跳过、不当本地文件），
    按 软件 + 标题 记、存字。"""
    P.process(FocusEvent(1000, 1, 1001, "QQ.exe", "QQ"))
    P.reader_.result = TextResult(lines("一条消息"), "uia", url=url)
    P.process(snap(3000, app="QQ.exe", title="QQ"))
    s = P.store_
    assert s.of("upsert_source")[0][1] == KIND_APP and "record_lines" in s.names()
    assert not P._needs_url("QQ.exe")


def test_url_label_uses_page_title(P):
    """有网址时出处的标题用网页自己的标题（Document 的名字），不带浏览器加在窗口标题后面的尾巴。"""
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章 - 个人 - Microsoft Edge"))
    P.reader_.result = TextResult(lines("一行字"), "uia", url="https://a.example.com/", page_title="文章")
    labels = []
    P.api.label_source = lambda *a: labels.append(a) or _label(*a)
    try:
        P.process(snap(3000, title="文章 - 个人 - Microsoft Edge"))
    finally:
        P.api.label_source = _label
    assert labels[-1][3] == "文章"


def test_stale_snap_after_switch_is_dropped(P):
    P.process(FocusEvent(1000, 1, 1001, "msedge.exe", "文章"))
    P.process(FocusEvent(1500, 2, 1002, "msedge.exe", "另一个"))
    P.process(snap(1400, hwnd=1))
    assert P.store_.calls == [] and P.reader_.reads == 0


def test_stop_leaves_current_and_flushes(P):
    P.process(FocusEvent(1000, 1, 1001, "WeChat.exe", "微信"))
    P.process(GreyEvent(1000, "paused"))
    P.events.put(SnapEvent(1, 1001, "WeChat.exe", "微信", 3000, "still", frame(), False, None))
    P.reader_.result = TextResult(lines("新消息"), "ocr")
    P.now_[0] = 10_000_000
    P.stop(3.0)
    s = P.store_
    assert "add_snap" in s.names()
    assert s.of("visit_leave")[0][2] == 10_000_000
    assert [c[4] for c in s.of("save_shot")] == ["visit"]
    assert s.of("grey_leave")


# ---------------- ControlLoop ----------------

class FakeLoop:
    def __init__(self):
        self.paused = None
        self.tiers = None
        self.kws = None
        self.state = "recording"
        self.current = ("msedge.exe", "文章")

    def set_paused(self, p):
        self.paused = p

    def set_tiers(self, t):
        self.tiers = t

    def set_excludes(self, kws):
        self.kws = list(kws)


class FakePipe:
    hosts = None
    skipped = False

    def __init__(self):
        self.forgot = 0

    def set_excluded_hosts(self, h):
        self.hosts = list(h)

    def is_skipped(self, hwnd, title):
        return self.skipped

    def forget_deleted(self, now=None):
        self.forgot += 1


def test_control_pause_rev_and_heartbeat(cfg):
    st, lp, pp = FakeStore(), FakeLoop(), FakePipe()
    st.ex = [{"exclude_id": 1, "kind": "title", "value": "网银"}, {"exclude_id": 2, "kind": "host", "value": "bank.example"}]
    c = ControlLoop(cfg, st, lp, pp, pid=4321, clock=lambda: 0)
    c.tick(1000)
    assert lp.paused is False and lp.tiers == {"msedge.exe": "record"}
    assert lp.kws == ["网银"] and pp.hosts == ["bank.example"]
    hb = st.of("heartbeat")
    assert hb[-1][1] == {"recorder_pid": 4321, "recorder_state": "recording", "current_app": "msedge.exe",
                         "current_title": "文章"}
    c.tick(2000)
    assert len(st.of("app_tiers")) == 1 and len(st.of("heartbeat")) == 1  # rev 没变不重读；心跳 2 秒一次
    c.tick(3000)
    assert len(st.of("heartbeat")) == 2

    st.ctl["paused_until_ms"] = 60_000
    c.tick(3500)
    assert lp.paused is True
    assert st.of("heartbeat")[-1][1]["recorder_state"] == "paused"
    assert st.of("heartbeat")[-1][1]["current_app"] is None
    c.tick(60_000)
    assert lp.paused is False  # 到时间自动继续
    st.ctl["paused_until_ms"] = PAUSE_FOREVER_MS
    c.tick(10**13)
    assert lp.paused is True

    st.ctl["rev"] = 2
    st.tiers = {"msedge.exe": "record", "code.exe": "record"}
    c.tick(10**13 + 1000)
    assert lp.tiers == st.tiers


def test_control_delete_rev_and_skipped_title(cfg):
    st, lp, pp = FakeStore(), FakeLoop(), FakePipe()
    lp.current_hwnd = 5
    st.meta["delete_rev"] = "3"  # 启动前删过：和这个进程无关
    c = ControlLoop(cfg, st, lp, pp, pid=1, clock=lambda: 0)
    c.tick(1000)
    assert pp.forgot == 0
    st.meta["delete_rev"] = "4"
    c.tick(2000)
    assert pp.forgot == 1
    c.tick(3000)
    assert pp.forgot == 1
    pp.skipped = True  # 这个页面按网址跳过了：标题不写进心跳
    c.tick(6000)
    assert st.of("heartbeat")[-1][1]["current_app"] is None and st.of("heartbeat")[-1][1]["current_title"] is None


def test_control_locked_state(cfg):
    st, lp = FakeStore(), FakeLoop()
    lp.state = "locked"
    ControlLoop(cfg, st, lp, FakePipe(), pid=1).tick(1000)
    assert st.of("heartbeat")[-1][1]["recorder_state"] == "locked"
    assert st.of("heartbeat")[-1][1]["current_title"] is None


# ---------------- 录制进程 ----------------

def test_only_ids_and_cli(monkeypatch):
    assert rec_mod.only_ids("0x1A,26", "", None, " 7 ") == {26, 7}
    with pytest.raises(ValueError):
        rec_mod.only_ids("abc")
    monkeypatch.setenv(rec_mod.TEST_ONLY_PIDS_ENV, "500")
    monkeypatch.setenv(rec_mod.TEST_ONLY_HWNDS_ENV, "")
    a = rec_mod.parse_args(["--data-dir", "D", "--home", "H", "--only-hwnds", "0x10", "--only-pids", "4",
                            "--no-ocr", "--exit-after-s", "12.5"])
    assert (a.data_dir, a.home, a.hwnds, a.pids, a.no_ocr, a.exit_after_s) == ("D", "H", {16}, {4, 500}, True, 12.5)
    b = rec_mod.parse_args([])
    assert b.hwnds == set() and b.pids == {500} and not b.no_ocr and b.exit_after_s is None
    assert rec_mod.TEST_ONLY_HWNDS_ENV == "SCREENRECALL_TEST_ONLY_HWNDS"
    assert rec_mod.TEST_ONLY_PIDS_ENV == "SCREENRECALL_TEST_ONLY_PIDS"


def test_only_windows_filters_by_hwnd_and_pid():
    w = FakeWindows()
    w.add(1, "msedge.exe", "a", pid=10)
    w.add(2, "msedge.exe", "b", pid=20)
    only = rec_mod.OnlyWindows(w, pids={10})
    w.fg = 2
    assert only.foreground() == 0
    w.fg = 1
    assert only.foreground() == 1 and only.describe(1) == ("msedge.exe", "a")
    both = rec_mod.OnlyWindows(w, hwnds={2}, pids={10})
    assert not both.allowed(1) and not both.allowed(2)


def test_recorder_process_wiring(cfg, monkeypatch):
    """组装：recover → init_defaults → control 先 tick（名单生效）→ 拍一次 → 收尾心跳 stopped、关库。"""
    from tests.test_v3_capture import FakeSource

    monkeypatch.setattr(pipeline_mod, "_apis", lambda: FAKE_API)
    monkeypatch.setattr(loop_mod, "_title_checks", lambda: (lambda t, k: False, lambda t, k: False))
    clock = SimpleNamespace(t=1_000_000)
    win, made = FakeWindows(), {}
    win.add(1, "msedge.exe", "文章")
    win.fg = 1

    def factory(h, kind):
        made[h] = FakeSource(h, kind, lambda: clock.t)
        return made[h]

    store, reader = FakeStore(), FakeReader()
    reader.result = TextResult(lines("一行字"), "uia", url="https://blog.example.com/a")
    p = rec_mod.RecorderProcess(cfg, windows=win, inputs=FakeInput(), source_factory=factory,
                                window_list=lambda: [], store=store, reader=reader, clock=lambda: clock.t, pid=99,
                                private_dirs=False, clipboard=False)
    p.open()
    p.pipeline._policy = FakePolicy(cfg)
    assert store.names()[:4] == ["recover", "init_defaults", "heartbeat", "app_tiers"]
    assert store.of("init_defaults")[0][1] == cfg.default_record_apps
    for _ in range(25):
        clock.t += 100
        p.loop.step()
        if 1 in made and not made[1].frames_seen:
            made[1].push(np.full((120, 200, 3), 240, dtype=np.uint8), clock.t)
    p.pipeline.drain()
    assert len(store.of("add_snap")) == 1 and store.of("save_shot")[0][4] == "first"
    p.close()
    assert store.of("visit_leave")
    assert store.of("heartbeat")[-1][1]["recorder_state"] == "stopped"
    assert store.names()[-1] == "close"
    assert p.stats()["loop"]["snaps"] == 1


def test_private_window_name_skips_everything(P, monkeypatch):
    """无痕窗口的标记只在辅助功能的窗口名字里（Chrome 实测：标题"V2EX - Google Chrome"，根下面的 Pane 叫
    "V2EX - Google Chrome（无痕）"）：这一拍什么都不写、不 OCR，复制也不记，心跳不写它的标题。"""
    from screenrecall.v3.read import ReadResult
    from screenrecall.v3.source import is_private_title

    monkeypatch.setattr(FAKE_API, "is_private_title", is_private_title, raising=False)
    title = "合成页 - 合成浏览器"
    P.process(FocusEvent(1000, 1, 1001, "chrome.exe", title))
    P.reader_.result = ReadResult(lines("一行字"), "uia", url="https://a.example.com/",
                                  window_names=(title, title + "（无痕）"))
    P.process(snap(3000, app="chrome.exe", title=title))
    assert P.store_.calls == [] and P.counts["skip_private"] == 1
    assert P.reader_.ocr_asked is False and P.is_skipped(1, title)
    P.process(CopyEvent(3100, 1, "chrome.exe", title, "复制"))
    assert P.store_.calls == []
    # 普通窗口：名字里没有关键词，照常存
    P.reader_.result = ReadResult(lines("一行字"), "uia", url="https://a.example.com/", window_names=(title,))
    P.process(snap(6000, app="chrome.exe", title=title))
    assert "record_lines" in P.store_.names()


def test_page_name_strips_learned_window_title_tail(P):
    """网页没给 Document 名字（浏览器的 PDF 查看器）：用窗口标题去掉这个软件的尾巴，尾巴从有网页标题的拍里学。"""
    labels = []
    P.api.label_source = lambda *a: labels.append(a) or _label(*a)
    try:
        P.process(FocusEvent(1000, 1, 1001, "chrome.exe", "x.pdf - 合成浏览器"))
        P.reader_.result = TextResult(lines("PDF 的字"), "uia", url="https://a.example.com/x.pdf")
        P.process(snap(3000, app="chrome.exe", title="x.pdf - 合成浏览器"))
        assert labels[-1][3] is None  # 还没学到：label_source 退回窗口标题
        P.process(FocusEvent(4000, 1, 1001, "chrome.exe", "文章 - 合成浏览器"))
        P.reader_.result = TextResult(lines("一行字"), "uia", url="https://b.example.com/", page_title="文章")
        P.process(snap(6000, app="chrome.exe", title="文章 - 合成浏览器"))
        P.process(FocusEvent(7000, 1, 1001, "chrome.exe", "x.pdf - 合成浏览器"))
        P.reader_.result = TextResult(lines("PDF 的字"), "uia", url="https://a.example.com/x.pdf")
        P.process(snap(9000, app="chrome.exe", title="x.pdf - 合成浏览器"))
        assert labels[-1][3] == "x.pdf"
        # 别的软件不用这条尾巴；不带网址的不改
        P.process(FocusEvent(10_000, 2, 1002, "other.exe", "y.pdf - 合成浏览器"))
        P.reader_.result = TextResult(lines("别的字"), "uia", url="https://c.example.com/y.pdf")
        P.process(snap(12_000, hwnd=2, app="other.exe", title="y.pdf - 合成浏览器"))
        assert labels[-1][3] is None
    finally:
        P.api.label_source = _label
