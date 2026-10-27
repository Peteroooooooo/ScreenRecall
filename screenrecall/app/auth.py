"""本机访问令牌与网页 / MCP 的访问控制（v2 主进程）。

旧版只核对 Host 和一个固定请求头，本机任何程序带上这个头就能读出全部屏幕历史。v2 的做法：
- 每次安装生成一个随机令牌，和端口一起写进 app_home()/endpoint.json（只有本人能读）。
  脚本、MCP 转发程序用 Authorization: Bearer <令牌>；令牌不写进任何 Agent 配置文件。
- 浏览器拿不到令牌：主进程（托盘"打开主页"）现场生成一次性短期链接 /auth?n=<nonce>（约 30 秒、只能用一次），
  打开后设一个 HttpOnly + SameSite=Strict 的 Cookie 再跳回首页。
- AuthGuard 中间件包在整个应用外面：所有路径都核对 Host（防 DNS rebinding）、拒绝外站 Origin；
  除静态页面外（默认拒绝），都要求 Cookie 或 Bearer 令牌。旧版 webapi 的 X-ScreenRecall 头检查仍在里面一层生效。

已知限制：浏览器的 Cookie 不区分端口，用户用浏览器访问本机别的端口时这个 Cookie 也会被带上；
Cookie 名里带端口只能避免多个实例互相覆盖。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs

from . import paths

ENDPOINT_FILE = "endpoint.json"
NONCE_TTL_S = 30.0
COOKIE_MAX_AGE_S = 30 * 24 * 3600
PUBLIC_PATHS = ("/", "/index.html", "/favicon.ico", "/favicon.svg", "/robots.txt")
PUBLIC_PREFIXES = ("/assets/",)
AUTH_PATH = "/auth"


# ---------------- endpoint.json ----------------

@dataclass(frozen=True)
class Endpoint:
    """主进程对外的地址和令牌。repr 不显示令牌。"""
    port: int
    token: str = field(repr=False)
    pid: int | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def endpoint_path(home: Path | None = None) -> Path:
    return (home or paths.app_home()) / ENDPOINT_FILE


def read_endpoint(home: Path | None = None) -> Endpoint | None:
    """读 endpoint.json；文件不存在、内容不对时返回 None。"""
    try:
        raw = json.loads(endpoint_path(home).read_text(encoding="utf-8"))
        port, token = int(raw["port"]), str(raw["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (0 < port < 65536) or len(token) < 32:
        return None
    pid = raw.get("pid")
    return Endpoint(port, token, int(pid) if isinstance(pid, int) else None)


def load_or_create_token(home: Path | None = None) -> str:
    """沿用已有令牌（重启后浏览器 Cookie 仍然有效），没有或不合格时新生成一个。"""
    old = read_endpoint(home)
    return old.token if old else secrets.token_urlsafe(32)


def write_endpoint(ep: Endpoint, home: Path | None = None) -> Path:
    """写 endpoint.json（只有本人能读，原子替换）。"""
    data = json.dumps({"version": 1, "port": ep.port, "token": ep.token, "pid": ep.pid}, ensure_ascii=False)
    return paths.write_private_file(endpoint_path(home), data.encode("utf-8"))


# ---------------- 一次性链接 ----------------

class NonceStore:
    """一次性短期链接：mint() 生成，consume() 只成功一次，过期作废。线程安全。"""

    def __init__(self, ttl_s: float = NONCE_TTL_S, clock: Callable[[], float] = time.monotonic, cap: int = 64):
        self.ttl_s, self._clock, self._cap = ttl_s, clock, cap
        self._items: dict[str, float] = {}
        self._lock = threading.Lock()

    def mint(self) -> str:
        n = secrets.token_urlsafe(24)
        now = self._clock()
        with self._lock:
            self._items = {k: t for k, t in self._items.items() if t > now}
            while len(self._items) >= self._cap:  # 防止被反复调用撑大
                self._items.pop(next(iter(self._items)))
            self._items[n] = now + self.ttl_s
        return n

    def consume(self, nonce: str | None) -> bool:
        if not nonce:
            return False
        with self._lock:
            expires = self._items.pop(nonce, None)
        return expires is not None and expires > self._clock()


def home_url(port: int, nonce: str) -> str:
    return f"http://127.0.0.1:{port}{AUTH_PATH}?n={nonce}"


def cookie_value(token: str) -> str:
    """浏览器 Cookie 的值：由令牌派生（重启后仍有效），Cookie 泄露也拿不到令牌本身。"""
    return hmac.new(token.encode("utf-8"), b"screenrecall-browser-session-v1", hashlib.sha256).hexdigest()


def cookie_name(port: int) -> str:
    return f"sr_session_{port}"


# ---------------- ASGI 中间件 ----------------

def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookies(header: str) -> dict[str, str]:
    out = {}
    for part in header.split(";"):
        k, sep, v = part.strip().partition("=")
        if sep:
            out.setdefault(k.strip(), v.strip())
    return out


async def _send_simple(send, status: int, body: dict | None, extra: Iterable[tuple[bytes, bytes]] = ()) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
    headers = [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(data)).encode()),
               (b"cache-control", b"no-store"), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": data})


_SECURITY_HEADERS = [(b"x-frame-options", b"DENY"), (b"x-content-type-options", b"nosniff"),
                     (b"referrer-policy", b"no-referrer")]


class AuthGuard:
    """包在整个 ASGI 应用外层的访问控制（见模块说明）。

    public_paths / public_prefixes 是不需要登录的静态页面；其余路径一律要求 Cookie 或 Bearer 令牌。
    extra_origins 只给开发时的前端地址（如 http://127.0.0.1:5173）用。
    """

    def __init__(self, app, *, port: int, token: str, nonces: NonceStore, extra_origins: tuple[str, ...] = (),
                 public_paths: tuple[str, ...] = PUBLIC_PATHS, public_prefixes: tuple[str, ...] = PUBLIC_PREFIXES):
        if len(token) < 32:
            raise ValueError("令牌太短")
        self.app, self.port, self.nonces = app, port, nonces
        self._token = token
        self._cookie = cookie_value(token)
        self._cookie_name = cookie_name(port)
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts} | set(extra_origins)
        self.public_paths, self.public_prefixes = set(public_paths), public_prefixes

    def __repr__(self) -> str:
        return f"AuthGuard(port={self.port})"

    def is_public(self, path: str) -> bool:
        return path in self.public_paths or path.startswith(self.public_prefixes)

    def authorized(self, headers: dict[str, str]) -> bool:
        auth = headers.get("authorization", "")
        if auth[:7].lower() == "bearer " and hmac.compare_digest(auth[7:].strip().encode(), self._token.encode()):
            return True
        got = _cookies(headers.get("cookie", "")).get(self._cookie_name, "")
        return bool(got) and hmac.compare_digest(got.encode(), self._cookie.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = _headers(scope)
        path = scope.get("path", "")
        if headers.get("host") not in self.hosts:
            return await self._deny(scope, receive, send, 403, "只接受本机地址访问")
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:  # 包括 "null"（沙箱页面、file://）
            return await self._deny(scope, receive, send, 403, "不接受其他网页发来的请求")
        if scope["type"] == "http" and path == AUTH_PATH:
            return await self._login(scope, send)
        if not self.is_public(path) and not self.authorized(headers):
            return await self._deny(scope, receive, send, 401, "需要登录：请从托盘图标的“打开主页”进入",
                                    [(b"www-authenticate", b'Bearer realm="screenrecall"')])

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + _SECURITY_HEADERS
            await send(message)

        return await self.app(scope, receive, send_with_headers if scope["type"] == "http" else send)

    async def _deny(self, scope, receive, send, status: int, msg: str, extra=()) -> None:
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        await _send_simple(send, status, {"error": msg}, [*_SECURITY_HEADERS, *extra])

    async def _login(self, scope, send) -> None:
        if scope["method"] == "HEAD":  # 链接预览 / 安全软件先发 HEAD 探一下：不消耗一次性链接，也不发 Cookie
            return await _send_simple(send, 204, None, _SECURITY_HEADERS)
        if scope["method"] != "GET":
            return await _send_simple(send, 405, {"error": "不支持这种请求方法"})
        nonce = (parse_qs(scope.get("query_string", b"").decode("latin-1")).get("n") or [""])[0]
        if not self.nonces.consume(nonce):
            return await _send_simple(send, 403, {"error": "链接已失效（只能用一次、30 秒内有效），请从托盘图标重新打开主页"},
                                      _SECURITY_HEADERS)
        cookie = (f"{self._cookie_name}={self._cookie}; Path=/; Max-Age={COOKIE_MAX_AGE_S}; "
                  f"HttpOnly; SameSite=Strict")
        await _send_simple(send, 303, None, [(b"location", b"/"), (b"set-cookie", cookie.encode("latin-1")),
                                             *_SECURITY_HEADERS])


def bearer_headers(ep: Endpoint) -> dict[str, str]:
    return {"Authorization": f"Bearer {ep.token}"}
