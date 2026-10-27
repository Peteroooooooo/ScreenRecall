"""主进程的网页服务：把旧版 webapi、/mcp 和登录入口拼成一个 ASGI 应用，外面包 AuthGuard，在线程里跑 uvicorn。

路由（AuthGuard 之内）：
- /auth?n=...         一次性登录链接（AuthGuard 自己处理）
- /mcp                MCP streamable HTTP（Bearer 令牌；浏览器 Cookie 也可以，但 MCP 只核对本服务自己的 Origin）
- /api/app/nonce      POST，给第二个启动的实例要一条登录链接（"已经在运行，打开它的主页"）
- /api/app/agent-configs  GET，"连接 Agent"页面用的配置片段（mcp_shim.agent_configs()，不含令牌）
- 其他                交给旧版 webapi.create_app 返回的应用（它自己还会核对 Host 和 X-ScreenRecall 头）
"""
from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from typing import Any, Sequence

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route, Router

from . import auth

log = logging.getLogger("screenrecall.app.server")


class _Asgi:
    """把 async (scope, receive, send) 函数包成 ASGI 应用对象（Route 对普通函数会当成请求处理函数）。"""

    def __init__(self, fn):
        self.fn = fn

    async def __call__(self, scope, receive, send):
        await self.fn(scope, receive, send)


def _inner_lifespan(app) -> Any:
    """找到被中间件包着的 Starlette，取它的 lifespan（旧版 webapi 的模型预加载在里面）。"""
    seen = 0
    while app is not None and seen < 8:
        router = getattr(app, "router", None)
        if router is not None and hasattr(router, "lifespan_context"):
            return router.lifespan_context(app)
        app, seen = getattr(app, "app", None), seen + 1
    return None


def build_app(*, port: int, token: str, nonces: auth.NonceStore, mcp=None, legacy=None,
              extra_routes: Sequence[BaseRoute] = (), extra_origins: tuple[str, ...] = ()) -> auth.AuthGuard:
    """拼出整个应用。mcp 是 mcp_server.build_mcp 的结果（None = 不开 MCP）；legacy 是旧版 webapi 应用。"""
    routes: list[BaseRoute] = list(extra_routes)

    async def nonce(request: Request) -> JSONResponse:
        return JSONResponse({"url": auth.home_url(port, nonces.mint())}, headers={"Cache-Control": "no-store"})

    async def agent_configs(request: Request) -> JSONResponse:
        from . import mcp_shim  # 只用它的配置片段函数（不含令牌）

        return JSONResponse({"configs": mcp_shim.agent_configs(), "mcp_url": f"http://127.0.0.1:{port}/mcp"},
                            headers={"Cache-Control": "no-store"})

    routes.append(Route("/api/app/nonce", nonce, methods=["POST"]))
    routes.append(Route("/api/app/agent-configs", agent_configs, methods=["GET"]))
    if mcp is not None:
        mcp.streamable_http_app()  # 创建 session manager（只建一次）
        routes.append(Route("/mcp", endpoint=_Asgi(mcp.session_manager.handle_request)))

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with contextlib.AsyncExitStack() as stack:
            if mcp is not None:
                await stack.enter_async_context(mcp.session_manager.run())
            inner = _inner_lifespan(legacy) if legacy is not None else None
            if inner is not None:
                await stack.enter_async_context(inner)
            yield

    router = Router(routes=routes, default=legacy, lifespan=lifespan)
    return auth.AuthGuard(router, port=port, token=token, nonces=nonces, extra_origins=extra_origins)


def bind_socket(preferred_port: int = 0) -> socket.socket:
    """绑定 127.0.0.1 上的端口：先试 preferred_port，被占用就让系统分一个。
    Windows 上设 SO_EXCLUSIVEADDRUSE，别的程序不能抢绑同一个端口来冒充本服务。"""
    for p in ([preferred_port, 0] if preferred_port else [0]):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", p))
        except OSError:
            s.close()
            if p == 0:
                raise
            log.info("端口 %d 已被占用，改用系统分配的端口", p)
            continue
        s.listen(128)
        s.set_inheritable(False)
        return s
    raise OSError("无法绑定端口")  # pragma: no cover


class ServerThread:
    """在后台线程里跑 uvicorn（用已经绑好的 socket，端口在启动前就确定）。"""

    def __init__(self, app, sock: socket.socket, log_level: str = "warning"):
        import uvicorn

        self.sock = sock
        self.port = sock.getsockname()[1]
        # log_config=None：沿用主进程的日志设置（pythonw 下没有标准错误，uvicorn 自带的配置会写到空流）
        self.server = uvicorn.Server(uvicorn.Config(app, log_level=log_level, lifespan="on", log_config=None))
        self.thread = threading.Thread(target=self._run, name="screenrecall-web", daemon=True)
        self.error: BaseException | None = None

    def _run(self) -> None:
        try:
            self.server.run(sockets=[self.sock])
        except BaseException as exc:  # noqa: BLE001 - 交给 start() 报告
            self.error = exc
            log.exception("网页服务异常退出")

    def start(self, timeout: float = 15.0) -> "ServerThread":
        self.thread.start()
        t0 = time.monotonic()
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() - t0 > timeout:
                raise RuntimeError(f"网页服务没能启动：{self.error!r}")
            time.sleep(0.02)
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        try:
            self.sock.close()
        except OSError:
            pass
