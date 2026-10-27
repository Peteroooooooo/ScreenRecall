"""桌面和开始菜单里的 ScreenRecall 快捷方式。

    python -m screenrecall.app.shortcut create [--desktop-dir D] [--start-menu-dir D] [--home H] [--data-dir D]
    python -m screenrecall.app.shortcut remove [--desktop-dir D] [--start-menu-dir D]

快捷方式和开机自启（app.autostart）是同一个启动方式：venv 里的 pythonw.exe（没有控制台窗口）+ autostart.launch_argv，
只是不带 --autostart（双击时要打开主页）。已经在运行时再点一次只是打开主页。
图标用托盘图标画一个 .ico，放在应用目录里。快捷方式用 Windows 自带的 WScript.Shell 写（经 powershell.exe，
路径都通过环境变量传进去，不拼进命令文本）。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from . import autostart, paths

LINK_NAME = "ScreenRecall.lnk"
ICON_NAME = "ScreenRecall.ico"
DESCRIPTION = "ScreenRecall：屏幕记忆（只在本机）"
_CSIDL_DESKTOPDIRECTORY = 0x10
_CSIDL_PROGRAMS = 0x02

_PS_CREATE = r"""
$ErrorActionPreference = 'Stop'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SR_LNK)
$s.TargetPath = $env:SR_TARGET
$s.Arguments = $env:SR_ARGS
$s.WorkingDirectory = $env:SR_CWD
$s.Description = $env:SR_DESC
if ($env:SR_ICON) { $s.IconLocation = $env:SR_ICON }
$s.Save()
"""

_PS_READ = r"""
$ErrorActionPreference = 'Stop'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SR_LNK)
[Console]::OutputEncoding = [Text.Encoding]::UTF8
@{target = $s.TargetPath; args = $s.Arguments; cwd = $s.WorkingDirectory; icon = $s.IconLocation;
  desc = $s.Description} | ConvertTo-Json -Compress
"""


def known_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    hr = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    if hr != 0:
        raise OSError(f"找不到系统文件夹（{hr:#x}）")
    return Path(buf.value)


def default_dirs() -> tuple[Path, Path]:
    """（桌面, 开始菜单\\程序）。桌面被 OneDrive 重定向时也是实际位置。"""
    return known_folder(_CSIDL_DESKTOPDIRECTORY), known_folder(_CSIDL_PROGRAMS)


def _powershell(script: str, env: dict[str, str]) -> str:
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    r = subprocess.run([str(exe), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, **env}, capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OSError(f"写快捷方式失败：{err[-1] if err else r.returncode}")
    return r.stdout.decode("utf-8", "replace")


def write_icon(home: Path) -> Path | None:
    """托盘图标存成 .ico（多种尺寸）；画不出来就不要图标（用 pythonw 的）。"""
    try:
        from .tray import draw_icon

        p = Path(home) / ICON_NAME
        paths.ensure_private_dir(home)
        draw_icon(256).save(p, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        return p
    except Exception:  # noqa: BLE001
        return None


def link_spec(*, home: Path | None = None, data_dir: Path | None = None, python: str | None = None) -> dict[str, str]:
    """快捷方式的目标、参数、起始位置。应用目录 / 数据目录不是默认位置时把 --home / --data-dir 带上。"""
    home = Path(home) if home else paths.app_home()
    extra = autostart.location_args(home, Path(data_dir) if data_dir else home / "data")
    argv = autostart.launch_argv(python, autostart.REPO_ROOT, extra)
    return {"target": argv[0], "args": subprocess.list2cmdline(argv[1:]), "cwd": str(autostart.REPO_ROOT)}


def create(*, desktop_dir: Path | None = None, start_menu_dir: Path | None = None, home: Path | None = None,
           data_dir: Path | None = None, python: str | None = None, icon: bool = True) -> list[Path]:
    """在桌面和开始菜单各放一个（已有的覆盖）。返回写好的快捷方式路径。"""
    if desktop_dir is None or start_menu_dir is None:
        d, s = default_dirs()
        desktop_dir, start_menu_dir = desktop_dir or d, start_menu_dir or s
    spec = link_spec(home=home, data_dir=data_dir, python=python)
    ico = write_icon(Path(home) if home else paths.app_home()) if icon else None
    out = []
    for folder in (Path(desktop_dir), Path(start_menu_dir)):
        folder.mkdir(parents=True, exist_ok=True)
        lnk = folder / LINK_NAME
        _powershell(_PS_CREATE, {"SR_LNK": str(lnk), "SR_TARGET": spec["target"], "SR_ARGS": spec["args"],
                                 "SR_CWD": spec["cwd"], "SR_DESC": DESCRIPTION, "SR_ICON": str(ico) if ico else ""})
        out.append(lnk)
    return out


def remove(*, desktop_dir: Path | None = None, start_menu_dir: Path | None = None) -> list[Path]:
    """删掉桌面和开始菜单里的快捷方式（只删 ScreenRecall.lnk）。返回删掉的路径。"""
    if desktop_dir is None or start_menu_dir is None:
        d, s = default_dirs()
        desktop_dir, start_menu_dir = desktop_dir or d, start_menu_dir or s
    out = []
    for folder in (Path(desktop_dir), Path(start_menu_dir)):
        lnk = folder / LINK_NAME
        if lnk.is_file():
            lnk.unlink()
            out.append(lnk)
    return out


def read(lnk: Path) -> dict[str, str]:
    """读回一个快捷方式（测试和排错用）。"""
    import json

    return json.loads(_powershell(_PS_READ, {"SR_LNK": str(lnk)}))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.app.shortcut", description="桌面 / 开始菜单快捷方式")
    ap.add_argument("action", choices=("create", "remove"))
    ap.add_argument("--desktop-dir", type=Path, help="放快捷方式的桌面文件夹（默认当前用户的桌面）")
    ap.add_argument("--start-menu-dir", type=Path, help="开始菜单\\程序 文件夹（默认当前用户的）")
    ap.add_argument("--home", type=Path, help="应用目录不是默认位置时给出（快捷方式会带上 --home）")
    ap.add_argument("--data-dir", type=Path, help="数据目录不是默认位置时给出（快捷方式会带上 --data-dir）")
    a = ap.parse_args(argv)
    try:
        if a.action == "create":
            done = create(desktop_dir=a.desktop_dir, start_menu_dir=a.start_menu_dir, home=a.home, data_dir=a.data_dir)
            print("已创建快捷方式：\n" + "\n".join(f"  {p}" for p in done))
        else:
            done = remove(desktop_dir=a.desktop_dir, start_menu_dir=a.start_menu_dir)
            print("已删除快捷方式：\n" + "\n".join(f"  {p}" for p in done) if done else "没有要删除的快捷方式。")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
