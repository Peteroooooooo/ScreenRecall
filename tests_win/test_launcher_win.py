"""ScreenRecall.cmd 真的启动主进程：pythonw（没有控制台）、cmd 马上返回；"现在就整理"拉起空闲整理子进程；退出时全部收干净。

录制进程和空闲整理进程换成测试替身（不截屏，不碰用户自己的窗口）；临时 SCREENRECALL_HOME 和数据目录；
--no-browser（不打开用户的浏览器）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

from screenrecall.app import auth
from tests_win.test_v2_main_win import STUB_RECORDER, STUB_WORKER, alive, wait_for

ROOT = Path(__file__).resolve().parent.parent


def visible_console(pid: int) -> bool:
    """pid 的控制台有没有屏幕上看得见的窗口（在一个独立的小进程里 AttachConsole，不动测试进程自己的控制台）。"""
    code = ("import ctypes,sys;k=ctypes.windll.kernel32;u=ctypes.windll.user32;k.FreeConsole();"
            f"ok=k.AttachConsole({int(pid)});h=k.GetConsoleWindow() if ok else 0;"
            "import ctypes.wintypes as w;r=w.RECT();u.GetWindowRect(h,ctypes.byref(r)) if h else 0;"
            "print(1 if h and u.IsWindowVisible(h) and r.right-r.left>0 and r.bottom-r.top>0 else 0)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
    return out == "1"


@pytest.fixture
def stubs(tmp_path):
    d = tmp_path / "stubs"
    d.mkdir()
    (d / "sr_stub_recorder.py").write_text(STUB_RECORDER, encoding="utf-8")
    (d / "sr_stub_worker.py").write_text(STUB_WORKER, encoding="utf-8")
    return d


def test_launcher_starts_main_without_a_console_and_idle_run_now(tmp_path, stubs):
    home, data = tmp_path / "home", tmp_path / "data"
    pyw = Path(sys.executable).with_name("pythonw.exe")
    assert pyw.is_file()
    env = dict(os.environ, SCREENRECALL_HOME=str(home), SCREENRECALL_PYTHONW=str(pyw), PYTHONPATH=str(stubs))
    env.pop("SCREENRECALL_DATA_DIR", None)
    t0 = time.perf_counter()
    # 不接管道：start 出来的主进程会继承句柄，管道就一直关不上
    r = subprocess.run(["cmd.exe", "/c", str(ROOT / "ScreenRecall.cmd"), "--no-tray", "--no-browser", "--data-dir",
                        str(data), "--port", "0", "--recorder-module", "sr_stub_recorder",
                        "--worker-module", "sr_stub_worker"], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    t_cmd = time.perf_counter() - t0
    assert r.returncode == 0 and t_cmd < 10, (r.returncode, t_cmd)  # cmd 马上返回，主进程在后台
    ep = wait_for(lambda: (e := auth.read_endpoint(home)) is not None and e.pid is not None and e, 60)
    assert ep, "主进程没启动"
    main_pid = ep.pid
    worker_pid = None
    try:
        p = psutil.Process(main_pid)
        # venv 的 pythonw.exe 是启动器：真正的主进程是它的子进程。由 pythonw 启动 = 没有控制台窗口
        chain = [p.name().lower()] + [q.name().lower() for q in p.parents()[:1]]
        assert "pythonw.exe" in chain, chain
        assert not visible_console(main_pid)
        assert Path(p.cwd()).resolve() == ROOT.resolve()
        h = {**auth.bearer_headers(ep), "X-ScreenRecall": "1"}
        with httpx.Client(base_url=ep.base_url, trust_env=False, timeout=15) as c:
            # （这台电脑正好键鼠空闲时，空闲监视自己已经拉起了替身；"现在就整理"照样返回 running、不开第二个）
            s = c.post("/api/v2/idle/run", json={}, headers=h).json()
            assert s["idle_work"] == "running" and set(s["queue"]) == {"ocr", "vectors", "summaries"}
            kids = [k for k in p.children() if "sr_stub_worker" in " ".join(k.cmdline())]
            assert len(kids) == 1, kids  # 一个替身（venv 启动器 + 它的真进程算一个）
            worker_pid = kids[0].pid
            assert c.post("/api/app/quit", headers=h).status_code == 200
        assert wait_for(lambda: not alive(main_pid), 30), "主进程没退出"
        assert wait_for(lambda: not alive(worker_pid), 10), "空闲整理替身还在"
        assert auth.read_endpoint(home).pid is None
        print(f"\nlauncher returned in {t_cmd:.2f} s")
    finally:
        for pid in (worker_pid, main_pid):
            if pid and alive(pid):
                psutil.Process(pid).kill()
