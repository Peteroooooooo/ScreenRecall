"""录制进程用到的录制核心小钩子：request_evidence / drop_candidates / last_change_ms / foreground（活动日志）。"""

from __future__ import annotations

from screenrecall.rec.core import RecorderConfig, RecorderCore
from screenrecall.rec.inputmon import FakeInput
from screenrecall.rec.tracker import ListSink
from screenrecall.rec.windows import FakeWindows, make_rules
from tests import _rec_synth as S
from tests.test_v2_recorder import Clock, FakeSource

W, VH = 800, 480


class FgSink(ListSink):
    def foreground(self, t_ms, hwnd, pid, process, title, excluded):
        self.events.append(("foreground", dict(t_ms=t_ms, hwnd=hwnd, pid=pid, process=process, title=title,
                                               excluded=excluded)))


class Env:
    def __init__(self, **cfg):
        self.clock = Clock(1_000_000)
        self.win = FakeWindows()
        self.sink = FgSink()
        self.core = RecorderCore(self.sink, RecorderConfig(**cfg), clock=self.clock, windows=self.win,
                                 inputs=FakeInput(), source_factory=lambda h, k: FakeSource(h, k, self.clock),
                                 watcher=None)
        self.doc = S.document(W, 5000, 5)
        self.head = S.header(W)
        self.win.add(1, "notepad.exe", "笔记 - 记事本")
        self.win.add(2, "msedge.exe", "网页 - Edge")
        self.win.fg = 1

    def run(self, ms, step=40):
        end = self.clock.t + ms
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + step)
            self.core.step()

    def show(self, hwnd, top):
        self.core.pool.sources()[hwnd].push(S.view(self.doc, top, VH, self.head), self.clock.t)


def test_request_evidence_saves_current_candidate_once():
    e = Env()
    e.core.step()
    e.show(1, 0)
    e.run(1200)
    still = e.sink.of("still")[-1]["ev"]
    e.core.request_evidence(1, still.still_id + 99)  # 不是当前候选：什么都不做
    e.run(40)
    assert e.sink.of("evidence") == []
    e.core.request_evidence(1, still.still_id)
    e.run(40)
    ev = e.sink.of("evidence")
    assert len(ev) == 1 and ev[0]["reason"] == "needs_ocr" and ev[0]["ev"] is still
    # 已经记进"已存"：内容滚走时不会再存一次
    e.show(1, 240)
    e.run(1200)
    assert [x["reason"] for x in e.sink.of("evidence")] == ["needs_ocr"]
    assert e.core.trackers[1].saved_count == 1


def test_drop_candidates_before_pause_saves_nothing():
    e = Env()
    e.core.step()
    e.show(1, 0)
    e.run(1200)
    assert e.core.trackers[1].candidate is not None
    e.core.drop_candidates()
    e.core.pause()
    e.run(40)
    assert e.sink.of("evidence") == [] and e.core.trackers[1].candidate is None
    assert e.sink.of("gap")[-1]["reason"] == "paused"


def test_last_change_ms_follows_accepted_frames():
    e = Env()
    assert e.core.last_change_ms(1) is None
    e.core.step()
    e.show(1, 0)
    t0 = e.clock.t
    e.run(200)
    assert e.core.last_change_ms(1) == t0
    e.show(1, 120)
    t1 = e.clock.t
    e.run(1100)  # 1 帧/秒限速：下一帧要等一会儿才取走
    assert e.core.last_change_ms(1) == t1


def test_foreground_hook_reports_switches_exclusion_and_settled_titles():
    e = Env()
    e.core.set_rules(make_rules(excluded_title_keywords=["InPrivate"]))
    e.run(80)
    e.win.fg = 2
    e.run(80)
    e.win.windows[2]["title"] = "银行 - InPrivate"
    e.run(80)
    e.win.windows[2]["title"] = "网页 2 - Edge"  # 离开排除也立刻报
    e.run(80)
    e.win.windows[2]["title"] = "网页 3 - Edge"
    e.run(400)  # 只是标题变了：还没稳定 1 秒
    n = len(e.sink.of("foreground"))
    e.run(1000)
    e.win.fg = 0  # 桌面
    e.run(80)
    e.win.is_locked = True
    e.run(600)
    fg = [(d["hwnd"], d["process"], d["title"], d["excluded"]) for d in e.sink.of("foreground")]
    assert fg == [
        (1, "notepad.exe", "笔记 - 记事本", False),
        (2, "msedge.exe", "网页 - Edge", False),
        (2, "msedge.exe", None, True),  # 被排除：不报标题，并且立刻报
        (2, "msedge.exe", "网页 2 - Edge", False),
        (2, "msedge.exe", "网页 3 - Edge", False),
        (None, None, None, False),
    ]
    assert n == 4
    assert e.sink.of("foreground")[0]["pid"] == 1001


def test_foreground_hook_is_optional_for_plain_sinks():
    e = Env()
    e.core.sink = ListSink()
    e.run(200)  # 没有 foreground 方法也照常运行
    assert "foreground" not in e.core.sink.names()
