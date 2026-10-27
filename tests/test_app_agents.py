"""python -m screenrecall.app.agents：登记 / 移除 / 状态。全部对着临时目录里的 config.toml 和假的 claude 命令，
不碰本机真正的 ~/.claude.json、~/.codex/config.toml。"""
from __future__ import annotations

import io
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from screenrecall.app import agents

ENTRY = {"command": r"D:\repo dir\.venv\Scripts\python.exe", "args": ["-m", "screenrecall.app.mcp_shim"],
         "env": {"PYTHONPATH": r"D:\repo dir"}}

USER_CFG = """\
# my codex settings
model_provider = "custom"
model = "gpt-6.1-sol-fast"   # keep me

[model_providers.custom]
name = "relay"
base_url = "https://example.invalid/v1"

[mcp_servers.github]
command = "npx"
args = [
  "-y",
  "@modelcontextprotocol/server-github",
]

[mcp_servers.github.env]
GITHUB_TOKEN = "placeholder"

[projects.'D:\\Desktop']
trust_level = "trusted"
"""


def test_codex_install_adds_only_our_table_and_keeps_the_rest(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(USER_CFG, encoding="utf-8")
    msg = agents.codex_install(ENTRY, p)
    new = p.read_text(encoding="utf-8")
    assert new.startswith(USER_CFG)  # 原文逐字保留（注释、多行数组、引号写法都在）
    data = tomllib.loads(new)
    ours = data["mcp_servers"]["screenrecall"]
    assert ours == {"command": ENTRY["command"], "args": ENTRY["args"], "default_tools_approval_mode": "approve",
                    "env": ENTRY["env"]}
    assert data["mcp_servers"]["github"]["env"]["GITHUB_TOKEN"] == "placeholder"
    backups = list(tmp_path.glob("config.toml.bak-screenrecall-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == USER_CFG
    assert "已写入" in msg and "token" not in new.lower().replace("github_token", "")


def test_codex_install_twice_replaces_in_place_and_is_idempotent(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(USER_CFG + "\n[tui]\nnotifications = true\n", encoding="utf-8")
    agents.codex_install({**ENTRY, "command": r"C:\old\python.exe"}, p)
    agents.codex_install(ENTRY, p)
    text = p.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.screenrecall]") == 1 and r"C:\old" not in text
    data = tomllib.loads(text)
    assert data["tui"] == {"notifications": True} and data["mcp_servers"]["screenrecall"]["command"] == ENTRY["command"]
    n_backups = len(list(tmp_path.glob("*.bak-screenrecall-*")))
    assert "没有改动" in agents.codex_install(ENTRY, p)
    assert len(list(tmp_path.glob("*.bak-screenrecall-*"))) == n_backups  # 没改就不备份


def test_codex_install_handles_our_table_in_the_middle_and_crlf(tmp_path):
    p = tmp_path / "config.toml"
    old = ('model = "x"\r\n\r\n[mcp_servers.screenrecall]\r\ncommand = "old"\r\n\r\n[mcp_servers.screenrecall.env]\r\n'
           'A = "1"\r\n\r\n[ mcp_servers . other ]  # comment\r\ncommand = "o"\r\n')
    p.write_bytes(old.encode())
    agents.codex_install(ENTRY, p)
    raw = p.read_bytes().decode()
    assert "\n" not in raw.replace("\r\n", "")  # 保持 CRLF
    data = tomllib.loads(raw)
    assert data["mcp_servers"]["other"] == {"command": "o"} and data["model"] == "x"
    assert data["mcp_servers"]["screenrecall"]["env"] == ENTRY["env"]  # 旧的 A = "1" 不留


def test_codex_install_creates_a_missing_file(tmp_path):
    p = tmp_path / "sub" / "config.toml"
    agents.codex_install(ENTRY, p)
    assert tomllib.loads(p.read_text(encoding="utf-8"))["mcp_servers"]["screenrecall"]["args"] == ENTRY["args"]
    assert not list(p.parent.glob("*.bak-*"))


def test_codex_refuses_broken_or_unusual_files_without_writing(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("model = \n", encoding="utf-8")
    with pytest.raises(agents.InstallError, match="解析不了"):
        agents.codex_install(ENTRY, p)
    assert p.read_text(encoding="utf-8") == "model = \n"
    # screenrecall 用内联表定义在 [mcp_servers] 里：本工具不去拆别人的写法
    odd = '[mcp_servers]\nscreenrecall = { command = "x" }\n'
    p.write_text(odd, encoding="utf-8")
    with pytest.raises(agents.InstallError):
        agents.codex_install(ENTRY, p)
    assert p.read_text(encoding="utf-8") == odd and not list(tmp_path.glob("*.bak-*"))


def test_codex_uninstall_and_status(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(USER_CFG, encoding="utf-8")
    assert agents.codex_status(ENTRY, p) == "Codex：没有登记。"
    agents.codex_install(ENTRY, p)
    assert agents.codex_status(ENTRY, p) == "Codex：已登记。"
    assert "不一样" in agents.codex_status({**ENTRY, "command": "other"}, p)
    assert "已移除" in agents.codex_uninstall(p)
    assert tomllib.loads(p.read_text(encoding="utf-8")) == tomllib.loads(USER_CFG)
    assert "没有 screenrecall" in agents.codex_uninstall(p)


class FakeClaude:
    def __init__(self, registered: bool = False):
        self.calls: list[list[str]] = []
        self.registered = registered

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(cmd)
        sub = cmd[2]
        if sub == "remove":
            rc, self.registered = (0 if self.registered else 1), False
        elif sub == "add-json":
            json.loads(cmd[4])
            rc, self.registered = 0, True
        elif sub == "get":
            rc = 0 if self.registered else 1
        else:
            rc = 2
        return subprocess.CompletedProcess(cmd, rc, "  Status: ✔ connected\n" if rc == 0 else "", "")


def test_claude_install_uses_add_json_in_user_scope():
    fake = FakeClaude(registered=True)
    msg = agents.claude_install(ENTRY, fake)
    assert fake.calls[0] == ["claude", "mcp", "remove", "screenrecall", "-s", "user"]
    add = fake.calls[1]
    assert add[:4] == ["claude", "mcp", "add-json", "screenrecall"] and add[5:] == ["-s", "user"]
    assert json.loads(add[4]) == {"type": "stdio", **ENTRY}
    assert "已登记" in msg
    assert "connected" in agents.claude_status(fake)
    assert "已移除" in agents.claude_uninstall(fake)
    assert agents.claude_status(fake) == "Claude Code：没有登记。"
    assert "本来就没有" in agents.claude_uninstall(fake)


def test_claude_install_failure_is_reported():
    def failing(cmd):
        return subprocess.CompletedProcess(list(cmd), 1, "", "boom")
    with pytest.raises(agents.InstallError, match="boom"):
        agents.claude_install(ENTRY, failing)


def test_cli_targets_both_by_default_and_one_with_a_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("SCREENRECALL_HOME", raising=False)
    p = tmp_path / "config.toml"
    fake = FakeClaude()
    out = io.StringIO()
    assert agents.main(["install"], run=fake, codex_path=p, python=ENTRY["command"], out=out) == 0
    assert fake.registered and "screenrecall" in tomllib.loads(p.read_text(encoding="utf-8"))["mcp_servers"]
    cmd = tomllib.loads(p.read_text(encoding="utf-8"))["mcp_servers"]["screenrecall"]
    assert cmd["command"] == ENTRY["command"] and cmd["env"]["PYTHONPATH"] == str(Path(agents.__file__).parents[2])
    fake.calls.clear()
    out = io.StringIO()
    agents.main(["uninstall", "--codex"], run=fake, codex_path=p, out=out)
    assert fake.calls == [] and "已移除" in out.getvalue()
    out = io.StringIO()
    agents.main(["status", "--claude"], run=fake, codex_path=p, out=out)
    assert out.getvalue().startswith("Claude Code：已登记") and "Codex" not in out.getvalue()
