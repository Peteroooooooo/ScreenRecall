"""v2 主进程骨架：python -m screenrecall.app [--data-dir D] [--port P] [--no-tray] [--no-browser] [--fake-queries]

做的事（按顺序）：单实例检查 → 建好只有本人能读的目录 → 令牌与 endpoint.json → 绑定端口 →
拼网页应用（旧版 webapi + /mcp + 登录入口，外面包 AuthGuard）并在线程里运行 → 写 MCP 工具缓存 →
启动子进程看护（录制进程、空闲整理进程在集成阶段接入，现在是空列表）→ 空闲监视 → 托盘（主线程）→ 退出时依次收尾。

已经有一个实例在运行时，第二次启动只是让它打开主页，然后退出。
数据层、录制控制都还没接：MCP 默认用 NotReadyQueries（--fake-queries 改用合成数据），托盘动作用 PlaceholderController。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import logging.handlers
import os
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, idle, mcp_server, mcp_shim, paths, server, supervisor, tray

DEFAULT_PORT = 8611
IDLE_CHILD = "idle-worker"
log = logging.getLogger("screenrecall.app")


# ---------------- 单实例 ----------------

class SingleInstance:
    """本用户会话内的命名互斥量（Local\\），按 app_home 区分：测试用的临时目录不会和正式实例冲突。"""

    def __init__(self, home: Path):
        digest = hashlib.sha1(str(home.resolve()).lower().encode("utf-8")).hexdigest()[:16]
        self.name = f"Local\\ScreenRecall-main-{digest}"
        self._handle = None

    def acquire(self) -> bool:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        ctypes.set_last_error(0)  # 新建时 CreateMutexW 不一定清零上次的错误码
        h = k32.CreateMutexW(None, False, self.name)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(ctypes.c_void_p(h))
            return False
        self._handle = h
        return True

    def release(self) -> None:
        if self._handle:
            ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


# ---------------- 托盘动作（集成前的占位） ----------------

class Controller(Protocol):
    """托盘要用的录制控制（集成阶段由录制进程的控制通道实现）。"""

    def pause_until(self, until: datetime | None) -> None: ...
    def resume(self) -> None: ...
    def delete_recent(self, minutes: int) -> None: ...
    def status(self) -> tray.TrayStatus: ...


class PlaceholderController:
    """只在内存里记暂停状态；删除只写日志。"""

    def __init__(self) -> None:
        self.paused_until: datetime | None = None
        self.paused = False

    def pause_until(self, until: datetime | None) -> None:
        self.paused, self.paused_until = True, until
        log.info("暂停到 %s（录制进程尚未接入）", until)

    def resume(self) -> None:
        self.paused, self.paused_until = False, None

    def delete_recent(self, minutes: int) -> None:
        log.info("删除最近 %d 分钟（数据层尚未接入，什么都没删）", minutes)

    def status(self) -> tray.TrayStatus:
        if self.paused and self.paused_until and datetime.now() >= self.paused_until:
            self.resume()
        if self.paused:
            until = f"到 {self.paused_until:%m-%d %H:%M}" if self.paused_until else ""
            return tray.TrayStatus(f"已暂停{until}", paused=True)
        return tray.TrayStatus("开发版：录制进程尚未接入")


# ---------------- 主程序 ----------------

@dataclass
class AppOptions:
    home: Path | None = None
    data_dir: Path | None = None
    port: int = DEFAULT_PORT
    tray: bool = True
    open_browser: bool = True
    fake_queries: bool = False
    legacy_web: bool = True
    extra_origins: tuple[str, ...] = ()
    children: list[supervisor.ChildSpec] = field(default_factory=list)
    idle_threshold_s: float = 300.0


class MainApp:
    def __init__(self, opts: AppOptions, *, queries: mcp_server.Queries | None = None,
                 controller: Controller | None = None, policy: mcp_server.AgentPolicy | None = None):
        self.opts = opts
        self.home = opts.home or paths.app_home()
        self.data_dir = opts.data_dir or (self.home / "data")
        self.queries = queries or (mcp_server.FakeQueries() if opts.fake_queries else mcp_server.NotReadyQueries())
        self.controller = controller or PlaceholderController()
        self.policy = policy or mcp_server.AgentPolicy()
        self.nonces = auth.NonceStore()
        self.instance = SingleInstance(self.home)
        self.stopped = threading.Event()
        self.endpoint: auth.Endpoint | None = None
        self.web: server.ServerThread | None = None
        self.sup: supervisor.Supervisor | None = None
        self.idle: idle.IdleWatcher | None = None
        self.tray: tray.Tray | None = None

    # ---------- 启动 ----------

    def _legacy_app(self, port: int):
        if not self.opts.legacy_web:
            return None
        from .. import webapi
        from ..config import load_config

        return webapi.create_app(load_config(self.data_dir), port=port, extra_origins=self.opts.extra_origins)

    def start(self) -> "MainApp":
        paths.ensure_private_dir(self.home)
        paths.ensure_private_dir(self.data_dir)
        token = auth.load_or_create_token(self.home)
        sock = server.bind_socket(self.opts.port)
        port = sock.getsockname()[1]
        mcp = mcp_server.build_mcp(self.queries, self.policy, port=port)
        mcp_shim.write_tools_cache(anyio.run(mcp_server.tool_schemas, mcp), self.home)

        async def quit_route(_request: Request) -> JSONResponse:  # 网页"退出"按钮、脚本和测试用（需要登录）
            threading.Thread(target=self.request_stop, daemon=True).start()
            return JSONResponse({"message": "正在退出"})

        app = server.build_app(port=port, token=token, nonces=self.nonces, mcp=mcp, legacy=self._legacy_app(port),
                               extra_routes=[Route("/api/app/quit", quit_route, methods=["POST"])],
                               extra_origins=self.opts.extra_origins)
        self.web = server.ServerThread(app, sock).start()
        self.endpoint = auth.Endpoint(port, token, os.getpid())
        auth.write_endpoint(self.endpoint, self.home)  # 服务已经能接请求后再公布端口
        log.info("主进程已启动：http://127.0.0.1:%d（数据目录 %s）", port, self.data_dir)
        self.sup = supervisor.Supervisor(self.opts.children).start()
        self.idle = idle.IdleWatcher(self.opts.idle_threshold_s, on_idle_start=self._on_idle,
                                     on_idle_end=self._on_active).start()
        return self

    def _on_idle(self, reason: str) -> None:
        log.info("电脑进入空闲（%s）", reason)
        if self.sup and IDLE_CHILD in self.sup.status():
            self.sup.start_child(IDLE_CHILD)

    def _on_active(self) -> None:
        log.info("电脑恢复使用")
        if self.sup and IDLE_CHILD in self.sup.status():
            self.sup.stop_child(IDLE_CHILD)

    # ---------- 运行 ----------

    def home_url(self) -> str:
        assert self.endpoint is not None
        return auth.home_url(self.endpoint.port, self.nonces.mint())

    def open_home(self) -> None:
        webbrowser.open(self.home_url())

    def request_stop(self) -> None:
        self.stopped.set()
        if self.tray:
            self.tray.stop()

    def run(self) -> int:
        if not self.instance.acquire():
            return open_running_instance(self.home, self.opts.open_browser)
        try:
            self.start()
            if self.opts.open_browser:
                self.open_home()
            if self.opts.tray:
                c = self.controller
                self.tray = tray.Tray(tray.TrayActions(pause_until=c.pause_until, resume=c.resume,
                                                       delete_recent=c.delete_recent, open_home=self.open_home,
                                                       quit=self.request_stop, status=c.status))
                self.tray.run()  # 阻塞到"退出"
            else:
                while not self.stopped.wait(0.5):
                    pass
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        """先停子进程（不再录），再停空闲监视和网页服务；令牌保留，pid 清空。"""
        self.stopped.set()
        if self.sup:
            self.sup.stop_all()
        if self.idle:
            self.idle.stop()
        if self.web:
            self.web.stop()
        if self.endpoint:
            try:
                auth.write_endpoint(auth.Endpoint(self.endpoint.port, self.endpoint.token, None), self.home)
            except OSError:
                log.warning("更新 endpoint.json 失败", exc_info=True)
        self.instance.release()
        log.info("主进程已退出")


def open_running_instance(home: Path, open_browser: bool = True) -> int:
    """已有实例在运行：向它要一条一次性登录链接并打开（open_browser=False 时只打印"已在运行"）。"""
    ep = auth.read_endpoint(home)
    if ep is None:
        print("ScreenRecall 已经在运行，但找不到它的地址文件。", file=sys.stderr)
        return 1
    try:
        r = httpx.post(f"{ep.base_url}/api/app/nonce", headers=auth.bearer_headers(ep), timeout=5, trust_env=False)
        r.raise_for_status()
        url = r.json()["url"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"ScreenRecall 已经在运行，但打不开它的主页：{type(exc).__name__}", file=sys.stderr)
        return 1
    if open_browser:
        webbrowser.open(url)
    else:
        print(f"ScreenRecall 已经在运行（端口 {ep.port}）。")
    return 0


def _setup_logging(home: Path) -> None:
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(logs / "main.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handlers: list[logging.Handler] = [fh]
    if sys.stderr is not None:  # pythonw 下没有标准错误
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    logging.getLogger("screenrecall").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.app", description="ScreenRecall v2 主进程")
    ap.add_argument("--data-dir", help="数据目录（默认 %%LOCALAPPDATA%%\\ScreenRecall\\data）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="优先使用的端口（被占用时自动换一个）")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标（按 Ctrl+C 退出）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不打开主页")
    ap.add_argument("--autostart", action="store_true", help="开机自启时使用：不打开主页")
    ap.add_argument("--fake-queries", action="store_true", help="MCP 用合成数据（开发 / 演示）")
    ap.add_argument("--no-legacy-web", action="store_true", help="不挂旧版 webapi")
    ap.add_argument("--dev-origin", action="append", default=[], help="开发时允许的前端地址")
    a = ap.parse_args(argv)
    home = paths.app_home()
    paths.ensure_private_dir(home)
    _setup_logging(home)
    opts = AppOptions(home=home, data_dir=Path(a.data_dir) if a.data_dir else None, port=a.port,
                      tray=not a.no_tray, open_browser=not (a.no_browser or a.autostart),
                      fake_queries=a.fake_queries, legacy_web=not a.no_legacy_web, extra_origins=tuple(a.dev_origin))
    return MainApp(opts).run()
