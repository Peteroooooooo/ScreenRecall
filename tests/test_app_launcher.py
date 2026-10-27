"""启动方式：开机自启、快捷方式、ScreenRecall.cmd 用同一个命令。快捷方式只写到临时文件夹（不碰真的桌面）。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from screenrecall.app import autostart, shortcut

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows 快捷方式")


def test_autostart_and_shortcut_share_the_launch_command(tmp_path):
    argv = autostart.launch_argv(args=("--x",))
    assert Path(argv[0]).name.lower() in ("pythonw.exe", "python.exe") and argv[1] == "-c" and argv[-1] == "--x"
    assert "runpy.run_module('screenrecall.app'" in argv[2] and repr(str(autostart.REPO_ROOT)) in argv[2]
    assert autostart.default_command() == subprocess.list2cmdline(autostart.launch_argv(args=("--autostart",)))
    spec = shortcut.link_spec(home=tmp_path / "h")
    assert spec["target"] == argv[0] and spec["cwd"] == str(autostart.REPO_ROOT)
    assert spec["args"] == subprocess.list2cmdline([argv[1], argv[2], "--home", str(tmp_path / "h")])
    # 默认位置不带参数
    import os

    default_home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ScreenRecall"
    assert autostart.location_args(default_home, default_home / "data") == ()
    assert autostart.location_args(default_home, tmp_path / "d") == ("--data-dir", str(tmp_path / "d"))


def test_create_and_remove_shortcuts_in_temp_folders(tmp_path):
    desk, menu, home = tmp_path / "Desktop", tmp_path / "Programs", tmp_path / "home"
    made = shortcut.create(desktop_dir=desk, start_menu_dir=menu, home=home)
    assert made == [desk / shortcut.LINK_NAME, menu / shortcut.LINK_NAME] and all(p.is_file() for p in made)
    info = shortcut.read(made[0])
    spec = shortcut.link_spec(home=home)
    assert Path(info["target"]).resolve() == Path(spec["target"]).resolve()
    assert info["args"] == spec["args"] and "--autostart" not in info["args"]  # 双击时打开主页
    assert Path(info["cwd"]).resolve() == autostart.REPO_ROOT.resolve()
    assert info["icon"].split(",")[0].lower() == str(home / shortcut.ICON_NAME).lower()
    assert (home / shortcut.ICON_NAME).is_file()
    # 再建一次覆盖；删掉两处，再删没有可删的
    shortcut.create(desktop_dir=desk, start_menu_dir=menu, home=home, icon=False)
    assert shortcut.remove(desktop_dir=desk, start_menu_dir=menu) == made
    assert not any(p.exists() for p in made)
    assert shortcut.remove(desktop_dir=desk, start_menu_dir=menu) == []


def test_shortcut_cli(tmp_path, capsys):
    desk, menu = tmp_path / "D", tmp_path / "M"
    args = ["--desktop-dir", str(desk), "--start-menu-dir", str(menu), "--home", str(tmp_path / "h")]
    assert shortcut.main(["create", *args]) == 0
    assert (desk / shortcut.LINK_NAME).is_file()
    assert shortcut.main(["remove", "--desktop-dir", str(desk), "--start-menu-dir", str(menu)]) == 0
    assert not (desk / shortcut.LINK_NAME).exists()
    assert "已删除" in capsys.readouterr().out


def test_launcher_cmd_uses_the_venv_pythonw_without_a_console():
    text = (autostart.REPO_ROOT / "ScreenRecall.cmd").read_text(encoding="ascii")
    assert r".venv\Scripts\pythonw.exe" in text and "-m screenrecall.app %*" in text
    assert 'start "" /D' in text  # start：cmd 窗口马上关掉，主进程没有控制台
    assert "\r\n" in (autostart.REPO_ROOT / "ScreenRecall.cmd").read_bytes().decode("ascii")
