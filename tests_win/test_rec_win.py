"""v2 录制核心在真实 Windows 上：WGC 会话、PrintWindow 兜底、窗口状态、输入监测、整套 RecorderCore。

只截、只操作本测试自己开的 tkinter 窗口；输出只有计数和数字。
输入监测的检查会往自己的窗口上发一次滚轮（先把光标移过去，结束后放回原处）和一次 F24 键。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import pytest

from _tkdoc import TkDoc
from screenrecall.rec import win32
from screenrecall.rec.clock import now_ms
from screenrecall.rec.core import RecorderConfig, RecorderCore
from screenrecall.rec.inputmon import FakeInput, InputMonitor
from screenrecall.rec.tracker import ListSink, TrackerConfig, WindowTracker
from screenrecall.rec.wgc import CaptureSession, PrintWindowSource
from screenrecall.rec.windows import ForegroundWatcher, RealWindows


@pytest.fixture(scope="module")
def _shared_doc():
    # 整个文件共用一个 Tk 窗口（省时间）。线程退出 / Tk 根窗口销毁导致 MTA 被拆、windows-capture 崩掉的问题
    # 由 wgc.keep_mta_alive() 解决，test_wgc_survives_thread_and_tk_churn 专门核对
    d = TkDoc()
    yield d
    d.close()


@pytest.fixture
def doc(_shared_doc):
    d = _shared_doc
    d.root.title("sr-rec-tk-check")
    d.root.deiconify()
    d.cv.yview_moveto(0)
    d.pump(0.5)
    return d


def _drive(doc: TkDoc, src, tracker: WindowTracker, seconds: float, log: list | None = None) -> None:
    def each():
        now = now_ms()
        f = src.take(now)
        if f is not None:
            kind = tracker.feed(f)
            if log is not None:
                log.append((kind, tracker.last_motion.dy if kind in ("shift", "inplace") else None, f))
        tracker.tick(now)

    doc.pump(seconds, each)


def test_wgc_session_first_frame_and_bounds(doc):
    src = CaptureSession(doc.hwnd, max_fps=10)
    t0 = now_ms()
    src.start()
    try:
        deadline = time.perf_counter() + 2
        f = None
        while f is None and time.perf_counter() < deadline:
            doc.pump(0.02)
            f = src.take()
        assert f is not None, "WGC 两秒内没有送第一帧"
        first_ms = src.first_frame_ms - t0
        bounds = win32.frame_bounds(doc.hwnd)
        bw, bh = bounds[2] - bounds[0], bounds[3] - bounds[1]
        h, w = f.shape
        print(f"\n第一帧 {first_ms} ms；帧 {w}x{h}；可见边框 {bw}x{bh}；换算比例 {f.fmap.scale_x},{f.fmap.scale_y}")
        assert abs(w - bw) <= 2 and abs(h - bh) <= 2
        assert f.fmap.left == bounds[0] and f.fmap.top == bounds[1]
        # 画面不变时 WGC 不送帧
        n = src.frames_seen
        doc.pump(1.5)
        assert src.frames_seen - n <= 1
    finally:
        src.stop()


def test_wgc_tracker_dy_matches_truth(doc):
    sink = ListSink()
    tracker = WindowTracker(doc.hwnd, sink, TrackerConfig())
    src = CaptureSession(doc.hwnd, max_fps=10)
    src.start()
    log: list = []
    try:
        _drive(doc, src, tracker, 1.6, log)
        assert len(sink.of("still")) == 1
        steps = [37, 90, 141, 90, 7, 200, 123, 61]
        truth = []
        for d in steps:
            before = doc.top()
            doc.scroll(d)
            truth.append(doc.top() - before)
            _drive(doc, src, tracker, 0.3, log)
        _drive(doc, src, tracker, 1.4, log)
        shifts = [dy for kind, dy, _ in log if kind == "shift"]
        print(f"\n收到帧 {src.frames_seen}，喂给跟踪器 {tracker.frames}，丢掉 {tracker.dropped}；"
              f"真实每步 {truth}；估出 {shifts}；静止 {len(sink.of('still'))}；证据 {len(sink.of('evidence'))}")
        assert shifts == truth
        assert tracker.pos_y == sum(truth)
        assert len(sink.of("still")) == 2
        assert [e["reason"] for e in sink.of("evidence")] == ["leaving"]
        assert len(sink.of("skim")) == 1 and sink.of("skim")[0]["y1"] == sum(truth)
    finally:
        src.stop()


def test_wgc_fast_scroll_at_10fps(doc):
    """每 50 ms 滚 60 像素（1200 px/s）：10 帧/秒限速下相邻帧差 ~120 px，全部接得上，累加等于真实距离。"""
    sink = ListSink()
    tracker = WindowTracker(doc.hwnd, sink, TrackerConfig())
    src = CaptureSession(doc.hwnd, max_fps=10)
    src.start()
    log: list = []
    try:
        _drive(doc, src, tracker, 1.2, log)
        y0 = doc.top()
        for _ in range(40):
            doc.scroll(60)
            _drive(doc, src, tracker, 0.05, log)
        _drive(doc, src, tracker, 1.3, log)
        dist = doc.top() - y0
        kinds = [k for k, _, _ in log]
        print(f"\n滚动 {dist} px：收到 {src.frames_seen} 帧，平移 {kinds.count('shift')}，接不上 {kinds.count('break')}，"
              f"跟踪位置 {tracker.pos_y}")
        assert kinds.count("break") == 0
        assert tracker.pos_y == dist
        assert src.frames_taken <= 2 + 10 * 4.5  # 限速：约 4 秒最多 10 帧/秒
    finally:
        src.stop()


def test_printwindow_source(doc):
    src = PrintWindowSource(doc.hwnd, max_fps=5)
    src.start()
    try:
        f = None
        deadline = time.perf_counter() + 2
        while f is None and time.perf_counter() < deadline:
            doc.pump(0.05)
            f = src.take()
        assert f is not None and f.source == "printwindow"
        bounds = win32.frame_bounds(doc.hwnd)
        assert f.shape == (bounds[3] - bounds[1], bounds[2] - bounds[0])
        n = src.frames_seen
        doc.pump(0.8)
        assert src.frames_seen == n  # 画面不变不送
        doc.scroll(50)
        doc.pump(0.6)
        assert src.frames_seen > n
    finally:
        src.stop()


def test_real_window_state_and_watcher(doc):
    rw = RealWindows()
    st = rw.state(doc.hwnd)
    assert st.exists and not st.minimized and st.gap_reason is None
    proc, title = rw.describe(doc.hwnd)
    assert proc.lower().startswith("python") and title == "sr-rec-tk-check"
    assert not rw.is_shell(doc.hwnd)
    assert rw.locked() is False
    w = ForegroundWatcher()
    assert w.start()
    try:
        doc.root.title("sr-rec-tk-check 改名")
        doc.pump(0.4)
        assert doc.hwnd in w.drain()
        doc.root.iconify()
        doc.pump(0.6)
        assert rw.state(doc.hwnd).gap_reason == "minimized"
        doc.root.deiconify()
        doc.pump(0.6)
        assert rw.state(doc.hwnd).gap_reason is None
    finally:
        w.stop()
    assert not rw.state(0x7FFF0).exists


# ---------- 输入监测 ----------

class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTU(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTU)]


def _send(*inputs: _INPUT) -> int:
    arr = (_INPUT * len(inputs))(*inputs)
    return ctypes.windll.user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _wheel_input(delta: int) -> _INPUT:
    i = _INPUT(type=0)
    i.u.mi = _MOUSEINPUT(0, 0, ctypes.c_uint32(delta).value, 0x0800, 0, 0)  # MOUSEEVENTF_WHEEL
    return i


def _key_input(vk: int, up: bool) -> _INPUT:
    i = _INPUT(type=1)
    i.u.ki = _KEYBDINPUT(vk, 0, 0x0002 if up else 0, 0, 0)
    return i


@pytest.mark.parametrize("hook", [False, True], ids=["raw_input", "ll_hook"])
def test_input_monitor_wheel_and_key(doc, hook):
    mon = InputMonitor(use_mouse_hook=hook)
    assert mon.start(), mon.error
    old = win32.cursor_pos()
    try:
        b = win32.frame_bounds(doc.hwnd)
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        ctypes.windll.user32.SetCursorPos(cx, cy)
        doc.pump(0.1)
        assert win32.root_window_at(cx, cy) == doc.hwnd
        top0 = doc.top()
        assert _send(_wheel_input(-120)) == 1
        doc.pump(0.4)
        assert _send(_key_input(0x87, False), _key_input(0x87, True)) == 2  # F24：几乎没有程序响应它
        doc.pump(0.3)
        evs = mon.drain_wheel()
        print(f"\n滚轮事件 {len(evs)}，按键次数 {mon.key_downs}，画布从 {top0} 滚到 {doc.top()}")
        assert evs and evs[0].delta == -120 and abs(evs[0].x - cx) <= 2 and abs(evs[0].y - cy) <= 2
        assert mon.key_downs >= 1 and mon.last_key_ms is not None
        assert 0 <= InputMonitor.idle_seconds() < 5
    finally:
        if old:
            ctypes.windll.user32.SetCursorPos(*old)
        mon.stop()


_CHURN = r"""
import subprocess, sys, threading, time
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from screenrecall import winapi
winapi.enable_dpi_awareness()
import screenrecall.rec.wgc as W
if sys.argv[3] == "nomta":
    W.keep_mta_alive = lambda: False
from screenrecall.rec.tracker import ListSink, WindowTracker

WIN = (
    "import sys\n"
    "sys.path[:0] = [sys.argv[3], sys.argv[4]]\n"
    "from _tkdoc import TkDoc\n"
    "d = TkDoc(title=sys.argv[1], lines=200)\n"
    "d.pump(float(sys.argv[2]))\n"
    "d.close()\n"
)


def find(title):
    for _ in range(150):
        for w in winapi.list_windows():
            if w.title == title:
                return w.hwnd
        time.sleep(0.1)


def run(h, box):
    s = W.CaptureSession(h, max_fps=10)
    s.start()
    tr = WindowTracker(h, ListSink())
    n = 0
    while not s.closed:
        f = s.take()
        if f is not None:
            tr.feed(f)
            f.bgr  # 用一次 OpenCV
            n += 1
        time.sleep(0.04)
    s.stop()
    box.append(n > 0)


ok = 0
for i in range(3):  # 每次：别的进程开一个窗口，新线程开会话、用 OpenCV，窗口关掉，线程退出
    title = f"sr-rec-churn-{i}"
    p = subprocess.Popen([sys.executable, "-c", WIN, title, "1.5", sys.argv[1], sys.argv[2]])
    box = []
    t = threading.Thread(target=run, args=(find(title), box))
    t.start()
    p.wait()
    t.join(10)
    ok += bool(box and box[0])
print("OK", ok)
"""


def _churn(mode: str):
    import subprocess
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    return subprocess.run([sys.executable, "-c", _CHURN, str(here), str(here.parent), mode], capture_output=True,
                          text=True, timeout=120)


def test_wgc_survives_thread_and_tk_churn():
    """开过会话的线程退出后再开会话：不占住 MTA 时 windows-capture 2.0.1 会访问冲突崩掉整个进程。
    在子进程里跑，两种都跑：不占 MTA 的那次用来确认这个测试确实测到了问题。"""
    r = _churn("mta")
    assert r.returncode == 0 and "OK 3" in r.stdout, f"退出码 {r.returncode}"
    bad = _churn("nomta")
    print(f"\n占住 MTA：退出码 {r.returncode}；不占 MTA：退出码 {bad.returncode}")
    assert bad.returncode != 0


_CHILD = r"""
import sys
sys.path[:0] = [sys.argv[3], sys.argv[4]]
from _tkdoc import TkDoc
d = TkDoc(title=sys.argv[1], lines=300)
d.pump(float(sys.argv[2]))
d.close()
"""


def _child_window(title: str, seconds: float):
    import subprocess
    import sys
    from pathlib import Path

    from screenrecall import winapi

    here = Path(__file__).resolve().parent
    p = subprocess.Popen([sys.executable, "-c", _CHILD, title, str(seconds), str(here), str(here.parent)])
    for _ in range(150):
        for w in winapi.list_windows():
            if w.title == title:  # venv 的 python.exe 是转发器，窗口属于它起的子进程，pid 对不上
                return p, w.hwnd
        time.sleep(0.1)
    p.kill()
    raise RuntimeError("子进程窗口没出来")


def test_child_process_window_closed():
    """截另一个进程（本测试起的）的窗口；窗口关掉后 WGC 报 closed，录制核心记 closed 缺口；再开一个照样能截。"""
    for i in range(2):
        p, hwnd = _child_window(f"sr-rec-child-{i}", 2.5)
        sink = ListSink()
        core = RecorderCore(sink, RecorderConfig(), windows=_OnlyMine(hwnd), inputs=FakeInput(), watcher=None)
        core.start()
        try:
            p.wait(10)
            time.sleep(0.6)
        finally:
            core.stop()
        names = sink.names()
        print(f"\n子窗口 {i}：事件 { {n: names.count(n) for n in set(names)} }")
        assert "block_started" in names and "still" in names
        assert any(g["reason"] == "closed" for g in sink.of("gap"))
        assert hwnd not in core.trackers


# ---------- 整套 RecorderCore ----------

class _OnlyMine(RealWindows):
    """前台永远当成测试窗口，绝不去截用户自己的窗口。"""

    def __init__(self, hwnd: int):
        super().__init__()
        self.mine = hwnd

    def foreground(self) -> int:
        return self.mine

    def root_at(self, x: int, y: int) -> int:
        return self.mine


def test_recorder_core_end_to_end(doc):
    sink = ListSink()
    inp = FakeInput()
    core = RecorderCore(sink, RecorderConfig(), windows=_OnlyMine(doc.hwnd), inputs=inp, watcher=None)
    core.start()
    try:
        doc.pump(1.6)
        for _ in range(6):
            inp.wheel(now_ms(), -120)
            doc.scroll(150)
            doc.pump(0.15)
        doc.pump(1.5)
        doc.root.iconify()
        doc.pump(0.4)
        doc.root.deiconify()
        doc.pump(1.6)
    finally:
        core.stop()
    st = core.stats()
    names = sink.names()
    print(f"\n事件：{ {n: names.count(n) for n in set(names)} }；喂帧 {core.frames_fed}；轮数 {core.steps}")
    assert core.pool.opened >= 1 and core.pool.fallbacks == 0
    assert names.count("block_started") == 1
    assert names.count("still") >= 2
    assert "leaving" in [e["reason"] for e in sink.of("evidence")]
    gaps = sink.of("gap")
    assert any(g["reason"] == "minimized" and g["t1_ms"] for g in gaps)
    assert gaps[-1]["reason"] == "stopped"
    assert st["paused"] is False
