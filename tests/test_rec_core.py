"""v2 录制核心：会话池、截图槽限速、RecorderCore 的窗口 / 缺口 / 输入分发（全部用假的窗口系统和截图来源）。"""

from __future__ import annotations

import numpy as np
import pytest

from screenrecall.rec.core import RecorderConfig, RecorderCore
from screenrecall.rec.frames import FrameMap
from screenrecall.rec.inputmon import FakeInput
from screenrecall.rec.sessions import PoolConfig, SessionPool
from screenrecall.rec.tracker import ListSink
from screenrecall.rec.wgc import PrintWindowSource, _BufferPool, _SlotSource
from screenrecall.rec.windows import FakeWindows, WindowState, make_rules, rule_reason
from tests import _rec_synth as S

W, VH = 800, 480


class FakeSource(_SlotSource):
    """接口和 WGC 会话一样；测试直接 push 画面。"""

    def __init__(self, hwnd, kind, clock):
        super().__init__(hwnd, None, None, 1.0, clock)
        self.kind = kind
        self.stopped = False
        self.started_ms = clock()
        self._finished = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    @property
    def finished(self):
        return self._finished

    def push(self, img: np.ndarray, t: int):
        self._put(img, t, (0, 0, img.shape[1], img.shape[0]), img.shape[2] == 4)


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __init__(self, **cfg):
        self.clock = Clock()
        self.win = FakeWindows()
        self.inp = FakeInput()
        self.sink = ListSink()
        self.made: list[FakeSource] = []
        self.fail_wgc: set[int] = set()

        def factory(hwnd, kind):
            if kind == "wgc" and hwnd in self.fail_wgc:
                raise OSError("不能截")
            s = FakeSource(hwnd, kind, self.clock)
            self.made.append(s)
            return s

        self.core = RecorderCore(self.sink, RecorderConfig(**cfg), clock=self.clock, windows=self.win, inputs=self.inp,
                                 source_factory=factory, watcher=None)
        self.doc = S.document(W, 5000, 5)
        self.head = S.header(W)

    def src(self, hwnd) -> FakeSource:
        return self.core.pool.sources()[hwnd]

    def img(self, top):
        return S.view(self.doc, top, VH, self.head)

    def run(self, ms, step=40):
        end = self.clock.t + ms
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + step)
            self.core.step()

    def show(self, hwnd, img):
        self.src(hwnd).push(img, self.clock.t)


@pytest.fixture
def env():
    e = Env()
    e.win.add(1, "notepad.exe", "笔记 - 记事本")
    e.win.add(2, "msedge.exe", "网页 - Edge")
    e.win.fg = 1
    return e


# ---------- 截图槽 ----------

def test_slot_latest_wins_and_rate_limit():
    clock = Clock(0)
    s = FakeSource(5, "wgc", clock)
    s.set_max_fps(1.0)
    a, b = np.zeros((8, 8, 3), np.uint8), np.full((8, 8, 3), 200, np.uint8)
    s.push(a, 10)
    f = s.take(100)
    assert f is not None and f.t_ms == 10 and f.seq == 1
    s.push(a, 200)
    s.push(b, 300)  # 只留最新
    assert s.take(500) is None  # 距离上次取走不到 1 秒
    f = s.take(1100)
    assert f is not None and f.t_ms == 300 and int(f.bgr[0, 0, 0]) == 200
    s.set_max_fps(10)
    s.push(a, 1150)
    assert s.take(1200) is not None
    assert s.frames_seen == 4 and s.frames_taken == 3


def test_slot_bgra_frames_convert_lazily():
    clock = Clock(0)
    s = FakeSource(5, "wgc", clock)
    img = np.random.default_rng(1).integers(0, 255, size=(20, 30, 4), dtype=np.uint8)
    s.push(img, 1)
    f = s.take(10)
    assert np.array_equal(f.gray, img[:, :, 1] >> 4)
    assert np.array_equal(f.bgr, img[:, :, :3]) and f.bgr.flags.c_contiguous
    assert f.fmap == FrameMap(0, 0, 30, 20)


def test_buffer_pool_reuses_only_free_buffers():
    pool = _BufferPool(keep=2)
    a = pool.get((4, 4, 4))
    held = a  # noqa: F841 - 模拟某个 Frame 还拿着
    b = pool.get((4, 4, 4))
    assert b is not a
    del held, a
    c = pool.get((4, 4, 4))
    assert c is not b  # b 还被本函数的变量 b 拿着
    del b
    d = pool.get((4, 4, 4))
    assert d is c or d.shape == (4, 4, 4)


def test_buffer_pool_free_refcount_is_measured():
    from screenrecall.rec import wgc

    pool = _BufferPool(keep=1)
    a = pool.get((2, 2, 4))
    slot = (a, 1)  # 槽里拿着
    del a
    b = pool.get((2, 2, 4))
    assert b is not slot[0]
    del slot, b
    assert pool.get((2, 2, 4)) is pool._bufs[0]
    assert wgc._FREE_REFS >= 1


class _Fake:
    def __init__(self, v):
        self.v = v

    def __call__(self):
        return self.v


def test_clock_follows_forward_jumps_and_never_goes_back():
    from screenrecall.rec.clock import _UtcClock

    mono, wall = _Fake(1_000), _Fake(1_700_000_000_000)
    c = _UtcClock(mono, wall)
    assert c() == 1_700_000_000_000
    # 睡眠：单调时钟没走、系统时间走了 1 小时 → 追上
    wall.v += 3_600_000
    assert c() == 1_700_003_600_000
    # 小的差（< 1 秒）不管
    mono.v += 10_000
    wall.v += 10_500
    assert c() == 1_700_003_610_000
    # 系统时间往回调 1 小时：不倒退，也不停住，只是每秒少走 1 毫秒
    wall.v -= 3_600_000
    t0 = c()
    assert t0 == 1_700_003_610_000
    seen = [t0]
    for _ in range(100):
        mono.v += 1_000
        wall.v += 1_000
        seen.append(c())
    assert all(b > a for a, b in zip(seen, seen[1:]))
    assert seen[-1] - t0 == 100 * 1_000 - 100


def test_clock_real_is_monotonic_and_close_to_utc():
    import time

    from screenrecall.rec.clock import now_ms

    xs = [now_ms() for _ in range(2000)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))
    assert abs(xs[-1] - time.time() * 1000) < 1500


def test_frame_map_conversions():
    m = FrameMap.from_bounds((100, 50, 900, 650), 800, 600)
    assert (m.scale_x, m.scale_y) == (1.0, 1.0)
    assert m.to_screen(10, 20) == (110, 70)
    assert m.to_frame(110, 70) == (10, 20)
    assert m.rect_to_frame(90, 40, 200, 100) == (0, 0, 100, 50)
    assert m.rect_to_frame(0, 0, 50, 40) is None
    half = FrameMap.from_bounds((0, 0, 1600, 1200), 800, 600)
    assert half.scale_x == 2.0 and half.to_frame(400, 400) == (200, 200)


def test_printwindow_source_only_on_change():
    clock = Clock(0)
    caps = []

    class Cap:
        def __init__(self, val):
            self.bgr = bytes([val]) * (6 * 4 * 3)
            self.width, self.height, self.rect = 6, 4, (10, 20, 16, 24)

    def grab(hwnd):
        return caps.pop(0)

    src = PrintWindowSource(9, clock=clock, grab=grab)
    caps += [Cap(1), Cap(1), Cap(2)]
    assert src.poll_once() and not src.poll_once() and src.poll_once()
    f = src.take(5)
    assert f.source == "printwindow" and int(f.bgr[0, 0, 0]) == 2 and f.fmap.left == 10

    class Gone(Exception):
        reason = "window_gone"

    def gone(hwnd):
        raise Gone()

    src2 = PrintWindowSource(9, clock=clock, grab=gone)
    src2.poll_once()
    assert src2.closed


# ---------- 会话池 ----------

def test_pool_delayed_stop_cap_and_fallback():
    clock = Clock(0)
    made = []
    bad = {3}

    def factory(h, kind):
        if kind == "wgc" and h in bad:
            raise OSError("x")
        s = FakeSource(h, kind, clock)
        s.first_frame_ms = 0  # 当作已经出过帧
        made.append(s)
        return s

    pool = SessionPool(factory, PoolConfig(cap=2, stop_delay_ms=3000))
    started, stopped = pool.update([1], 0)
    assert started == [1] and stopped == []
    started, stopped = pool.update([2], 100)  # 前台换了：1 延迟停
    assert started == [2] and 1 in pool
    started, stopped = pool.update([2], 2000)
    assert stopped == [] and 1 in pool
    started, stopped = pool.update([1, 2], 2500)  # 又切回来：取消停止
    assert started == [] and pool.sources()[1].stopped is False
    pool.update([2], 2600)
    started, stopped = pool.update([2], 5700)
    assert stopped == [1] and 1 not in pool
    # 上限 2：新的窗口挤掉等着停的那个
    pool.update([4], 6000)
    started, stopped = pool.update([5, 4], 6100)
    assert 5 in pool and 4 in pool and 2 not in pool and 2 in stopped
    # WGC 打不开 → PrintWindow
    pool2 = SessionPool(factory, PoolConfig(cap=4))
    pool2.update([3], 0)
    assert pool2.kind_of(3) == "printwindow" and pool2.fallbacks == 1


def test_pool_restarts_dead_wgc_then_falls_back():
    clock = Clock(0)

    def factory(h, kind):
        return FakeSource(h, kind, clock)

    pool = SessionPool(factory, PoolConfig(max_restarts=1, first_frame_timeout_ms=1000))
    pool.update([1], 0)
    first = pool.sources()[1]
    first._finished = True
    started, _ = pool.update([1], 100)
    assert started == [1] and pool.sources()[1] is not first and pool.kind_of(1) == "wgc"
    # 开了很久都没出第一帧，也算坏了；失败次数超过上限后换 PrintWindow
    clock.t = 5000
    pool.update([1], 5000)
    assert pool.kind_of(1) == "printwindow"
    # 窗口关了的会话直接移除
    pool.sources()[1].closed = True
    _, stopped = pool.update([1], 5100)
    assert stopped == [1] and 1 not in pool


# ---------- 规则 ----------

def test_rules_reuse_existing_logic():
    rules = make_rules(excluded_processes=[" KeePass.exe "], excluded_title_keywords=["无痕", "InPrivate"])
    assert rule_reason("keepass.EXE", "密码库", rules)
    assert rule_reason("msedge.exe", "某网站 - [InPrivate] - Edge", rules)
    assert rule_reason("msedge.exe", "某网站 - inprivate", rules)
    assert rule_reason("msedge.exe", "普通网页", rules) is None
    assert rule_reason("x.exe", "y", None) is None
    assert WindowState(True, minimized=True).gap_reason == "minimized"
    assert WindowState(False).gap_reason == "closed"


# ---------- RecorderCore ----------

def test_foreground_window_is_recorded_and_still(env):
    env.core.step()
    assert 1 in env.core.pool and env.core.pool.kind_of(1) == "wgc"
    env.show(1, env.img(0))
    env.run(100)
    assert env.sink.names() == ["block_started"]
    env.run(1100)
    st = env.sink.of("still")
    assert len(st) == 1 and st[0]["ev"].hwnd == 1


def test_switching_foreground_stops_old_session_later_and_saves_candidate(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.win.fg = 2
    env.run(100)
    assert 2 in env.core.pool and 1 in env.core.pool  # 1 还没停
    assert env.sink.of("evidence") == []
    env.run(3100)
    assert 1 not in env.core.pool
    ev = env.sink.of("evidence")
    assert len(ev) == 1 and ev[0]["reason"] == "closing" and ev[0]["ev"].hwnd == 1


def test_minimized_gap(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.win.windows[1]["minimized"] = True
    env.run(100)
    g = env.sink.of("gap")
    assert g and g[-1]["reason"] == "minimized" and g[-1]["t1_ms"] is None
    assert env.sink.of("evidence")[-1]["reason"] == "closing"
    env.win.windows[1]["minimized"] = False
    env.run(100)
    assert env.sink.of("gap")[-1]["t1_ms"] is not None


def test_excluded_title_drops_frames(env):
    env.core.set_rules(make_rules(excluded_title_keywords=["InPrivate"]))
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    n_still = len(env.sink.of("still"))
    env.win.windows[1]["title"] = "银行 - InPrivate"
    env.run(40)
    assert env.sink.of("gap")[-1]["reason"] == "excluded"
    env.show(1, env.img(300))
    env.run(2000)
    assert len(env.sink.of("still")) == n_still
    assert env.core.trackers[1].latest is None
    env.win.windows[1]["title"] = "普通网页"
    env.run(40)
    assert env.sink.of("gap")[-1]["t1_ms"] is not None


def test_excluded_process_never_opens_session(env):
    env.core.set_rules(make_rules(excluded_processes=["notepad.exe"]))
    env.run(200)
    assert 1 not in env.core.pool
    assert env.sink.of("gap")[0]["reason"] == "excluded"


def test_lock_closes_sessions_and_unlock_resumes(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.win.is_locked = True
    env.run(600)
    assert len(env.core.pool) == 0
    assert env.sink.of("gap")[-1]["reason"] == "locked"
    env.win.is_locked = False
    env.run(600)
    assert env.sink.of("gap")[-1]["t1_ms"] is not None and 1 in env.core.pool


def test_pinned_windows_and_cap(env):
    for h in (3, 4, 5, 6):
        env.win.add(h, "x.exe", f"窗口 {h}")
        env.core.pin(h)
    env.core.step()
    assert set(env.core.pool.sources()) == {1, 3, 4, 5}  # 前台 + 前 3 个钉住的（上限 4）
    env.core.unpin(3)
    env.core.step()
    assert env.core.pinned == (4, 5, 6)


def test_newly_pinned_window_is_read_on_its_first_frame(env):
    """刚钉住的窗口：第一帧来了马上静止判断（不等 1 秒）；钉住一个已经跟踪过、画面没变的窗口也一样
    （一直不变的窗口没有"下一次变化"，不然第一屏永远不读）。"""
    env.core.pin(2)
    env.core.step()
    env.show(2, env.img(0))
    env.run(80)
    st = [e["ev"] for e in env.sink.of("still") if e["ev"].hwnd == 2]
    assert len(st) == 1 and not st[0].identical_to_saved
    # 窗口 1 在前台录过、静止过；换到窗口 2 前台后它的会话停了。之后把它钉住，画面和之前一模一样
    env.show(1, env.img(300))
    env.run(1200)
    env.win.fg = 2
    env.run(3200)
    assert 1 not in env.core.pool
    n = len(env.sink.of("still"))
    env.core.pin(1)
    env.core.step()
    env.show(1, env.img(300))
    env.run(80)
    assert len(env.sink.of("still")) == n + 1 and env.sink.of("still")[-1]["ev"].hwnd == 1


def test_window_back_from_a_gap_is_read_on_its_first_frame(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    n = len(env.sink.of("still"))
    env.win.windows[1]["minimized"] = True
    env.run(100)
    env.win.windows[1]["minimized"] = False
    env.run(40)
    env.show(1, env.img(0))
    env.run(80)
    assert len(env.sink.of("still")) == n + 1


def test_moving_content_keeps_normal_still_timing_after_a_pin(env):
    env.core.pin(2)
    env.core.step()
    env.show(2, env.img(0))
    env.run(80)
    n = len(env.sink.of("still"))
    env.show(2, env.img(200))  # 画面在动：按平常的计时
    env.run(400)
    assert len(env.sink.of("still")) == n
    env.run(800)
    assert len(env.sink.of("still")) == n + 1


def test_closed_window_drops_tracker_and_pin(env):
    env.core.pin(2)
    env.core.step()
    env.show(2, env.img(0))
    env.run(1200)
    del env.win.windows[2]
    env.run(100)
    assert 2 not in env.core.trackers and env.core.pinned == ()
    assert env.sink.of("gap")[-1]["reason"] == "closed"
    assert env.sink.of("evidence")[-1]["reason"] == "closing"


def test_wheel_boosts_rate_and_reaches_tracker(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    src = env.src(1)
    assert src.max_fps == 1.0
    for _ in range(3):
        env.inp.wheel(env.clock.t, 120)
    env.run(40)
    assert src.max_fps == 10.0
    env.run(800)
    assert env.sink.of("block_anchor")[-1]["anchor"] == "top"
    env.run(1000)
    assert src.max_fps == 1.0


def test_navigation_key_boosts_rate_of_the_foreground_window(env):
    """翻页 / 方向 / 空格键之后 nav_boost_ms 内用 wheel_fps（浏览器平滑翻页的动画要多拿几帧）；普通按键不算。"""
    env.core.pin(2)
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    src = env.src(1)
    env.inp.key(env.clock.t)  # 打字
    env.run(40)
    assert src.max_fps == 1.0
    env.inp.key(env.clock.t, nav=True)
    env.run(40)
    assert src.max_fps == 10.0 and env.src(2).max_fps == 1.0  # 只给前台窗口
    env.run(600)
    assert src.max_fps == 1.0


def test_consecutive_shifts_keep_the_fast_rate(env):
    """画面一帧帧平移（平滑滚动、惯性，没有滚轮事件）：最后一次判成平移之后 shift_boost_ms 内都用 wheel_fps，
    整段动画都能接上。"""
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    src = env.src(1)
    env.inp.key(env.clock.t, nav=True)
    tops = [0]
    for top in (40, 160, 330, 420):  # 一次翻页的动画：先快后慢
        env.run(100)
        env.show(1, env.img(top))
        tops.append(top)
    env.run(2000)
    assert env.core.trackers[1].pos_y == 420
    assert env.core.feed_kinds.get("shift", 0) == 4 and env.core.feed_kinds.get("break", 0) == 0
    # 没有按键、只靠"一直在平移"：第一帧按 1 帧/秒来，之后每一帧都还在平移 → 一直快
    env.run(1000)
    t = env.clock.t
    env.show(1, env.img(460))
    env.run(1000)
    assert src.max_fps == 1.0
    env.show(1, env.img(500))
    env.run(80)
    assert env.core.feed_kinds["shift"] == 6 and src.max_fps == 10.0
    env.run(100)
    env.show(1, env.img(540))
    env.run(100)
    assert env.core.feed_kinds["shift"] == 7  # 100 ms 后的下一帧就收了
    env.run(700)
    assert src.max_fps == 1.0 and env.clock.t > t


def test_wheel_over_other_window_goes_there(env):
    env.core.pin(2)
    env.core.step()
    env.show(1, env.img(0))
    env.show(2, env.img(0))
    env.run(1200)
    env.win.under_cursor = 2
    env.inp.wheel(env.clock.t, -120)
    env.run(40)
    assert env.src(2).max_fps == 10.0 and env.src(1).max_fps == 1.0


def test_keys_go_to_foreground_tracker(env):
    env.core.step()
    a = env.img(0)
    env.show(1, a)
    env.run(1200)
    n = len(env.sink.of("still"))
    env.inp.key(env.clock.t)
    env.show(1, S.write_text(a, 20, 200, "打字 typing"))
    env.run(1500)
    assert len(env.sink.of("still")) == n
    env.run(700)
    assert len(env.sink.of("still")) == n + 1


def test_pause_and_resume(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.core.pause()
    env.run(40)
    assert env.core.paused and len(env.core.pool) == 0
    assert env.sink.of("gap")[-1]["reason"] == "paused"
    env.core.resume()
    env.run(40)
    assert env.sink.of("gap")[-1]["t1_ms"] is not None and 1 in env.core.pool


def test_wgc_failure_falls_back_to_printwindow(env):
    env.fail_wgc.add(1)
    env.core.step()
    assert env.core.pool.kind_of(1) == "printwindow"


def test_block_and_still_ids_unique_across_windows(env):
    env.core.pin(2)
    env.core.step()
    env.show(1, env.img(0))
    env.show(2, env.img(100))
    env.run(1200)
    blocks = [b["block"] for b in env.sink.of("block_started")]
    stills = [s["ev"].still_id for s in env.sink.of("still")]
    assert len(set(blocks)) == len(blocks) == 2 and len(set(stills)) == len(stills) == 2


def _events_for(sink, hwnd, names=("block_started", "still", "skim", "block_anchor")):
    out = []
    for n, d in sink.events:
        if n not in names:
            continue
        h = d["ev"].hwnd if "ev" in d else d["hwnd"]
        if h == hwnd:
            out.append(n)
    return out


def test_excluded_window_is_not_recorded_after_losing_foreground(env):
    """命中排除规则的窗口被切到后台后，会话还要延迟约 3 秒才停：这期间它的画面不能被录进来。"""
    env.core.set_rules(make_rules(excluded_title_keywords=["InPrivate"]))
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.win.windows[1]["title"] = "银行 - InPrivate"
    env.run(40)
    assert env.sink.of("gap")[-1]["reason"] == "excluded"
    before = _events_for(env.sink, 1)
    env.win.fg = 2
    env.run(40)
    assert 1 in env.core.pool  # 会话还没停
    env.show(1, env.img(900))  # 后台的排除窗口还在变
    env.run(1500)
    env.show(1, env.img(1200))
    env.run(1500)
    assert _events_for(env.sink, 1) == before
    assert env.core.trackers[1].frames == 1


def test_background_window_during_delayed_stop_is_not_fed(env):
    """前台换走后，旧窗口在后台（可能被挡住、用户看不到）的新画面不录。"""
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.win.fg = 2
    env.run(80)
    n = env.core.trackers[1].frames
    env.show(1, env.img(700))
    env.run(1500)
    assert env.core.trackers[1].frames == n
    assert _events_for(env.sink, 1, ("still",)) == ["still"]
    assert env.core.stats()["recording"] == [2]


def test_last_frame_before_leaving_foreground_is_kept(env):
    """离开前台前刚到的一帧（还在限速里没取走）在离开那一轮收走，不丢。"""
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.show(1, env.img(120))
    env.run(40)
    assert env.core.trackers[1].pos_y == 120
    env.show(1, env.img(240))  # 上一帧刚取走，1 帧/秒限速下这一帧要等将近 1 秒
    env.run(40)
    assert env.core.trackers[1].pos_y == 120
    env.win.fg = 2
    env.run(40)
    assert env.core.trackers[1].pos_y == 240
    assert [e["reason"] for e in env.sink.of("evidence")] == ["leaving"]


def test_last_frame_is_dropped_if_window_became_excluded(env):
    env.core.set_rules(make_rules(excluded_title_keywords=["InPrivate"]))
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.show(1, env.img(120))
    env.run(40)
    env.show(1, env.img(240))  # 还在限速里
    env.win.windows[1]["title"] = "银行 - InPrivate"  # 同一瞬间改了标题又切走
    env.win.fg = 2
    env.run(40)
    assert env.core.trackers[1].pos_y == 120
    env.run(1500)  # 会话延迟停的这段时间里也不能再取走它
    assert env.core.trackers[1].pos_y == 120


def test_stale_wheel_events_after_pause_do_not_make_anchor(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.core.pause()
    env.run(40)
    for _ in range(3):
        env.inp.wheel(env.clock.t, 120)
    env.run(2000)
    env.core.resume()
    env.run(1500)
    assert env.sink.of("block_anchor") == []


def test_wheel_map_only_keeps_windows_with_sessions(env):
    env.core.step()
    for h in range(100, 140):
        env.win.under_cursor = h
        env.inp.wheel(env.clock.t, -120)
        env.run(40)
    assert set(env.core._wheel_at) <= set(env.core.pool.sources())


def test_stats_from_another_thread_while_stepping(env):
    import threading

    env.core.pin(2)
    errors = []
    done = threading.Event()

    def reader():
        while not done.is_set():
            try:
                env.core.stats()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    th = threading.Thread(target=reader)
    th.start()
    try:
        for i in range(30):
            env.win.add(10 + i, "x.exe", f"窗口 {i}")
            env.win.fg = 10 + i
            env.run(40)
    finally:
        done.set()
        th.join()
    assert errors == []


def test_stop_without_start_still_shuts_down(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.core.stop()
    assert env.sink.of("gap")[-1]["reason"] == "stopped" and not env.core.trackers


def test_shutdown_closes_everything(env):
    env.core.step()
    env.show(1, env.img(0))
    env.run(1200)
    env.core.shutdown(env.clock.t)
    assert env.sink.of("evidence")[-1]["reason"] == "closing"
    assert env.sink.of("gap")[-1]["reason"] == "stopped"
    assert len(env.core.pool) == 0 and not env.core.trackers
