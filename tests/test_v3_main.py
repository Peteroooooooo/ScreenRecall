"""v3 主进程接线：子进程命令、配置开关、存储模式的 MainApp（进程内启动，端口 0，无托盘、无子进程），
在 v3.demo 写的临时库上打 /api/v3 和 /mcp（Bearer 令牌，和别的 app 测试一样）。"""
from __future__ import annotations

import sys
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from screenrecall.app import auth, main, mcp_server
from screenrecall.v3 import demo
from screenrecall.v3.config import build
from screenrecall.v3.controller import TrayController
from screenrecall.v3.mcp_queries import McpQueries

DAY = date(2026, 10, 28)
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(method, params=None, i=1):
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}


def test_build_children(tmp_path):
    cfg = build({}, data_dir=tmp_path / "data", home=tmp_path / "home")
    rec, worker = main.build_children(cfg, home=tmp_path / "home")
    assert rec.name == main.RECORDER_CHILD and rec.restart == "always" and rec.autostart and not rec.below_normal
    assert rec.argv[:3] == [sys.executable, "-m", "screenrecall.v3.recorder"]
    assert rec.argv[3:] == ["--data-dir", str(cfg.data_dir), "--home", str(tmp_path / "home")]
    assert worker.name == main.IDLE_CHILD and worker.restart == "on-failure"
    assert worker.autostart is False and worker.below_normal is True
    assert worker.argv[2] == "screenrecall.v3.worker" and worker.argv[3:] == rec.argv[3:]
    assert rec.log_path.parent == cfg.logs_dir and worker.env["TQDM_DISABLE"] == "1"


def test_app_switches_read_v3_config(tmp_path):
    cfg = build({"tray": False}, data_dir=tmp_path, home=tmp_path)
    args = SimpleNamespace(no_tray=False, no_browser=False, autostart=False)
    assert main.app_switches(cfg, args) == (False, True)
    assert main.app_switches(cfg, SimpleNamespace(no_tray=False, no_browser=False, autostart=True)) == (False, False)
    assert main.app_switches(None, args) == (True, True)  # --fake-queries：没有配置，按默认


@pytest.fixture
def app(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    demo.run(data, DAY)
    cfg = build({"idle_s": 120.0}, data_dir=data, home=home)
    opts = main.AppOptions(home=home, port=0, tray=False, open_browser=False, legacy_web=True, store=True, cfg=cfg)
    a = main.MainApp(opts).start()
    try:
        yield a
    finally:
        a.shutdown()


def test_store_mode_serves_v3_api_and_mcp(app):
    assert isinstance(app.queries, McpQueries) and isinstance(app.controller, TrayController)
    assert app.idle.threshold_s == 120.0
    ep = app.endpoint
    with httpx.Client(base_url=ep.base_url, trust_env=False, timeout=15) as c:
        assert c.get("/api/v3/status").status_code == 401
        h = auth.bearer_headers(ep)
        st = c.get("/api/v3/status", headers=h).json()
        assert st["state"] == "stopped" and st["recorder_alive"] is False  # 演示库：没有录制进程
        tl = c.get("/api/v3/timeline", params={"day": DAY.isoformat()}, headers=h).json()
        kinds = [it["type"] for it in tl["items"]]
        assert kinds.count("entry") == 3 and kinds.count("grey") == 1
        # 旧版 webapi 只发网页：它的 /api/* 一律 404；/api/v3 没被拦
        assert c.post("/api/search", json={"q": "x"}, headers={**h, "X-ScreenRecall": "1"}).status_code == 404
        assert c.get("/api/v3/nope", headers=h).status_code == 404
        # 托盘暂停 → 网页状态马上看得到（没有录制进程时 state 仍是 stopped，暂停记在 paused_*）；继续后清掉
        app.controller.pause_until(None)
        assert c.get("/api/v3/status", headers=h).json()["paused_forever"] is True
        assert "已暂停" in app.controller.status().text
        app.controller.resume()
        assert c.get("/api/v3/status", headers=h).json()["paused_until_ms"] is None

        mh = MCP_HEADERS | h | {"Origin": ep.base_url}
        init = _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "1"}})
        assert c.post("/mcp", json=init, headers=MCP_HEADERS).status_code == 401
        r = c.post("/mcp", json=init, headers=mh)
        assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "ScreenRecall"
        r = c.post("/mcp", json=_rpc("tools/list", i=2), headers=mh | {"MCP-Protocol-Version": "2025-06-18"})
        assert [t["name"] for t in r.json()["result"]["tools"]] == ["search", "read", "timeline", "summary",
                                                                     "get_frame", "copied"]
    assert not (app.data_dir / "screenrecall.sqlite3").exists()  # 旧版库没被建出来


def test_fake_queries_mode_has_no_v3_api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    a = main.MainApp(main.AppOptions(home=home, port=0, tray=False, open_browser=False, legacy_web=False,
                                     fake_queries=True)).start()
    try:
        assert isinstance(a.queries, mcp_server.FakeQueries) and a.store is None
        with httpx.Client(base_url=a.endpoint.base_url, trust_env=False, timeout=10) as c:
            assert c.get("/api/v3/status", headers=auth.bearer_headers(a.endpoint)).status_code == 404
    finally:
        a.shutdown()
