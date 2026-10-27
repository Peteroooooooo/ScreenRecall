"""给 Agent 用的 stdio MCP 转发程序：python -m screenrecall.app.mcp_shim

Claude Desktop / Claude Code / Cursor 用 stdio 启动它；它把每个工具调用转发到主进程的 http://127.0.0.1:<端口>/mcp。
端口和令牌每次调用时从 %LOCALAPPDATA%\\ScreenRecall\\endpoint.json 现读，所以 Agent 的配置文件里没有令牌，
主进程换了端口也不用改配置。只依赖 mcp 和 httpx，不加载模型、不碰数据库，常驻内存很小。

主程序没在运行时：列工具用主程序上次写下的 mcp_tools.json（没有密钥），调用工具返回一句说明。
标准输出是 MCP 协议通道，日志只写标准错误。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import auth, paths

TOOLS_CACHE = "mcp_tools.json"
NOT_RUNNING = "ScreenRecall 没有在运行（或还没启动完）。请先打开 ScreenRecall（托盘里能看到它的图标），再重试。"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
log = logging.getLogger("screenrecall.app.mcp_shim")


class NotRunning(RuntimeError):
    pass


def tools_cache_path(home: Path | None = None) -> Path:
    return (home or paths.app_home()) / TOOLS_CACHE


def write_tools_cache(tools: list[dict[str, Any]], home: Path | None = None) -> Path:
    """主进程启动时调用：把工具定义写下来（转发程序离线时用）。"""
    p = tools_cache_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tools, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def read_tools_cache(home: Path | None = None) -> list[types.Tool]:
    try:
        raw = json.loads(tools_cache_path(home).read_text(encoding="utf-8"))
        return [types.Tool.model_validate(t) for t in raw]
    except (OSError, ValueError):
        return []


def listener_pid(port: int) -> int | None:
    """本机 127.0.0.1:port（或 0.0.0.0:port）上正在监听的进程号；没人监听返回 None。"""
    import psutil  # 只在这里用：GetExtendedTcpTable，几毫秒

    for c in psutil.net_connections(kind="tcp4"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.laddr.ip in ("127.0.0.1", "0.0.0.0"):
            return c.pid
    return None


def verified_endpoint(home: Path | None) -> auth.Endpoint:
    """读 endpoint.json，并确认监听这个端口的正是 endpoint.json 里记的主进程，才把令牌发过去。

    主程序没运行时端口是空着的：本机别的程序（包括别的账户）可以先占住这个端口，等 Agent 调用时
    收下 Authorization 头里的令牌。所以只"能连上"不够，要核对监听者的进程号。"""
    ep = auth.read_endpoint(home)
    if ep is None or ep.pid is None:  # 正常退出时 pid 已清空
        raise NotRunning(NOT_RUNNING)
    try:
        owner = listener_pid(ep.port)
    except Exception:  # noqa: BLE001 - 查不到就不发令牌
        log.warning("无法确认端口 %d 的监听进程", ep.port, exc_info=True)
        raise NotRunning(NOT_RUNNING) from None
    if owner is None:
        raise NotRunning(NOT_RUNNING)
    if owner != ep.pid:
        log.warning("端口 %d 被别的进程（%s）占用，不是 ScreenRecall 主进程（%s），不发送令牌", ep.port, owner, ep.pid)
        raise NotRunning(NOT_RUNNING)
    return ep


@contextlib.asynccontextmanager
async def remote_session(home: Path | None = None, timeout: float = 60.0) -> AsyncIterator[ClientSession]:
    """连到主进程的 /mcp（每次调用新开一个会话：服务端是无状态模式，主进程重启也不受影响）。"""
    ep = verified_endpoint(home)
    async with httpx.AsyncClient(headers=auth.bearer_headers(ep), timeout=httpx.Timeout(timeout, connect=3.0),
                                 trust_env=False) as client:  # trust_env=False：本机地址不走系统代理
        async with streamable_http_client(f"{ep.base_url}/mcp", http_client=client) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def build_shim(home: Path | None = None) -> Server:
    server: Server = Server("screenrecall")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        try:
            async with remote_session(home) as s:
                return (await s.list_tools()).tools
        except Exception as exc:  # noqa: BLE001 - 主程序没运行 / 连接失败 → 用缓存的工具定义
            log.info("列出工具时连不上主程序（%s），使用缓存", type(exc).__name__)
            return read_tools_cache(home)

    @server.call_tool(validate_input=False)  # 参数校验交给主进程，转发程序不重复做
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            async with remote_session(home) as s:
                return await s.call_tool(name, arguments or {})
        except NotRunning as exc:
            msg = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("转发 %s 失败", name, exc_info=True)
            msg = f"转发到 ScreenRecall 失败：{type(exc).__name__}。请确认 ScreenRecall 正在运行。"
        return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], isError=True)

    return server


async def serve(home: Path | None = None) -> None:
    server = build_shim(home)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


# ---------------- 给"连接 Agent"页面的配置片段 ----------------

def shim_command(python: str | None = None, root: Path = REPO_ROOT) -> dict[str, Any]:
    """启动转发程序的命令（command + args + env）。用 python.exe 而不是 pythonw：stdio 需要标准输入输出。
    主进程开机自启时自己是 pythonw.exe 跑的，所以这里把 pythonw.exe 换回同目录的 python.exe。"""
    exe = Path(python or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    env = {"PYTHONPATH": str(root)}
    # 主进程用的不是默认目录（SCREENRECALL_HOME，便携使用 / 测试）：转发程序也得去那里找 endpoint.json，
    # 不然它会连默认目录里的另一个实例，或者说"没在运行"
    if os.environ.get("SCREENRECALL_HOME"):
        env["SCREENRECALL_HOME"] = str(paths.app_home())
    return {"command": str(exe), "args": ["-m", "screenrecall.app.mcp_shim"], "env": env}


def agent_configs(python: str | None = None, root: Path = REPO_ROOT) -> dict[str, dict[str, Any]]:
    """三种 Agent 的配置。都不含令牌（转发程序运行时自己读 endpoint.json）。"""
    cmd = shim_command(python, root)
    entry = {"mcpServers": {"screenrecall": dict(cmd)}}
    env_args = [x for k, v in cmd["env"].items() for x in ("-e", f"{k}={v}")]
    cli = subprocess.list2cmdline(["claude", "mcp", "add", "screenrecall", "--scope", "user", *env_args, "--",
                                   cmd["command"], *cmd["args"]])
    return {
        "claude_desktop": {"name": "Claude Desktop", "file": r"%APPDATA%\Claude\claude_desktop_config.json",
                           "json": entry},
        "claude_code": {"name": "Claude Code", "file": ".mcp.json（项目）或用下面的命令添加",
                        "json": {"mcpServers": {"screenrecall": {"type": "stdio", **cmd}}}, "cli": cli},
        "cursor": {"name": "Cursor", "file": r"%USERPROFILE%\.cursor\mcp.json", "json": entry},
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    anyio.run(serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
