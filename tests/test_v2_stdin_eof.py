"""stdin_eof：子进程等 stdin EOF 时不能挂着同步读（否则别的线程加载 numpy 的 DLL 会卡死）。"""
import subprocess
import sys
import time

import pytest

CHILD = r"""
import sys, threading, time
from screenrecall.v2 import stdin_eof
eof = threading.Event()
stdin_eof.watch(eof.set, poll_s=0.05)
time.sleep(0.3)  # 让看 stdin 的线程先跑起来
t = time.time()
import numpy  # noqa: F401 —— 加载带 CRT 的 DLL
print("imported", flush=True)
print("eof" if eof.wait(10) else "no-eof", flush=True)
"""


def _spawn():
    from screenrecall.app.supervisor import child_env

    return subprocess.Popen([sys.executable, "-c", CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=child_env())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 管道语义")
def test_dll_import_not_blocked_while_watching_open_pipe():
    p = _spawn()
    try:
        t0 = time.monotonic()
        line = p.stdout.readline().strip()  # 管道一直开着：导入不能被看 stdin 的线程卡住
        assert line == "imported", line
        assert time.monotonic() - t0 < 15
        p.stdin.close()  # 关管道 = 让它退出
        assert p.stdout.readline().strip() == "eof"
        assert p.wait(10) == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(5)


def test_watch_file_stdin_reads_to_eof(tmp_path):
    import threading

    from screenrecall.v2 import stdin_eof

    f = tmp_path / "in.txt"
    f.write_bytes(b"abc")
    done = threading.Event()
    with open(f, "rb") as fh:
        th = stdin_eof.watch(done.set, fh, poll_s=0.05)
        assert th is not None
        assert done.wait(5)


def test_watch_tty_or_missing_stdin_is_ignored():
    from screenrecall.v2 import stdin_eof

    class Tty:
        def isatty(self):
            return True

    assert stdin_eof.watch(lambda: None, Tty()) is None
