"""v2 主进程：空闲检测（注入的空闲秒数 / 锁屏函数）。"""
from __future__ import annotations

import sys
import time

import pytest

from screenrecall.app import idle


class Fake:
    def __init__(self):
        self.idle_s = 0.0
        self.locked = False
        self.events = []


def watcher(f: Fake, **kw):
    return idle.IdleWatcher(300, idle_fn=lambda: f.idle_s, locked_fn=lambda: f.locked,
                            on_idle_start=lambda r: f.events.append(("start", r)),
                            on_idle_end=lambda: f.events.append(("end",)), **kw)


def test_idle_start_and_end_fire_once():
    f = Fake()
    w = watcher(f)
    assert not w.poll()
    f.idle_s = 299
    assert not w.poll()
    f.idle_s = 300
    assert w.poll() and w.poll()
    f.idle_s = 1
    assert not w.poll() and not w.poll()
    assert f.events == [("start", "input"), ("end",)]


def test_locked_counts_as_idle_and_updates_reason():
    f = Fake()
    w = watcher(f)
    f.locked = True
    w.poll()
    assert w.reason == "locked"
    f.locked, f.idle_s = False, 400
    w.poll()
    assert w.reason == "input" and f.events == [("start", "locked")]


def test_query_errors_and_callback_errors_do_not_break():
    f = Fake()

    def boom():
        raise OSError("nope")

    w = idle.IdleWatcher(300, idle_fn=boom, locked_fn=lambda: False)
    assert w.check() == (False, None)
    w = idle.IdleWatcher(300, idle_fn=lambda: 999, locked_fn=lambda: False,
                         on_idle_start=lambda r: 1 / 0)
    assert w.poll() and w.idle


def test_thread_runs_and_stops():
    f = Fake()
    f.idle_s = 1000
    w = watcher(f, interval_s=0.01).start()
    t0 = time.monotonic()
    while not f.events and time.monotonic() - t0 < 2:
        time.sleep(0.01)
    w.stop()
    assert f.events == [("start", "input")]


def test_return_from_idle_is_noticed_quickly():
    """空闲期间按 idle_interval_s 查：用户回来后很快触发 on_idle_end（停掉占显卡的空闲整理进程）。"""
    f = Fake()
    f.idle_s = 1000
    w = watcher(f, interval_s=30, idle_interval_s=0.02).start()
    try:
        t0 = time.monotonic()
        while not f.events and time.monotonic() - t0 < 2:
            time.sleep(0.01)
        f.idle_s = 0
        t1 = time.monotonic()
        while len(f.events) < 2 and time.monotonic() - t1 < 2:
            time.sleep(0.01)
        assert f.events == [("start", "input"), ("end",)]
        assert time.monotonic() - t1 < 1.0
    finally:
        w.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_real_api_values_are_sane():
    s = idle.idle_seconds()
    assert 0 <= s < 50 * 24 * 3600
    assert isinstance(idle.is_locked(), bool)
