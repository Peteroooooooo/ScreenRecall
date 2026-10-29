"""把 ScreenRecall 的 MCP 转发程序登记到本机的 Claude Code / Codex：

    python -m screenrecall.app.agents install|uninstall|status [--claude] [--codex]

两个都不写时两个都做。登记的命令和"连接 Agent"页面上的一样（mcp_shim.shim_command()：本机 python.exe +
-m screenrecall.app.mcp_shim，env 里带 PYTHONPATH，自定义 SCREENRECALL_HOME 时也带上），都不含令牌。

- Claude Code：经 claude 命令行（user 范围）：先 claude mcp remove screenrecall -s user（没有也不报错），
  再 claude mcp add-json screenrecall '<json>' -s user；status 用 claude mcp get screenrecall。
- Codex：直接改 ~/.codex/config.toml（或 CODEX_HOME 下的）。只动 [mcp_servers.screenrecall] 这一张表（及其子表），
  别的内容逐字保留：改之前先备份（config.toml.bak-screenrecall-<时间>），写之前核对新文件解析后除了这一张表
  和原来完全一样，不一样就不写；原子替换。工具都是只读的，所以写 default_tools_approval_mode = "approve"
  （否则 codex exec 每次调用都因"需要审批"失败，交互模式下每次都要点同意）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from . import mcp_shim

NAME = "screenrecall"
APPROVAL = "approve"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

# 表头：[a.b] 或 [[a.b]]，后面可以跟注释。数组里另起一行的 [1, 2], 有逗号，不算
_HEADER = re.compile(r"""^\s*\[\[?\s*([A-Za-z0-9_\-."' ]+?)\s*\]\]?\s*(#.*)?$""")
_OURS = re.compile(r"""^mcp_servers\s*\.\s*(screenrecall|"screenrecall"|'screenrecall')\s*(\.|$)""")


class InstallError(RuntimeError):
    pass


# ---------------- 命令 ----------------

def shim_entry(python: str | None = None) -> dict[str, Any]:
    return mcp_shim.shim_command(python)


def _run(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def claude_exe() -> str | None:
    return shutil.which("claude")


# ---------------- Claude Code ----------------

def claude_install(entry: dict, run: Runner = _run, exe: str = "claude") -> str:
    run([exe, "mcp", "remove", NAME, "-s", "user"])  # 没登记过时报错，不管它
    spec = {"type": "stdio", "command": entry["command"], "args": list(entry["args"]), "env": dict(entry["env"])}
    p = run([exe, "mcp", "add-json", NAME, json.dumps(spec, ensure_ascii=False), "-s", "user"])
    if p.returncode != 0:
        raise InstallError(f"claude mcp add-json 失败（{p.returncode}）：{(p.stderr or p.stdout).strip()[:300]}")
    return "Claude Code：已登记（user 范围）。新开的 Claude Code 会话里就能用 screenrecall 的工具。"


def claude_uninstall(run: Runner = _run, exe: str = "claude") -> str:
    p = run([exe, "mcp", "remove", NAME, "-s", "user"])
    if p.returncode != 0:
        return "Claude Code：本来就没有登记 screenrecall。"
    return "Claude Code：已移除 screenrecall。"


def claude_status(run: Runner = _run, exe: str = "claude") -> str:
    p = run([exe, "mcp", "get", NAME])
    if p.returncode != 0:
        return "Claude Code：没有登记。"
    first = next((ln.strip() for ln in (p.stdout or "").splitlines() if "status" in ln.lower()), "")
    return "Claude Code：已登记。" + (f"（{first}）" if first else "")


# ---------------- Codex ----------------

def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


def _toml_str(s: str) -> str:
    """TOML 基本字符串（JSON 的转义规则在这里通用）。"""
    return json.dumps(str(s), ensure_ascii=False)


def codex_block(entry: dict) -> str:
    lines = [f"[mcp_servers.{NAME}]",
             "# ScreenRecall（python -m screenrecall.app.agents 写的；工具只读，所以预先批准）",
             f"command = {_toml_str(entry['command'])}",
             "args = [" + ", ".join(_toml_str(a) for a in entry["args"]) + "]",
             f'default_tools_approval_mode = "{APPROVAL}"']
    if entry.get("env"):
        lines.append("")
        lines.append(f"[mcp_servers.{NAME}.env]")
        lines += [f"{k} = {_toml_str(v)}" for k, v in entry["env"].items()]
    return "\n".join(lines) + "\n"


def strip_ours(text: str) -> str:
    """去掉 [mcp_servers.screenrecall] 及其子表（到下一个不是它的表头为止），其余逐字保留。"""
    out: list[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line.rstrip("\r\n"))
        if m:
            inside = bool(_OURS.match(re.sub(r"\s*\.\s*", ".", m.group(1).strip())))
        if not inside:
            out.append(line)
    return "".join(out)


def _parsed_without_ours(text: str) -> dict:
    data = tomllib.loads(text)
    servers = data.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(NAME, None)
        if not servers and "mcp_servers" in data:
            data.pop("mcp_servers")
    return data


def _check_same(old: str, new: str) -> None:
    """新文件解析后除了 mcp_servers.screenrecall 和原来一样，否则不写。"""
    try:
        a = _parsed_without_ours(old) if old.strip() else {}
        b = _parsed_without_ours(new)
    except tomllib.TOMLDecodeError as e:
        raise InstallError(f"config.toml 解析失败，没有改动：{e}") from None
    if a != b:
        raise InstallError("改动会影响 config.toml 里别的设置（文件格式超出了本工具能安全处理的范围），没有写入；"
                           "请手动把“连接 Agent”页面上的 [mcp_servers.screenrecall] 块加进去。")


def _backup(path: Path, now: Callable[[], datetime] = datetime.now) -> Path | None:
    if not path.exists():
        return None
    b = path.with_name(f"{path.name}.bak-screenrecall-{now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, b)
    return b


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".screenrecall.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _read(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def codex_install(entry: dict, path: Path | None = None) -> str:
    path = path or codex_config_path()
    old = _read(path)
    if old.strip():
        try:
            tomllib.loads(old)
        except tomllib.TOMLDecodeError as e:
            raise InstallError(f"{path} 现在就解析不了，没有改动：{e}") from None
    base = strip_ours(old)
    nl = _newline(old)
    block = codex_block(entry).replace("\n", nl)
    # 我们的表总放在最后，前面空一行；再装一次得到逐字相同的文件
    base = base.rstrip("\r\n") + nl if base.strip() else ""
    new = base + (nl if base else "") + block
    _check_same(old, new)
    got = tomllib.loads(new).get("mcp_servers", {}).get(NAME, {})
    if got.get("command") != entry["command"] or got.get("args") != list(entry["args"]):
        raise InstallError("写入后核对失败，没有改动")
    if new == old:
        return f"Codex：{path} 里已经是这份配置，没有改动。"
    b = _backup(path)
    _write_atomic(path, new)
    return f"Codex：已写入 {path} 的 [mcp_servers.{NAME}]" + (f"（原文件备份为 {b.name}）" if b else "") + "。"


def codex_uninstall(path: Path | None = None) -> str:
    path = path or codex_config_path()
    old = _read(path)
    if not old:
        return f"Codex：{path} 不存在，没有要移除的。"
    new = strip_ours(old)
    if new == old:
        return "Codex：config.toml 里没有 screenrecall。"
    _check_same(old, new)
    if NAME in (tomllib.loads(new).get("mcp_servers") or {}):
        raise InstallError("config.toml 里 screenrecall 是用别的写法定义的，没有改动；请手动删除。")
    b = _backup(path)
    _write_atomic(path, new)
    return f"Codex：已移除 screenrecall（{path}；原文件备份为 {b.name if b else '—'}）。"


def codex_status(entry: dict | None = None, path: Path | None = None) -> str:
    path = path or codex_config_path()
    old = _read(path)
    try:
        got = (tomllib.loads(old).get("mcp_servers") or {}).get(NAME) if old else None
    except tomllib.TOMLDecodeError:
        return f"Codex：{path} 解析失败。"
    if not got:
        return "Codex：没有登记。"
    note = ""
    if entry and (got.get("command") != entry["command"] or got.get("env", {}) != entry.get("env", {})):
        note = "（命令或环境变量和本机现在的不一样，重新 install 一次即可）"
    if got.get("default_tools_approval_mode") != APPROVAL:
        note += "（没有预先批准：codex exec 里调用工具会失败）"
    return "Codex：已登记。" + note


# ---------------- 设置页用 ----------------

def claude_config_path() -> Path:
    return Path.home() / ".claude.json"


def claude_registered(path: Path | None = None) -> bool:
    """~/.claude.json 里（user 范围或任一项目）有没有 screenrecall。直接读文件：不起 claude 命令行（要好几秒）。"""
    try:
        data = json.loads((path or claude_config_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if NAME in (data.get("mcpServers") or {}):
        return True
    return any(NAME in ((p or {}).get("mcpServers") or {}) for p in (data.get("projects") or {}).values()
               if isinstance(p, dict))


def codex_registered(path: Path | None = None) -> bool:
    try:
        text = _read(path or codex_config_path())
        return bool(text) and NAME in (tomllib.loads(text).get("mcp_servers") or {})
    except (OSError, tomllib.TOMLDecodeError):
        return False


def quick_status(*, claude_path: Path | None = None, codex_path: Path | None = None) -> dict[str, Any]:
    """{claude: 已登记?, codex: 已登记?, claude_cli: 找得到 claude 命令?}"""
    return {"claude": claude_registered(claude_path), "codex": codex_registered(codex_path),
            "claude_cli": claude_exe() is not None}


def install_all(*, run: Runner = _run, codex_path: Path | None = None, python: str | None = None) -> list[str]:
    """两个都登记一遍（已经登记的照样重写成本机现在的命令）。返回每一项的结果说明；某一项失败不影响另一项。"""
    entry = shim_entry(python)
    msgs: list[str] = []
    exe = claude_exe() if run is _run else "claude"
    if exe is None:
        msgs.append("Claude Code：找不到 claude 命令，跳过。")
    else:
        try:
            msgs.append(claude_install(entry, run, exe))
        except (InstallError, OSError, subprocess.SubprocessError) as e:
            msgs.append(f"Claude Code：{e}")
    try:
        msgs.append(codex_install(entry, codex_path))
    except (InstallError, OSError) as e:
        msgs.append(f"Codex：{e}")
    return msgs


# ---------------- 命令行 ----------------

@dataclass
class Targets:
    claude: bool
    codex: bool


def main(argv: list[str] | None = None, *, run: Runner = _run, codex_path: Path | None = None,
         python: str | None = None, out=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.app.agents",
                                 description="把 ScreenRecall 登记到 Claude Code / Codex（不含令牌）")
    ap.add_argument("action", choices=("install", "uninstall", "status"))
    ap.add_argument("--claude", action="store_true", help="只处理 Claude Code")
    ap.add_argument("--codex", action="store_true", help="只处理 Codex")
    args = ap.parse_args(argv)
    out = out or sys.stdout
    t = Targets(args.claude or not args.codex, args.codex or not args.claude)
    entry = shim_entry(python)
    rc = 0
    msgs: list[str] = []
    if t.claude:
        exe = claude_exe() if run is _run else "claude"
        if exe is None:
            msgs.append("Claude Code：找不到 claude 命令（没有安装，或不在 PATH 里），跳过。")
            rc = 1 if args.action == "install" else rc
        else:
            try:
                msgs.append({"install": lambda: claude_install(entry, run, exe),
                             "uninstall": lambda: claude_uninstall(run, exe),
                             "status": lambda: claude_status(run, exe)}[args.action]())
            except (InstallError, OSError, subprocess.SubprocessError) as e:
                msgs.append(f"Claude Code：{e}")
                rc = 1
    if t.codex:
        try:
            msgs.append({"install": lambda: codex_install(entry, codex_path),
                         "uninstall": lambda: codex_uninstall(codex_path),
                         "status": lambda: codex_status(entry, codex_path)}[args.action]())
        except (InstallError, OSError) as e:
            msgs.append(f"Codex：{e}")
            rc = 1
    for m in msgs:
        print(m, file=out)
    return rc


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
