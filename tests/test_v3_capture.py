"""v3 capture 单元：SnapTrigger（合成画面 + 假时钟）、打字判断、CaptureLoop（假的窗口系统 / 输入 / 截图来源）、剪贴板回调。"""

from __future__ import annotations

import queue

import numpy as np
import pytest

from screenrecall.rec.frames import Frame
from screenrecall.rec.inputmon import FakeInput, TypingState
from screenrecall.rec.wgc import _SlotSource
from screenrecall.rec.windows import FakeWindows
from screenrecall.v3 import loop as loop_mod
from screenrecall.v3.config import build
from screenrecall.v3.loop import CaptureLoop
from screenrecall.v3.trigger import SnapTrigger
from screenrecall.v3.types import AwayEvent, CopyEvent, FocusEvent, GreyEvent, SnapEvent

H, W = 256, 384


def img(seed: int = 0) -> np.ndarray:
    out = np.full((H, W, 3), 250, dtype=np.uint8)
    out[40:60, 20:300] = 30 + seed % 7  # 一行"字"
    return out


def changed(base: np.ndarray, y: int, x: int, h: int, w: int, seed: int) -> np.ndarray:
    out = base.copy()
    out[y:y + h, x:x + w] = np.random.default_rng(seed).integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    return out


def fr(arr: np.ndarray, t: int) -> Frame:
    return Frame.from_bgr(1, t, arr)


@pytest.fixture
def cfg(tmp_path):
    return build({}, data_dir=tmp_path, home=tmp_path)


# ---------------- SnapTrigger ----------------

def test_trigger_still_after_2s_and_changed_box(cfg):
    tr = SnapTrigger(cfg)
    base = img()
    tr.feed(fr(base, 0), 0)
    assert tr.tick(1999, typing=False) is None
    assert tr.tick(2000, typing=False) == "still"
    assert tr.snapped(2000) is None  # 第一帧：整幅都算新的
    assert tr.tick(5000, typing=False) is None  # 没再变：不再拍

    tr.feed(fr(changed(base, 100, 70, 20, 40, 1), 3000), 3000)  # 一块小区域变了
    assert tr.tick(4999, typing=False) is None
    assert tr.tick(5000, typing=False) == "still"
    assert tr.snapped(5000) == (64, 96, 64, 32)  # 32 像素小块的外接框：x 64–128，y 96–128
    assert tr.latest.t_ms == 3000


def test_trigger_forced_after_10s_of_change(cfg):
    tr = SnapTrigger(cfg)
    tr.feed(fr(img(), 0), 0)
    assert tr.tick(2000, typing=False) == "still"
    tr.snapped(2000)
    reasons = []
    for i, t in enumerate(range(3000, 16000, 500)):  # 整屏一直在变（视频）：不是"小块"，照算变化
        tr.feed(fr(changed(img(), 0, 0, H, W, i), t), t)
        for tt in range(t, t + 500, 100):
            r = tr.tick(tt, typing=False)
            if r:
                reasons.append((tt, r))
                tr.snapped(tt)
    assert reasons[0] == (13000, "forced")
    assert all(r == "forced" for _, r in reasons)


def test_trigger_ignores_volatile_corner(cfg):
    tr = SnapTrigger(cfg)
    base = img()
    tr.feed(fr(base, 0), 0)
    assert tr.tick(2000, typing=False) == "still"
    tr.snapped(2000)
    fired = []
    for i, t in enumerate(range(2500, 21000, 500)):  # 右下角 32×32 的"时钟"一直在跳
        tr.feed(fr(changed(base, 224, 352, 32, 32, i), t), t)
        r = tr.tick(t, typing=False)
        if r:
            fired.append((t, r))
            tr.snapped(t)
    # 前几次还没认出它一直在变，算一次变化、停 2 秒拍一次；之后再也不拍（也不强制拍）
    assert len(fired) == 1 and fired[0][1] == "still"
    corner = changed(base, 224, 352, 32, 32, 999)
    text = corner.copy()
    text[130:150, 20:200] = 0  # 正文里出了新的一行
    tr.feed(fr(text, 21000), 21000)
    assert tr.tick(23000, typing=False) == "still"
    x, y, w, h = tr.snapped(23000)
    assert (x, y) == (0, 128) and x + w <= 224 and y + h <= 160  # 框里没有一直在变的角落


def test_trigger_typing_defers_then_fires(cfg):
    tr = SnapTrigger(cfg)
    tr.feed(fr(img(), 0), 0)
    assert tr.tick(2500, typing=True) is None
    assert tr.tick(12000, typing=True) is None  # 打字时连强制拍也先不拍
    assert tr.tick(12100, typing=False) == "still"


def test_trigger_does_not_learn_volatile_while_typing(cfg):
    """打字时同一行的小块一直在变，不能学成"一直在变"：停手后再改这一行照样算变化。"""
    def run(typing: bool):
        tr = SnapTrigger(cfg)
        base = img()
        tr.feed(fr(base, 0), 0)
        tr.tick(2000, typing=False)
        tr.snapped(2000)
        for i, t in enumerate(range(2200, 6200, 200)):  # 在同一块里连续打字 4 秒
            tr.feed(fr(changed(base, 100, 64, 20, 30, i), t), t, typing=typing)
        tr.tick(12000, typing=False)
        tr.snapped(12000)
        tr.feed(fr(changed(base, 100, 64, 20, 30, 500), 12500), 12500)  # 停手后又改了这一行
        return tr.tick(14500, typing=False)

    assert run(typing=True) == "still"
    assert run(typing=False) is None  # 对照：不带 typing 时会被学成一直在变、改动被忽略


def test_trigger_reset_fires_for_static_window(cfg):
    tr = SnapTrigger(cfg)
    tr.feed(fr(img(), 0), 0)
    tr.tick(2000, typing=False)
    tr.snapped(2000)
    tr.feed(fr(img(), 5000), 5000)  # 一模一样的画面：不算变化
    assert tr.tick(9000, typing=False) is None
    tr.reset(10000)  # 切进来
    assert tr.tick(11999, typing=False) is None
    assert tr.tick(12000, typing=False) == "still"
    assert tr.snapped(12000) is None


def test_trigger_no_frame_no_snap(cfg):
    tr = SnapTrigger(cfg)
    tr.reset(0)
    assert tr.tick(5000, typing=False) is None


# ---------------- 打字判断 ----------------

def test_typing_state_classification():
    s = TypingState()
    assert s.key(0x41, True, 0)                 # A
    assert not s.key(0x25, True, 10)            # 方向键
    assert not s.key(0x22, True, 20)            # PageDown
    assert not s.key(0x20, True, 25)            # 空格（翻页）
    assert not s.key(0x10, True, 30)            # Shift 本身
    assert s.key(0x41, True, 40)                # Shift+A 照样算打字
    s.key(0x10, False, 50)
    assert not s.key(0x11, True, 60)            # Ctrl
    assert not s.key(0x56, True, 70)            # Ctrl+V：粘贴不算
    assert not s.key(0x43, True, 80)            # Ctrl+C
    assert not s.key(0x56, False, 90)
    s.key(0x11, False, 100)
    assert s.key(0x56, True, 110)               # 松开 Ctrl 后的 V 算
    assert not s.key(0xA4, True, 120)           # 左 Alt
    assert not s.key(0x09, True, 130)           # Alt+Tab
    s.key(0xA4, False, 140)
    assert not s.key(0x5B, True, 200)           # Win，没收到抬起（Win+L 锁屏）
    assert not s.key(0x4C, True, 210)
    assert s.key(0x41, True, 200 + 31000)       # 很久没再按下：当已经松开


def test_fake_input_typing():
    inp = FakeInput()
    inp.key(100, nav=True)
    assert inp.last_typing_ms is None
    inp.key(200, typing=False)  # Ctrl+V
    assert inp.last_typing_ms is None
    inp.typing(300)
    assert inp.last_typing_ms == 300


# ---------------- CaptureLoop ----------------

class FakeSource(_SlotSource):
    def __init__(self, hwnd, kind, clock):
        super().__init__(hwnd, None, None, 1.0, clock)
        self.kind = kind
        self.started_ms = clock()
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    @property
    def finished(self):
        return False

    def push(self, arr, t):
        self._put(arr, t, (0, 0, arr.shape[1], arr.shape[0]), False)


class Clock:
    def __init__(self, t=1_000_000):
        self.t = t

    def __call__(self):
        return self.t


class Env:
    def __init__(self, cfg):
        self.cfg = cfg
        self.clock = Clock()
        self.win = FakeWindows()
        self.inp = FakeInput()
        self.q: queue.Queue = queue.Queue()
        self.made: dict[int, FakeSource] = {}

        def factory(hwnd, kind):
            s = FakeSource(hwnd, kind, self.clock)
            self.made[hwnd] = s
            return s

        def window_list():
            return [(h, w["process"], w["title"]) for h, w in self.win.windows.items()]

        self.loop = CaptureLoop(cfg, self.q, windows=self.win, inputs=self.inp, source_factory=factory,
                                clock=self.clock, window_list=window_list)
        self.loop.set_tiers({"msedge.exe": "record"})

    def run(self, ms: int, step: int = 100) -> None:
        end = self.clock.t + ms
        while self.clock.t < end:
            self.clock.t = min(end, self.clock.t + step)
            self.loop.step()

    def show(self, hwnd: int, arr: np.ndarray) -> None:
        self.made[hwnd].push(arr, self.clock.t)

    def events(self, cls=None) -> list:
        out = []
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            if cls is None or isinstance(ev, cls):
                out.append(ev)
        return out


@pytest.fixture
def env(cfg, monkeypatch):
    def checks():
        def private(title, kws):
            return any(k.lower() in title.lower() for k in kws)

        return private, private

    monkeypatch.setattr(loop_mod, "_title_checks", checks)
    e = Env(cfg)
    e.win.add(1, "msedge.exe", "文章 - Edge")
    e.win.add(2, "Code.exe", "main.py - VS Code")
    e.win.add(3, "zoom.exe", "会议")
    e.win.add(4, "explorer.exe", "")
    e.win.windows[4]["shell"] = True
    e.win.add(5, "msedge.exe", "新的 InPrivate 窗口 - Edge")
    return e


def test_recorded_foreground_snaps_still_after_switch_in(env):
    env.win.fg = 1
    env.run(100)
    evs = env.events()
    assert [type(e) for e in evs] == [FocusEvent]
    assert (evs[0].hwnd, evs[0].app, evs[0].title) == (1, "msedge.exe", "文章 - Edge")
    env.show(1, img())
    env.run(1900)
    assert env.events(SnapEvent) == []
    env.run(300)
    snaps = env.events(SnapEvent)
    assert len(snaps) == 1 and snaps[0].reason == "still" and snaps[0].changed is None
    assert snaps[0].hwnd == 1 and not snaps[0].background and snaps[0].frame is not None
    env.run(5000)
    assert env.events(SnapEvent) == []  # 画面没变：不再拍


def test_unlisted_app_grey_with_app(env):
    env.win.fg = 2
    env.run(500)
    evs = env.events()
    assert len(evs) == 1 and isinstance(evs[0], GreyEvent)
    assert (evs[0].reason, evs[0].app, evs[0].end) == ("unlisted", "Code.exe", False)
    assert len(env.loop.pool) == 0
    env.win.fg = 1
    env.run(100)
    evs = env.events()
    assert isinstance(evs[0], GreyEvent) and evs[0].end and evs[0].app == "Code.exe"
    assert isinstance(evs[1], FocusEvent) and evs[1].hwnd == 1


def test_shell_and_private_leave_nothing(env):
    env.win.fg = 4
    env.run(500)
    env.win.fg = 5
    env.run(3000)
    assert env.events() == []
    assert len(env.loop.pool) == 0


def test_private_after_recorded_sends_focus_zero(env):
    env.win.fg = 1
    env.run(100)
    env.events()
    env.win.fg = 5
    env.run(100)
    evs = env.events()
    assert len(evs) == 1 and isinstance(evs[0], FocusEvent) and evs[0].hwnd == 0


def test_title_excluded_by_user_keyword(env):
    env.loop.set_excludes(["文章"])
    env.win.fg = 1
    env.run(500)
    assert env.events() == []


def test_set_tiers_starts_recording(env):
    env.win.fg = 2
    env.run(200)
    env.events()
    env.loop.set_tiers({"msedge.exe": "record", "code.exe": "record"})  # 页面上点了"以后录它"
    env.run(100)
    evs = env.events()
    assert isinstance(evs[0], GreyEvent) and evs[0].end
    assert isinstance(evs[1], FocusEvent) and evs[1].app == "Code.exe"
    assert 2 in env.loop.pool


def test_title_change_settles_before_focus(env):
    env.win.fg = 1
    env.run(100)
    env.events()
    env.win.windows[1]["title"] = "下载 42% - Edge"
    env.run(500)
    env.win.windows[1]["title"] = "下载 43% - Edge"
    env.run(500)
    assert env.events(FocusEvent) == []
    env.run(1200)
    evs = env.events(FocusEvent)
    assert len(evs) == 1 and evs[0].title == "下载 43% - Edge"


def test_always_tier_background_window(env):
    env.loop.set_tiers({"msedge.exe": "record", "zoom.exe": "always"})
    env.win.fg = 1
    env.run(100)
    assert 3 in env.loop.pool and 1 in env.loop.pool
    env.show(3, img(3))
    env.show(1, img(1))
    env.run(2200)
    snaps = env.events(SnapEvent)
    bg = [s for s in snaps if s.hwnd == 3]
    assert len(bg) == 1 and bg[0].background and bg[0].app == "zoom.exe"
    assert [s.hwnd for s in snaps if not s.background] == [1]


def test_background_capped(env, cfg):
    for h in (10, 11, 12):
        env.win.add(h, "zoom.exe", f"会议 {h}")
    env.loop.set_tiers({"zoom.exe": "always"})
    env.run(100)
    assert len(env.loop.pool) == cfg.max_background_windows


def test_pause_grey_and_no_snaps(env):
    env.win.fg = 1
    env.run(100)
    env.show(1, img())
    env.run(500)
    env.events()
    env.loop.set_paused(True)
    env.run(100)
    evs = env.events()
    assert isinstance(evs[0], FocusEvent) and evs[0].hwnd == 0
    assert isinstance(evs[1], GreyEvent) and evs[1].reason == "paused" and not evs[1].end
    assert len(env.loop.pool) == 0
    env.loop.on_copy("暂停时复制的", env.clock.t)
    env.run(5000)
    assert env.events() == []
    env.loop.set_paused(False)
    env.run(100)
    evs = env.events()
    assert isinstance(evs[0], GreyEvent) and evs[0].reason == "paused" and evs[0].end
    assert isinstance(evs[1], FocusEvent) and evs[1].hwnd == 1


def test_lock_grey(env):
    env.win.fg = 1
    env.run(100)
    env.events()
    env.win.is_locked = True
    env.run(600)
    evs = env.events()
    assert [type(e) for e in evs] == [FocusEvent, GreyEvent]
    assert evs[1].reason == "locked"
    env.win.is_locked = False
    env.run(600)
    evs = env.events(GreyEvent)
    assert len(evs) == 1 and evs[0].end


def test_copy_snaps_then_copy_event(env, cfg):
    env.win.fg = 1
    env.run(100)
    env.show(1, img())
    env.run(2200)
    env.events()
    env.loop.on_copy("x" * (cfg.clip_max_chars + 50), env.clock.t)
    env.run(100)
    evs = env.events()
    assert isinstance(evs[0], SnapEvent) and evs[0].reason == "copy"
    assert isinstance(evs[1], CopyEvent) and evs[1].hwnd == 1 and len(evs[1].text) == cfg.clip_max_chars


def test_copy_in_unlisted_app_dropped(env):
    env.win.fg = 2
    env.run(100)
    env.events()
    env.loop.on_copy("别的软件里复制的", env.clock.t)
    env.run(100)
    assert env.events() == []


def test_copy_written_by_another_process_dropped(env):
    """剪贴板属主是别的进程（后台的剪贴板历史工具等）：不算在前台窗口里复制的；属主就是前台进程时照记。"""
    env.win.fg = 1
    env.run(100)
    env.show(1, img())
    env.run(2200)
    env.events()
    env.loop.on_copy("后台程序写的", env.clock.t, owner_pid=99999)
    env.run(100)
    assert env.events(CopyEvent) == []
    env.loop.on_copy("前台复制的", env.clock.t, owner_pid=env.win.pid(1))
    env.run(100)
    assert [e.text for e in env.events(CopyEvent)] == ["前台复制的"]


def test_typing_defers_snap_per_page(env):
    env.win.fg = 1
    env.run(100)
    env.show(1, img())
    env.run(1000)
    env.inp.typing(env.clock.t)
    env.run(3000)
    assert env.events(SnapEvent) == []
    env.loop.on_copy("打字时复制", env.clock.t)
    env.run(100)
    evs = env.events()
    assert [type(e) for e in evs] == [CopyEvent]  # 复制照记，拍先不拍
    env.run(2000)
    snaps = env.events(SnapEvent)
    assert len(snaps) == 1 and snaps[0].reason == "still"


def test_away_event(env, cfg):
    env.win.fg = 1
    env.run(100)
    env.events()
    env.inp.idle = cfg.away_ms / 1000 + 10
    env.run(100)
    start = env.clock.t - cfg.away_ms - 10_000
    assert env.events(AwayEvent) == []
    env.run(60_000)
    env.inp.idle = 0.5
    env.run(100)
    evs = env.events(AwayEvent)
    assert len(evs) == 1
    assert evs[0].start_ms == start and evs[0].end_ms == env.clock.t - 500


def test_away_then_lock_reports_away_before_leaving(env, cfg):
    """走开 6 分钟后自动锁屏：先报走开（扣给锁屏前那条），再 FocusEvent(0)；解锁回来后接着报剩下的一段。"""
    env.win.fg = 1
    env.run(100)
    env.events()
    env.inp.idle = 6 * 60
    env.run(100)
    start = env.clock.t - 6 * 60_000
    env.win.is_locked = True
    env.run(600)
    lock_evs = env.events()
    away = [e for e in lock_evs if isinstance(e, AwayEvent)]
    focus = [i for i, e in enumerate(lock_evs) if isinstance(e, FocusEvent)]
    assert len(away) == 1 and away[0].start_ms == start
    assert lock_evs.index(away[0]) < focus[0] and lock_evs[focus[0]].hwnd == 0
    locked_at = away[0].end_ms
    env.win.is_locked = False
    env.inp.idle = 0.5
    env.run(600)
    rest = env.events(AwayEvent)
    assert len(rest) == 1 and rest[0].start_ms == locked_at


def test_shutdown_closes_everything(env):
    env.win.fg = 2
    env.run(100)
    env.events()
    env.loop.shutdown(env.clock.t)
    evs = env.events()
    assert len(evs) == 1 and isinstance(evs[0], GreyEvent) and evs[0].end


def test_session_restarted_resets_trigger(env):
    env.win.fg = 1
    env.run(100)
    env.show(1, img())
    env.run(2200)
    assert len(env.events(SnapEvent)) == 1
    env.win.fg = 2
    env.run(4000)  # 会话延迟 3 秒停
    assert 1 not in env.loop.pool
    env.win.fg = 1
    env.run(100)
    env.show(1, img())  # 新会话的第一帧（画面没变）
    env.run(2200)
    assert len(env.events(SnapEvent)) == 1


# ---------------- 剪贴板 ----------------

def test_clipboard_on_update_calls_back_text_only():
    from screenrecall.v3.clipboard import ClipboardWatcher

    got = []
    texts = iter(["复制的一句话", None, "   "])
    w = ClipboardWatcher(lambda text, t, owner_pid=None: got.append((text, t, owner_pid)), clock=lambda: 42,
                         reader=lambda h: next(texts), owner=lambda: 4321)
    w.on_update()
    w.on_update()
    w.on_update()
    assert got == [("复制的一句话", 42, 4321)] and w.updates == 3


def test_clipboard_listener_starts_and_stops():
    from screenrecall.v3.clipboard import ClipboardWatcher

    # 只建消息窗口、装监听；读剪贴板用假的（不碰真实剪贴板内容）
    w = ClipboardWatcher(lambda text, t, owner_pid=None: None, reader=lambda h: None)
    assert w.start()
    w.stop()
