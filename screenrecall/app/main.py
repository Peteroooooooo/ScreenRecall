"""v2 主进程：python -m screenrecall.app [--data-dir D] [--port P] [--no-tray] [--no-browser] [--fake-queries]

做的事（按顺序）：单实例检查 → 建好只有本人能读的目录 → v2 存储（写线程、只读查询、检索服务；第一次用的数据目录
写入默认规则并开始录制）→ 令牌与 endpoint.json → 绑定端口 → 拼网页应用（/api/v2 + 旧版 webapi + /mcp +
登录入口，外面包 AuthGuard）并在线程里运行 → 写 MCP 工具缓存 → 启动子进程看护（录制进程一直在；
空闲整理进程只在电脑空闲时运行）→ 空闲监视 → 托盘（主线程）→ 退出时依次收尾。

已经有一个实例在运行时，第二次启动只是让它打开主页，然后退出。
--fake-queries（测试 / 演示）：不接 v2 存储、不启动子进程，MCP 用合成数据，托盘动作用 PlaceholderController。
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import logging
import logging.handlers
import os
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import anyio
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import auth, idle, mcp_server, mcp_shim, paths, server, supervisor, tray

DEFAULT_PORT = 8611
RECORDER_CHILD = "recorder"
IDLE_CHILD = "idle-worker"
RECORDER_MODULE = "screenrecall.v2.recorder"
WORKER_MODULE = "screenrecall.v2.worker"
V2_NOTICE = "页面只在本机访问（127.0.0.1），需要从托盘图标的“打开主页”登录。"
HOUSEKEEPING_S = 2.0
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
    store: bool = False             # 接 v2 存储（/api/v2、MCP 查询、托盘控制）；命令行在没有 --fake-queries 时打开
    cfg: Any = None                 # v2.config.V2Config；store=True 且没给时按 data_dir / home 读


def build_children(cfg, *, home: Path, recorder_module: str = RECORDER_MODULE,
                   worker_module: str | None = WORKER_MODULE) -> list[supervisor.ChildSpec]:
    """录制进程（一直运行，退出就重启）+ 空闲整理进程（空闲时才启动，出错才重启，低优先级）。"""
    args = ("--data-dir", str(cfg.data_dir), "--home", str(home))
    logs = Path(cfg.logs_dir)
    specs = [supervisor.ChildSpec(RECORDER_CHILD, supervisor.python_child(recorder_module, *args), restart="always",
                                  env=supervisor.child_env(), log_path=logs / "recorder.out.log")]
    if worker_module:
        specs.append(supervisor.ChildSpec(IDLE_CHILD, supervisor.python_child(worker_module, *args),
                                          restart="on-failure", autostart=False, below_normal=True,
                                          env=supervisor.child_env(), log_path=logs / "idle-worker.out.log"))
    return specs


class MainApp:
    def __init__(self, opts: AppOptions, *, queries: mcp_server.Queries | None = None,
                 controller: Controller | None = None, policy: mcp_server.AgentPolicy | None = None):
        self.opts = opts
        self.home = opts.home or paths.app_home()
        self.data_dir = Path(opts.cfg.data_dir) if opts.cfg is not None else (opts.data_dir or (self.home / "data"))
        self._given = (queries, controller, policy)
        self.queries = queries or (mcp_server.FakeQueries() if opts.fake_queries else mcp_server.NotReadyQueries())
        self.controller = controller or PlaceholderController()
        self.policy = policy or mcp_server.AgentPolicy()
        self.stack = None               # v2.adapters.StoreStack（store=True 时）
        self.guard: auth.AuthGuard | None = None
        self._housekeeping: threading.Thread | None = None
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

        notice = V2_NOTICE if self.opts.store else webapi.NO_AUTH_NOTICE
        return webapi.create_app(load_config(self.data_dir), port=port, extra_origins=self.opts.extra_origins,
                                 notice=notice)

    def _start_store(self) -> list:
        """v2 存储与 /api/v2 路由；返回要挂上的路由。"""
        from ..v2 import adapters, web
        from ..v2.config import load_v2

        cfg = self.opts.cfg or load_v2(self.data_dir, home=self.home)
        self.stack = adapters.StoreStack(cfg)
        if self.stack.seed_control():
            log.info("新的数据目录：已写入默认排除规则并开始录制")
        queries, controller, policy = self._given
        self.queries = queries or adapters.StoreQueriesAdapter(self.stack.queries, self.stack.search)
        self.controller = controller or adapters.StoreController(self.stack)
        self.policy = policy or adapters.agent_policy(self.stack.control)
        ctx = web.V2Context(self.stack, self.home, idle_running=self._idle_running, rotate_token=self._rotate_token,
                            on_settings=self._on_settings)
        return web.routes(ctx)

    def start(self) -> "MainApp":
        t0 = time.perf_counter()
        paths.ensure_private_dir(self.home)
        paths.ensure_private_dir(self.data_dir)
        v2_routes = self._start_store() if self.opts.store else []
        token = auth.load_or_create_token(self.home)
        sock = server.bind_socket(self.opts.port)
        port = sock.getsockname()[1]
        mcp = mcp_server.build_mcp(self.queries, self.policy, port=port)
        mcp_shim.write_tools_cache(anyio.run(mcp_server.tool_schemas, mcp), self.home)

        async def quit_route(_request: Request) -> JSONResponse:  # 网页"退出"按钮、脚本和测试用（需要登录）
            threading.Thread(target=self.request_stop, daemon=True).start()
            return JSONResponse({"message": "正在退出"})

        self.guard = server.build_app(port=port, token=token, nonces=self.nonces, mcp=mcp,
                                      legacy=self._legacy_app(port),
                                      extra_routes=[Route("/api/app/quit", quit_route, methods=["POST"]), *v2_routes],
                                      extra_origins=self.opts.extra_origins)
        self.web = server.ServerThread(self.guard, sock).start()
        self.endpoint = auth.Endpoint(port, token, os.getpid())
        auth.write_endpoint(self.endpoint, self.home)  # 服务已经能接请求后再公布端口
        log.info("主进程已启动：http://127.0.0.1:%d（数据目录 %s，用时 %.2f 秒）", port, self.data_dir,
                 time.perf_counter() - t0)
        self.sup = supervisor.Supervisor(self.opts.children).start()
        threshold = self.stack.cfg.idle.threshold_s if self.stack is not None else self.opts.idle_threshold_s
        self.idle = idle.IdleWatcher(threshold, on_idle_start=self._on_idle, on_idle_end=self._on_active).start()
        if hasattr(self.controller, "tick"):
            self._housekeeping = threading.Thread(target=self._housekeep, name="screenrecall-housekeeping",
                                                  daemon=True)
            self._housekeeping.start()
        return self

    def _housekeep(self) -> None:
        """暂停到点改回录制、补删没删掉的向量（StoreController.tick）。"""
        while not self.stopped.wait(HOUSEKEEPING_S):
            try:
                self.controller.tick()
            except Exception:  # noqa: BLE001 - 定时任务不能停
                log.warning("定时检查出错", exc_info=True)

    def _idle_running(self) -> bool:
        return bool(self.sup and IDLE_CHILD in self.sup.status() and self.sup.pid(IDLE_CHILD) is not None)

    def _on_settings(self, changes: dict) -> None:
        if "idle_minutes" in changes and self.idle is not None:
            self.idle.threshold_s = float(changes["idle_minutes"]) * 60.0

    def _rotate_token(self) -> str:
        """"重置访问令牌"：新令牌写进 endpoint.json（转发程序每次调用现读），旧令牌和旧 Cookie 立即失效。
        返回给当前浏览器的新 Set-Cookie 值。"""
        assert self.endpoint is not None and self.guard is not None
        token = secrets.token_urlsafe(32)
        self.guard.set_token(token)
        self.endpoint = auth.Endpoint(self.endpoint.port, token, os.getpid())
        auth.write_endpoint(self.endpoint, self.home)
        log.info("访问令牌已重置")
        return auth.session_cookie(self.endpoint.port, token)

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
        """先停空闲监视（它会启动空闲整理进程），再停子进程（不再录），最后停网页服务；令牌保留，pid 清空。"""
        self.stopped.set()
        if self.idle:
            self.idle.stop()
        if self.sup:
            self.sup.stop_all()
        if self.web:
            self.web.stop()
        if self._housekeeping is not None:
            self._housekeeping.join(5)
        if self.stack is not None:
            try:
                self.stack.close()
            except Exception:  # noqa: BLE001
                log.warning("关闭存储出错", exc_info=True)
        if self.endpoint:
            try:
                auth.write_endpoint(auth.Endpoint(self.endpoint.port, self.endpoint.token, None), self.home)
            except OSError:
                log.warning("更新 endpoint.json 失败", exc_info=True)
        self.instance.release()
        log.info("主进程已退出")


def open_running_instance(home: Path, open_browser: bool = True) -> int:
    """已有实例在运行：向它要一条一次性登录链接并打开（open_browser=False 时只打印"已在运行"）。"""
    try:
        ep = mcp_shim.verified_endpoint(home)  # 同样核对监听端口的就是记录里的主进程，才发令牌
    except mcp_shim.NotRunning:
        print("ScreenRecall 已经在运行，但找不到它的地址（可能还没启动完），请稍后再试。", file=sys.stderr)
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
    ap.add_argument("--port", type=int, default=None,
                    help=f"优先使用的端口（默认取配置 [v2.app] port，即 {DEFAULT_PORT}；被占用时自动换一个；0 = 任意）")
    ap.add_argument("--no-tray", action="store_true", help="不显示托盘图标（按 Ctrl+C 退出）")
    ap.add_argument("--no-browser", action="store_true", help="启动后不打开主页")
    ap.add_argument("--autostart", action="store_true", help="开机自启时使用：不打开主页")
    ap.add_argument("--fake-queries", action="store_true", help="MCP 用合成数据（开发 / 演示）")
    ap.add_argument("--no-legacy-web", action="store_true", help="不挂旧版 webapi")
    ap.add_argument("--dev-origin", action="append", default=[], help="开发时允许的前端地址")
    ap.add_argument("--no-children", action="store_true", help="不启动录制进程和空闲整理进程（只开网页和 MCP）")
    # 测试用：换掉子进程模块（真实窗口测试用替身，不录用户自己的屏幕）
    ap.add_argument("--recorder-module", default=RECORDER_MODULE, help=argparse.SUPPRESS)
    ap.add_argument("--worker-module", default=WORKER_MODULE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    home = paths.app_home()
    paths.ensure_private_dir(home)
    _setup_logging(home)
    store = not a.fake_queries
    cfg = None
    children: list[supervisor.ChildSpec] = []
    if store:
        from ..v2.config import load_v2

        cfg = load_v2(a.data_dir, home=home)
        if not a.no_children:
            children = build_children(cfg, home=home, recorder_module=a.recorder_module,
                                      worker_module=a.worker_module or None)
    port = a.port if a.port is not None else (cfg.app.port if cfg is not None else DEFAULT_PORT)
    opts = AppOptions(home=home, data_dir=Path(cfg.data_dir) if cfg else (Path(a.data_dir) if a.data_dir else None),
                      port=port, tray=not a.no_tray, open_browser=not (a.no_browser or a.autostart),
                      fake_queries=a.fake_queries, legacy_web=not a.no_legacy_web, extra_origins=tuple(a.dev_origin),
                      children=children, store=store, cfg=cfg,
                      idle_threshold_s=cfg.idle.threshold_s if cfg else 300.0)
    return MainApp(opts).run()
