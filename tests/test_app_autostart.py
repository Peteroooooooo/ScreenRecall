"""v2 主进程：开机自启（HKCU Run，用临时值名，测试后删除）。"""
from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")

from screenrecall.app import autostart  # noqa: E402


@pytest.fixture
def name():
    n = f"ScreenRecallTest-{uuid.uuid4().hex[:8]}"
    yield n
    autostart.disable(n)
    assert not autostart.is_enabled(n)


def test_enable_disable_roundtrip(name):
    assert not autostart.is_enabled(name)
    cmd = autostart.enable(name=name)
    assert autostart.is_enabled(name) and autostart.current_command(name) == cmd
    assert "pythonw.exe" in cmd.lower() and "screenrecall.app" in cmd and "--autostart" in cmd
    autostart.enable("custom.exe --x", name=name)
    assert autostart.current_command(name) == "custom.exe --x"
    assert autostart.disable(name) and not autostart.disable(name)


def test_default_command_runs_the_package_from_any_cwd(tmp_path):
    """自启命令里的 -c 代码能在任意工作目录下找到仓库（用 python.exe + --help 验证，不真的启动主进程）。"""
    cmd = autostart.default_command(python=sys.executable, args=("--help",))
    console = cmd.replace("pythonw.exe", "python.exe", 1)
    r = subprocess.run(console, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    assert "ScreenRecall v2" in r.stdout


def test_quote_in_root_rejected():
    with pytest.raises(ValueError):
        autostart.default_command(root=Path("C:/it's"))
