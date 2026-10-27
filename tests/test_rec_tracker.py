"""v2 录制快路径：WindowTracker 的每个分支（合成画面 + 注入时间，不碰真实窗口）。"""

from __future__ import annotations

import numpy as np
import pytest

from screenrecall.rec.frames import Frame, FrameMap
from screenrecall.rec.inputmon import WheelEvent
from screenrecall.rec.tracker import ListSink, TrackerConfig, WindowTracker
from tests import _rec_synth as S

W, VH = 900, 520
HWND = 7


@pytest.fixture(scope="module")
def doc():
    return S.document(W, 6000, 2)


@pytest.fixture(scope="module")
def head():
    return S.header(W)


class Rig:
    def __init__(self, doc, head, **cfg):
        self.doc, self.head = doc, head
        self.sink = ListSink()
        self.tr = WindowTracker(HWND, self.sink, TrackerConfig(**cfg))
        self.t = 10_000

    def img(self, top: int) -> np.ndarray:
        return S.view(self.doc, top, VH, self.head)

    def feed(self, img: np.ndarray, dt: int = 100) -> str:
        self.t += dt
        return self.tr.feed(Frame.from_bgr(HWND, self.t, img, FrameMap(100, 50, img.shape[1], img.shape[0])))

    def wait(self, ms: int, step: int = 50) -> None:
        end = self.t + ms
        while self.t < end:
            self.t = min(end, self.t + step)
            self.tr.tick(self.t)

    def wheel(self, delta: int, dt: int = 30) -> None:
        self.t += dt
        self.tr.on_wheel(WheelEvent(self.t, delta, False, 0, 0))

    def names(self):
        return self.sink.names()


@pytest.fixture
def rig(doc, head):
    return Rig(doc, head)


def test_first_frame_starts_block_and_still_after_1s(rig):
    assert rig.feed(rig.img(0)) == "first"
    assert rig.names() == ["block_started"]
    assert rig.sink.of("block_started")[0]["after_block"] is None
    rig.wait(900)
    assert "still" not in rig.names()
    rig.wait(150)
    stills = rig.sink.of("still")
    assert len(stills) == 1
    ev = stills[0]["ev"]
    assert (ev.block, ev.pos_y, ev.pos_x) == (0, 0, 0)
    assert not ev.identical_to_saved and ev.new_cells > 0
    assert ev.bgr.shape == (VH + 60, W, 3) and ev.gray.shape == (VH + 60, W)
    assert ev.fmap.to_screen(0, 0) == (100, 50)
    assert rig.tr.candidate is ev
    # 静止只发一次
    rig.wait(3000)
    assert len(rig.sink.of("still")) == 1


def test_scroll_positions_follow_truth(rig):
    rig.feed(rig.img(0))
    tops = [0]
    for d in (37, 41, 103, 250, 7, 333):
        tops.append(tops[-1] + d)
        assert rig.feed(rig.img(tops[-1])) == "shift"
        assert rig.tr.pos_y == tops[-1]
    rig.feed(rig.img(tops[-1] - 120))
    assert rig.tr.pos_y == tops[-1] - 120
    rig.wait(1100)
    ev = rig.sink.of("still")[-1]["ev"]
    assert ev.pos_y == tops[-1] - 120
    skims = rig.sink.of("skim")
    assert len(skims) == 1
    sk = skims[0]
    assert sk["y0"] == 0 and sk["y1"] == tops[-1] - 120 and sk["direction"] == "down"
    assert sk["distance_px"] == sum((37, 41, 103, 250, 7, 333)) + 120


def test_leaving_content_saves_candidate(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    cand = rig.tr.candidate
    assert cand is not None
    rig.feed(rig.img(150))  # 顶上 150 行滚出去了，它们还没存过
    ev = rig.sink.of("evidence")
    assert len(ev) == 1 and ev[0]["reason"] == "leaving" and ev[0]["ev"] is cand
    assert rig.tr.candidate is None and rig.tr.saved_count == 1


def test_small_scroll_keeps_candidate_until_content_leaves(rig):
    """候选帧里没存过的内容还在屏幕上时不存；滚到它离开时才存，而且存的是那次静止的画面。"""
    rig.feed(rig.img(0))
    rig.wait(1100)
    cand = rig.tr.candidate
    # 只把标题栏下方的空白滚走 3 行：文档第 0..9 行是空白，没有内容离开
    assert rig.feed(rig.img(3)) == "shift"
    assert rig.sink.of("evidence") == []
    assert rig.tr.candidate is cand
    rig.feed(rig.img(300))
    assert [e["reason"] for e in rig.sink.of("evidence")] == ["leaving"]


def test_inplace_change_saves_candidate_as_replaced(rig):
    a = rig.img(400)
    rig.feed(a)
    rig.wait(1100)
    cand = rig.tr.candidate
    b = S.write_text(a, 20, 200, "原地改掉的一行 replaced line 7")
    assert rig.feed(b) == "inplace"
    ev = rig.sink.of("evidence")
    assert len(ev) == 1 and ev[0]["reason"] == "replaced" and ev[0]["ev"] is cand
    rig.wait(1100)
    st = rig.sink.of("still")[-1]["ev"]
    # 新的静止帧只有改过的那一行是新的
    assert not st.identical_to_saved and 0 < st.new_rows <= 40
    assert rig.tr.candidate is st


def test_identical_revisit_does_not_save_again(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    rig.feed(rig.img(400))  # 第一张存下（leaving）
    rig.wait(1100)
    rig.feed(rig.img(0))    # 回到原处：第二张的下半部分离开 → 存第二张
    assert [e["reason"] for e in rig.sink.of("evidence")] == ["leaving", "leaving"]
    rig.wait(1100)
    st = rig.sink.of("still")[-1]["ev"]
    assert st.identical_to_saved and st.saved_ref == rig.sink.of("still")[0]["ev"].still_id
    assert rig.tr.candidate is None
    # 再滚走、再回来：不再存
    rig.feed(rig.img(200))
    rig.wait(1100)
    rig.feed(rig.img(0))
    rig.wait(1100)
    assert len(rig.sink.of("evidence")) == 2
    assert rig.sink.of("still")[-1]["ev"].identical_to_saved


def test_covered_by_two_saved_frames_needs_no_candidate(rig):
    for top in (0, 300, 600):  # 每次停 1 秒；往下滚时上一张的新内容离开 → 存
        rig.feed(rig.img(top))
        rig.wait(1100)
    rig.feed(rig.img(400))  # 往回滚：第三张底部的新内容离开 → 存
    assert len(rig.sink.of("evidence")) == 3
    rig.wait(1100)
    st = rig.sink.of("still")[-1]["ev"]  # 这一屏分别在第二、三张里
    assert not st.identical_to_saved and st.new_cells == 0
    assert rig.tr.candidate is None


def test_video_only_change_is_dropped(rig):
    base = rig.img(1200)
    rig.feed(base)
    rig.wait(1100)
    results = []
    for i in range(8):
        results.append(rig.feed(S.paint_box(base, 600, 200, 160, 96, seed=i), dt=1000))
        rig.tr.tick(rig.t)
    # 前几次是原地变（还没认出是视频），之后全部丢掉
    assert results[0] == "inplace"
    assert results[-3:] == ["volatile"] * 3
    n_still = len(rig.sink.of("still"))
    n_ev = len(rig.sink.of("evidence"))
    for i in range(8, 12):
        assert rig.feed(S.paint_box(base, 600, 200, 160, 96, seed=i), dt=1000) == "volatile"
        rig.wait(1000)
    assert len(rig.sink.of("still")) == n_still and len(rig.sink.of("evidence")) == n_ev
    # 视频在播时，视频之外的变化照样算
    changed = S.write_text(S.paint_box(base, 600, 200, 160, 96, seed=99), 20, 420, "视频旁边的新字 new words")
    assert rig.feed(changed) == "inplace"


def test_fast_jump_is_break_with_skim_and_direction(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    for top in (120, 240, 360):
        rig.wheel(-120)
        assert rig.feed(rig.img(top)) == "shift"
    rig.wheel(-120)
    assert rig.feed(rig.img(3000)) == "break"
    names = rig.names()
    i_skim, i_block = names.index("skim"), len(names) - 1 - names[::-1].index("block_started")
    assert i_skim < i_block
    sk = rig.sink.of("skim")[0]
    assert sk["y0"] == 0 and sk["y1"] == 360 and sk["direction"] == "down"
    bs = rig.sink.of("block_started")[-1]
    assert bs["block"] == 1 and bs["after_block"] == 0 and bs["direction"] == "down"
    assert rig.tr.block == 1 and rig.tr.pos_y == 0
    # 第一张候选在第一次滚动时就因为离开被存了
    assert [e["reason"] for e in rig.sink.of("evidence")] == ["leaving"]


def test_break_without_wheel_has_no_direction_and_replaced(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    other = S.view(S.document(W, 3000, 77), 0, VH, rig.head)
    assert rig.feed(other) == "break"
    assert rig.sink.of("block_started")[-1]["direction"] is None
    assert [e["reason"] for e in rig.sink.of("evidence")] == ["replaced"]


def test_wheel_at_top_gives_top_anchor(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    for _ in range(3):
        rig.wheel(120)
    rig.wait(300)
    assert rig.sink.of("block_anchor") == []
    rig.wait(400)
    an = rig.sink.of("block_anchor")
    assert len(an) == 1 and an[0]["anchor"] == "top" and an[0]["pos_y"] == 0 and an[0]["block"] == 0
    # 同一位置再滚，不重复发
    for _ in range(3):
        rig.wheel(120)
    rig.wait(800)
    assert len(rig.sink.of("block_anchor")) == 1


def test_wheel_down_at_bottom_gives_bottom_anchor(rig):
    rig.feed(rig.img(0))
    rig.feed(rig.img(250))
    rig.feed(rig.img(500))
    rig.wait(1100)
    for _ in range(2):
        rig.wheel(-120)
    rig.wait(700)
    an = rig.sink.of("block_anchor")
    assert an and an[-1]["anchor"] == "bottom" and an[-1]["pos_y"] == 500 and an[-1]["block"] == 0


def test_wheel_that_scrolls_is_not_an_anchor(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    rig.wheel(-120)
    rig.wheel(-120)
    rig.feed(rig.img(240))
    rig.wait(1200)
    assert rig.sink.of("block_anchor") == []


def test_typing_waits_two_seconds(rig):
    a = rig.img(600)
    rig.feed(a)
    rig.wait(1100)
    n0 = len(rig.sink.of("still"))
    rig.t += 100
    rig.tr.on_key(rig.t)
    b = S.write_text(a, 20, 260, "正在打字 typing")
    assert rig.feed(b, dt=10) == "inplace"
    rig.wait(1500)
    assert len(rig.sink.of("still")) == n0
    rig.wait(600)
    assert len(rig.sink.of("still")) == n0 + 1


def test_typing_does_not_make_the_line_volatile(rig):
    a = rig.img(600)
    rig.feed(a)
    rig.wait(1100)
    out = []
    for i in range(6):
        rig.t += 300
        rig.tr.on_key(rig.t)
        out.append(rig.feed(S.write_text(a, 20, 260, "打字 " + "字" * (i + 1)), dt=10))
    assert out == ["inplace"] * 6


def test_gap_saves_candidate_and_reports_interval(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    cand = rig.tr.candidate
    rig.tr.gap_start(rig.t, "minimized")
    assert rig.sink.of("evidence")[-1]["reason"] == "closing" and rig.sink.of("evidence")[-1]["ev"] is cand
    g = rig.sink.of("gap")
    assert g == [dict(hwnd=HWND, t0_ms=rig.t, t1_ms=None, reason="minimized")]
    t0 = rig.t
    assert rig.feed(rig.img(0)) == "ignored"
    rig.t += 5000
    rig.tr.gap_end(rig.t)
    assert rig.sink.of("gap")[-1] == dict(hwnd=HWND, t0_ms=t0, t1_ms=rig.t, reason="minimized")
    # 回来后画面没变：不开新块、不再存
    assert rig.feed(rig.img(0)) == "same"
    rig.wait(1100)
    assert len(rig.sink.of("evidence")) == 1


def test_excluded_gap_forgets_latest_frame(rig):
    rig.feed(rig.img(0))
    rig.tr.gap_start(rig.t, "excluded")
    assert rig.tr.latest is None
    rig.tr.gap_end(rig.t + 1000)
    rig.t += 1000
    assert rig.feed(rig.img(0)) == "first"
    assert [b["block"] for b in rig.sink.of("block_started")] == [0, 1]


def test_wheel_during_gap_is_ignored(rig):
    """缺口里转的滚轮，缺口结束后不能被当成"滚轮在转、画面不动"而误报到顶。"""
    rig.feed(rig.img(0))
    rig.wait(1100)
    rig.tr.gap_start(rig.t, "locked")
    for _ in range(3):
        rig.wheel(120)
    rig.tr.gap_end(rig.t)
    rig.wait(1000)
    assert rig.sink.of("block_anchor") == []


def test_close_saves_candidate(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    rig.tr.close(rig.t)
    assert rig.sink.of("evidence")[-1]["reason"] == "closing"
    assert rig.sink.of("gap")[-1]["reason"] == "closed"
    assert rig.feed(rig.img(10)) == "ignored"


def test_revisit_after_break_rejoins_old_block(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    other = S.view(S.document(W, 3000, 78), 0, VH, rig.head)
    rig.feed(other)            # 跳到别的页面：存第一张（replaced），开块 1
    rig.wait(1100)
    rig.feed(rig.img(0))       # 回来：又接不上，开块 2；存块 1 的画面
    assert rig.tr.block == 2
    rig.wait(1100)
    st = rig.sink.of("still")[-1]["ev"]
    assert st.identical_to_saved and st.block == 0 and st.rejoined_from == 2
    assert rig.tr.block == 0
    rig.feed(rig.img(100))
    assert rig.tr.block == 0 and rig.tr.pos_y == 100


def test_resize_is_new_block(rig):
    rig.feed(rig.img(0))
    rig.wait(1100)
    small = S.view(rig.doc, 0, VH - 100, rig.head)
    assert rig.feed(small) == "break"
    assert [e["reason"] for e in rig.sink.of("evidence")] == ["replaced"]


def test_blank_window_has_no_candidate(rig):
    blank = np.full((VH, W, 3), 250, np.uint8)
    rig.feed(blank)
    rig.wait(1100)
    st = rig.sink.of("still")[-1]["ev"]
    assert st.new_cells == 0 and rig.tr.candidate is None
