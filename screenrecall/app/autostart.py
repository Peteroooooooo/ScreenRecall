"""开机自启：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run 下的一个值（只影响当前用户，不需要管理员）。

默认命令用 pythonw.exe（没有控制台窗口）启动主进程。仓库没有装进 site-packages，开机时的工作目录也不是仓库，
所以命令里用 -c 先把仓库根目录加进 sys.path 再按 -m screenrecall.app 的方式运行；以后做成安装包时直接传 command。
"""
from __future__ import annotations

import subprocess
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ScreenRecall"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def pythonw_path(python: str | None = None) -> str:
    """当前解释器旁边的 pythonw.exe（venv 的 Scripts 目录里也有）；没有就用原解释器。"""
    exe = Path(python or sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def default_command(python: str | None = None, root: Path = REPO_ROOT, args: tuple[str, ...] = ("--autostart",)) -> str:
    """开机时运行的完整命令行（已按 Windows 规则加引号）。"""
    root_s = str(root)
    if "'" in root_s or '"' in root_s:
        raise ValueError("仓库路径含引号，无法生成自启命令，请直接传 command")
    code = (f"import sys,runpy;sys.path.insert(0,{root_s!r});"
            "runpy.run_module('screenrecall.app',run_name='__main__',alter_sys=True)")
    return subprocess.list2cmdline([pythonw_path(python), "-c", code, *args])


def enable(command: str | None = None, name: str = VALUE_NAME) -> str:
    """写入（或覆盖）自启项，返回写入的命令。"""
    cmd = command or default_command()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
    return cmd


def disable(name: str = VALUE_NAME) -> bool:
    """删除自启项。返回原来是否存在。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except FileNotFoundError:
        return False


def current_command(name: str = VALUE_NAME) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as k:
            val, _ = winreg.QueryValueEx(k, name)
            return str(val)
    except FileNotFoundError:
        return None


def is_enabled(name: str = VALUE_NAME) -> bool:
    return current_command(name) is not None
