"""真实主进程：python -m screenrecall.app（临时 app_home 和数据目录、无托盘、不开浏览器）。

检查：目录权限、endpoint.json、Bearer / 一次性链接登录、第二个实例、退出接口、强制结束主进程时子进程一起结束。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import psutil
import pytest

from screenrecall.app import auth, paths

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def wait_for(cond, timeout=30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if cond():
                return True
        except (OSError, ValueError, httpx.HTTPError):
            pass
        time.sleep(0.1)
    return False


def env_for(home: Path) -> dict[str, str]:
    return dict(os.environ, SCREENRECALL_HOME=str(home), PYTHONPATH=str(ROOT))


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.fixture
def running(tmp_path):
    home = tmp_path / "home"
    args = [PY, "-m", "screenrecall.app", "--no-tray", "--no-browser", "--port", "0", "--fake-queries"]
    p = subprocess.Popen(args, cwd=tmp_path, env=env_for(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert wait_for(lambda: (ep := auth.read_endpoint(home)) is not None and ep.pid is not None), "主进程没启动"
        yield home, auth.read_endpoint(home), p
    finally:
        if p.poll() is None:
            p.kill()
        p.wait(10)
        if p.stdout:
            p.stdout.close()


def test_main_process_end_to_end(running, tmp_path):
    home, ep, proc = running
    t_ready = time.monotonic()
    assert paths.verify_private(home) == [] and paths.verify_private(home / "data") == []
    assert paths.verify_private(home / "endpoint.json") == []
    base = ep.base_url
    with httpx.Client(base_url=base, trust_env=False, timeout=10, follow_redirects=False) as c:
        assert c.get("/api/meta").status_code == 401
        r = c.get("/api/meta", headers=auth.bearer_headers(ep))
        assert r.status_code == 200 and r.json()["db_ok"] is False
        url = c.post("/api/app/nonce", headers=auth.bearer_headers(ep)).json()["url"]
        r = c.get(url.removeprefix(base))
        assert r.status_code == 303
        assert c.get("/api/meta").status_code == 200  # 已带 Cookie
        assert c.get("/api/meta", headers={"Host": f"evil.example:{ep.port}"}).status_code == 403

    # 第二个实例：不另起服务，只是确认已在运行后退出
    r = subprocess.run([PY, "-m", "screenrecall.app", "--no-tray", "--no-browser", "--port", "0"], cwd=tmp_path,
                       env=env_for(home), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0 and "已经在运行" in r.stdout
    assert auth.read_endpoint(home).pid == ep.pid

    r = httpx.post(f"{base}/api/app/quit", headers=auth.bearer_headers(ep), trust_env=False, timeout=10)
    assert r.status_code == 200
    assert proc.wait(20) == 0
    assert auth.read_endpoint(home).pid is None and auth.read_endpoint(home).token == ep.token
    print(f"\n启动到可用 + 检查用时 {time.monotonic() - t_ready:.2f} 秒")


def test_hard_kill_of_main_stops_children(tmp_path):
    home = tmp_path / "home"
    pid_file = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent(f"""
        import os, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        time.sleep(120)
    """), encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(f"""
        import sys
        from pathlib import Path
        from screenrecall.app import main, supervisor
        opts = main.AppOptions(home=Path({str(home)!r}), port=0, tray=False, open_browser=False, legacy_web=False,
                               children=[supervisor.ChildSpec("recorder", [sys.executable, {str(child)!r}])])
        raise SystemExit(main.MainApp(opts).run())
    """), encoding="utf-8")
    p = subprocess.Popen([PY, str(runner)], cwd=tmp_path, env=env_for(home))
    try:
        assert wait_for(lambda: (ep := auth.read_endpoint(home)) is not None and ep.pid and pid_file.exists()
                        and pid_file.read_text().strip())
        main_pid, child_pid = auth.read_endpoint(home).pid, int(pid_file.read_text())
        assert alive(child_pid)
        psutil.Process(main_pid).kill()  # 不走任何清理代码
        assert wait_for(lambda: not alive(child_pid), 10), "主进程被强制结束后录制子进程还在"
    finally:
        p.kill()
        p.wait(10)
