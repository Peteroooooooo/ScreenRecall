"""v2 主进程：子进程看护（作业对象、崩溃重启、优雅停止、主进程被强制结束时子进程一起结束）。

用临时目录里的小脚本当子进程，不涉及录制或任何屏幕内容。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")

from screenrecall.app import supervisor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def wait_for(cond, timeout=15.0, step=0.05):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(step)
    return False


def lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").split() if p.exists() else []


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def gone(pid: int, timeout: float = 10.0) -> bool:
    return wait_for(lambda: not alive(pid), timeout)


def test_crash_is_restarted_with_backoff(tmp_path):
    out = tmp_path / "runs.txt"
    s = script(tmp_path, "crash.py", f"""
        import os
        with open({str(out)!r}, "a") as f:
            f.write(str(os.getpid()) + "\\n")
        raise SystemExit(3)
    """)
    events = []
    sup = supervisor.Supervisor([supervisor.ChildSpec("c", [PY, str(s)], restart="on-failure",
                                                      backoff_initial_s=0.05, backoff_max_s=0.2)],
                                on_event=lambda n, e, i: events.append(e), poll_s=0.05).start()
    try:
        assert wait_for(lambda: len(lines(out)) >= 3)
        assert wait_for(lambda: sup.status()["c"]["restarts"] >= 2)
        assert sup.status()["c"]["last_exit"] == 3
    finally:
        sup.stop_all(grace_s=0.5)
    assert "exited" in events and "restart_scheduled" in events
    assert sup.status()["c"]["state"] == "stopped"


def test_clean_exit_with_on_failure_is_not_restarted(tmp_path):
    out = tmp_path / "runs.txt"
    s = script(tmp_path, "done.py", f"""
        open({str(out)!r}, "a").write("x\\n")
    """)
    sup = supervisor.Supervisor([supervisor.ChildSpec("w", [PY, str(s)], restart="on-failure",
                                                      backoff_initial_s=0.05, autostart=False)], poll_s=0.05).start()
    try:
        assert sup.status()["w"]["state"] == "stopped"  # autostart=False：不自动启动
        assert sup.start_child("w")
        assert wait_for(lambda: sup.status()["w"]["state"] == "exited")
        time.sleep(0.3)
        assert lines(out) == ["x"] and sup.status()["w"]["last_exit"] == 0
    finally:
        sup.stop_all(grace_s=0.5)


def test_stop_closes_stdin_for_graceful_exit(tmp_path):
    out = tmp_path / "bye.txt"
    s = script(tmp_path, "graceful.py", f"""
        import sys
        open({str(out)!r}, "w").write("ready")
        sys.stdin.read()            # 主进程关闭标准输入 = 该收尾了
        open({str(out)!r}, "w").write("bye")
    """)
    sup = supervisor.Supervisor([supervisor.ChildSpec("g", [PY, str(s)])], poll_s=0.05).start()
    try:
        assert wait_for(lambda: out.exists() and out.read_text() == "ready")
        assert sup.stop_child("g", grace_s=10) == 0
        assert out.read_text() == "bye"
    finally:
        sup.stop_all(grace_s=0.5)


def test_stop_kills_stubborn_child_and_its_grandchildren(tmp_path):
    pids = tmp_path / "pids.txt"
    s = script(tmp_path, "stubborn.py", f"""
        import os, subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{g.pid}}")
        time.sleep(120)
    """)
    sup = supervisor.Supervisor([supervisor.ChildSpec("s", [PY, str(s)])], poll_s=0.05).start()
    try:
        assert wait_for(lambda: len(lines(pids)) == 2)
        child, grand = map(int, lines(pids))
        t0 = time.monotonic()
        sup.stop_child("s", grace_s=0.3)
        assert time.monotonic() - t0 < 5
        assert gone(child) and gone(grand)
    finally:
        sup.stop_all(grace_s=0.3)


def test_below_normal_priority(tmp_path):
    out = tmp_path / "prio.txt"
    s = script(tmp_path, "prio.py", f"""
        import psutil, time
        open({str(out)!r}, "w").write(str(psutil.Process().nice()))
        time.sleep(60)
    """)
    sup = supervisor.Supervisor([supervisor.ChildSpec("p", [PY, str(s)], below_normal=True)], poll_s=0.05).start()
    try:
        assert wait_for(lambda: bool(lines(out)))
        assert int(lines(out)[0]) == psutil.BELOW_NORMAL_PRIORITY_CLASS
    finally:
        sup.stop_all(grace_s=0.2)


def test_start_failure_is_reported_and_retried(tmp_path):
    events = []
    sup = supervisor.Supervisor([supervisor.ChildSpec("x", [str(tmp_path / "missing.exe")], backoff_initial_s=0.05)],
                                on_event=lambda n, e, i: events.append(e), poll_s=0.05).start()
    try:
        assert wait_for(lambda: events.count("start_failed") >= 2)
    finally:
        sup.stop_all(grace_s=0.1)
    assert sup.status()["x"]["pid"] is None


def test_hard_killed_parent_takes_children_with_it(tmp_path):
    """主进程被强制结束（TerminateProcess，不走任何清理代码）→ 作业句柄被系统关闭 → 子进程被结束。"""
    pids = tmp_path / "pids.txt"
    child = script(tmp_path, "child.py", """
        import time
        time.sleep(120)
    """)
    parent = script(tmp_path, "parent.py", f"""
        import os, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from screenrecall.app import supervisor
        sup = supervisor.Supervisor([supervisor.ChildSpec("rec", [sys.executable, {str(child)!r}])]).start()
        time.sleep(0.2)
        open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{sup.pid('rec')}}")
        time.sleep(120)
    """)
    launcher = subprocess.Popen([PY, str(parent)])
    try:
        assert wait_for(lambda: len(lines(pids)) == 2)
        parent_pid, child_pid = map(int, lines(pids))
        # child_pid 可能是 venv 启动器，把它整棵进程树都记下来
        tree = [child_pid] + [p.pid for p in psutil.Process(child_pid).children(recursive=True)]
        assert all(alive(p) for p in tree)
        psutil.Process(parent_pid).kill()
        for pid in tree:
            assert gone(pid), pid
    finally:
        launcher.kill()
        launcher.wait(10)


def test_no_child_starts_after_stop_all(tmp_path):
    """关机途中晚到的空闲回调（start_child）不能再拉起子进程。"""
    out = tmp_path / "runs.txt"
    s = script(tmp_path, "w.py", f"""
        open({str(out)!r}, "a").write("x\\n")
    """)
    sup = supervisor.Supervisor([supervisor.ChildSpec("w", [PY, str(s)], autostart=False)], poll_s=0.05).start()
    sup.stop_all(grace_s=0.1)
    assert sup.start_child("w") is False
    time.sleep(0.3)
    assert not out.exists() and sup.status()["w"]["pid"] is None


def test_duplicate_names_rejected():
    with pytest.raises(ValueError):
        supervisor.Supervisor([supervisor.ChildSpec("a", ["x"]), supervisor.ChildSpec("a", ["y"])])


def test_helpers():
    cmd = supervisor.python_child("screenrecall.app", "--x", pythonw=True)
    assert cmd[1:] == ["-m", "screenrecall.app", "--x"]
    env = supervisor.child_env({"A": "1"})
    assert env["PYTHONPATH"].split(";")[0] == str(ROOT) and env["A"] == "1"
