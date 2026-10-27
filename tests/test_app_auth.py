"""v2 主进程：令牌、一次性登录链接与 AuthGuard（DNS rebinding、外站 Origin、Cookie / Bearer），并包住旧版 webapi。"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from screenrecall import webapi
from screenrecall.app import auth, server
from screenrecall.config import load_config

PORT = 18611
BASE = f"http://127.0.0.1:{PORT}"
TOKEN = "t" * 43


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(tmp_path, clock):
    legacy = webapi.create_app(load_config(tmp_path / "data"), port=PORT)
    nonces = auth.NonceStore(clock=clock)
    app = server.build_app(port=PORT, token=TOKEN, nonces=nonces, legacy=legacy)
    with TestClient(app, base_url=BASE, follow_redirects=False) as c:
        c.nonces = nonces
        yield c


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ---------- endpoint.json ----------

def test_endpoint_roundtrip_keeps_token_private(tmp_path):
    assert auth.read_endpoint(tmp_path) is None
    tok = auth.load_or_create_token(tmp_path)
    assert len(tok) >= 32
    ep = auth.Endpoint(8611, tok, 42)
    assert tok not in repr(ep)
    auth.write_endpoint(ep, tmp_path)
    assert auth.read_endpoint(tmp_path) == ep
    assert auth.load_or_create_token(tmp_path) == tok  # 重启后沿用
    from screenrecall.app import paths
    assert paths.verify_private(tmp_path / "endpoint.json") == []


def test_bad_endpoint_file_is_ignored(tmp_path):
    (tmp_path / "endpoint.json").write_text(json.dumps({"port": 8611, "token": "short"}))
    assert auth.read_endpoint(tmp_path) is None
    assert auth.load_or_create_token(tmp_path) != "short"


# ---------- 一次性链接 ----------

def test_nonce_single_use_and_expiry(clock):
    ns = auth.NonceStore(ttl_s=30, clock=clock)
    a, b = ns.mint(), ns.mint()
    assert ns.consume(a) and not ns.consume(a)
    clock.t += 31
    assert not ns.consume(b)
    assert not ns.consume("") and not ns.consume(None) and not ns.consume("made-up")


def test_nonce_store_is_capped(clock):
    ns = auth.NonceStore(clock=clock, cap=4)
    first = ns.mint()
    for _ in range(10):
        ns.mint()
    assert not ns.consume(first)


# ---------- 中间件 ----------

def test_dns_rebinding_host_rejected_on_every_path(client):
    for path in ("/", "/api/meta", "/mcp", "/auth?n=x"):
        r = client.get(path, headers={"Host": f"evil.example:{PORT}"} | bearer())
        assert r.status_code == 403, path
    r = client.get("/api/meta", headers={"Host": "127.0.0.1:9999"} | bearer())
    assert r.status_code == 403


def test_foreign_origin_rejected_even_with_token(client):
    for origin in ("http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}"):
        r = client.get("/api/meta", headers={"Origin": origin} | bearer())
        assert r.status_code == 403, origin
    assert client.get("/api/meta", headers={"Origin": BASE} | bearer()).status_code == 200


def test_api_requires_credentials(client):
    r = client.get("/api/meta")
    assert r.status_code == 401
    assert "www-authenticate" in r.headers
    assert client.get("/api/meta", headers=bearer("x" * 43)).status_code == 401
    assert client.get("/api/meta", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    r = client.get("/api/meta", headers=bearer())
    assert r.status_code == 200 and r.json()["db_ok"] is False  # 旧版 webapi 照常应答（还没有数据库）
    assert r.headers["x-frame-options"] == "DENY"


def test_unknown_paths_are_denied_by_default(client):
    assert client.get("/anything").status_code == 401
    assert client.get("/mcp").status_code == 401
    assert client.get("/").status_code != 401  # 静态首页不需要登录（这里没有构建产物，所以是 404）


def test_nonce_login_sets_strict_httponly_cookie(client):
    n = client.nonces.mint()
    r = client.get(f"/auth?n={n}")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert TOKEN not in cookie  # Cookie 里不是令牌本身
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/api/meta").status_code == 200  # TestClient 带上了 Cookie
    client.cookies.clear()
    assert client.get("/api/meta").status_code == 401


def test_reused_missing_and_expired_nonce(client):
    n = client.nonces.mint()
    assert client.get(f"/auth?n={n}").status_code == 303
    client.cookies.clear()
    assert client.get(f"/auth?n={n}").status_code == 403  # 第二次
    assert client.get("/auth").status_code == 403         # 没带
    n2 = client.nonces.mint()
    client.nonces._clock.t += auth.NONCE_TTL_S + 1
    r = client.get(f"/auth?n={n2}")
    assert r.status_code == 403 and "set-cookie" not in r.headers
    assert client.post(f"/auth?n={client.nonces.mint()}").status_code == 405


def test_head_probe_does_not_burn_the_nonce(client):
    n = client.nonces.mint()
    r = client.head(f"/auth?n={n}")
    assert r.status_code == 204 and "set-cookie" not in r.headers
    assert client.get(f"/auth?n={n}").status_code == 303


def test_wrong_cookie_and_other_port_cookie_rejected(client):
    client.cookies.set(auth.cookie_name(PORT), "0" * 64)
    assert client.get("/api/meta").status_code == 401
    client.cookies.clear()
    client.cookies.set(auth.cookie_name(PORT + 1), auth.cookie_value(TOKEN))
    assert client.get("/api/meta").status_code == 401


def test_legacy_mutation_header_check_still_applies(client):
    r = client.post("/api/control", json={"action": "pause"}, headers=bearer())
    assert r.status_code == 403 and "X-ScreenRecall" in r.json()["error"]


def test_nonce_endpoint_for_second_instance(client):
    assert client.post("/api/app/nonce").status_code == 401
    r = client.post("/api/app/nonce", headers=bearer())
    url = r.json()["url"]
    assert url.startswith(f"{BASE}/auth?n=")
    assert client.get(url.removeprefix(BASE)).status_code == 303


def test_agent_configs_endpoint_needs_login_and_has_no_token(client):
    assert client.get("/api/app/agent-configs").status_code == 401
    r = client.get("/api/app/agent-configs", headers=bearer())
    assert r.status_code == 200
    body = r.json()
    assert set(body["configs"]) == {"claude_desktop", "claude_code", "cursor"}
    assert body["mcp_url"] == f"{BASE}/mcp" and TOKEN not in r.text


def test_lifespan_passes_through(tmp_path):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                await send({"type": msg["type"] + ".complete"})
                if msg["type"] == "lifespan.shutdown":
                    return

    guard = auth.AuthGuard(inner, port=PORT, token=TOKEN, nonces=auth.NonceStore())
    with TestClient(guard, base_url=BASE):
        pass
    assert seen == ["lifespan"]


def test_short_token_rejected():
    with pytest.raises(ValueError):
        auth.AuthGuard(lambda *a: None, port=PORT, token="abc", nonces=auth.NonceStore())
