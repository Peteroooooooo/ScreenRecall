"""主进程接线：子进程命令、store 模式的 MainApp（进程内启动，端口 0，无托盘、无子进程）。"""
from __future__ import annotations

import sys

import httpx
import pytest

from screenrecall.app import auth, main, mcp_server, supervisor
from screenrecall.v2 import adapters
from screenrecall.v2.config import load_v2


def test_build_children(tmp_path):
    cfg = load_v2(tmp_path / "data", home=tmp_path / "home")
    rec, worker = main.build_children(cfg, home=tmp_path / "home")
    assert rec.name == "recorder" and rec.restart == "always" and rec.autostart and not rec.below_normal
    assert rec.argv[:3] == [sys.executable, "-m", "screenrecall.v2.recorder"]
    assert rec.argv[3:] == ["--data-dir", str(cfg.data_dir), "--home", str(tmp_path / "home")]
    assert worker.name == main.IDLE_CHILD and worker.restart == "on-failure"
    assert worker.autostart is False and worker.below_normal is True
    assert worker.argv[2] == "screenrecall.v2.worker" and worker.argv[3:] == rec.argv[3:]
    assert "PYTHONPATH" in rec.env and rec.log_path.parent == cfg.logs_dir
    only = main.build_children(cfg, home=tmp_path / "home", recorder_module="x.y", worker_module=None)
    assert [c.name for c in only] == ["recorder"] and only[0].argv[2] == "x.y"


@pytest.fixture
def app(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    cfg = load_v2(tmp_path / "data", home=home)
    opts = main.AppOptions(home=home, port=0, tray=False, open_browser=False, legacy_web=False, store=True, cfg=cfg)
    a = main.MainApp(opts).start()
    try:
        yield a
    finally:
        a.shutdown()


def test_store_mode_wiring(app):
    assert isinstance(app.queries, adapters.StoreQueriesAdapter)
    assert isinstance(app.controller, adapters.StoreController)
    assert app.idle.threshold_s == app.stack.cfg.idle.threshold_s
    ep = app.endpoint
    with httpx.Client(base_url=ep.base_url, trust_env=False, timeout=10) as c:
        assert c.get("/api/v2/status").status_code == 401
        h = auth.bearer_headers(ep)
        s = c.get("/api/v2/status", headers=h).json()
        assert s["recording"] == "stopped" and s["idle_work"] == "idle"  # 新数据目录：期望录制，但没有录制进程
        assert app.stack.queries.control()["desired_state"] == "recording"
        r = c.post("/api/v2/settings", json={"idle_minutes": 3}, headers={**h, "X-ScreenRecall": "1"})
        assert r.status_code == 200 and app.idle.threshold_s == 180  # 空闲阈值立即生效
        # 换令牌：endpoint.json 里是新令牌，旧令牌失效
        r = c.post("/api/v2/agent/reset-token", json={}, headers={**h, "X-ScreenRecall": "1"})
        assert r.status_code == 200 and "set-cookie" in r.headers
        new = auth.read_endpoint(app.home)
        assert new.token != ep.token and new.pid == ep.pid
        assert c.get("/api/v2/status").status_code == 200  # 响应里的新 Cookie 有效
        c.cookies.clear()
        assert c.get("/api/v2/status", headers=h).status_code == 401
        assert c.get("/api/v2/status", headers=auth.bearer_headers(new)).status_code == 200


def test_store_mode_legacy_app_only_serves_pages(tmp_path, monkeypatch):
    """v2 模式下旧版 webapi 只发网页：/api/import 等旧接口会写进 v2 删除管不到的旧版库，一律 404。"""
    home = tmp_path / "home"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    cfg = load_v2(tmp_path / "data", home=home)
    opts = main.AppOptions(home=home, port=0, tray=False, open_browser=False, legacy_web=True, store=True, cfg=cfg)
    a = main.MainApp(opts).start()
    try:
        h = {**auth.bearer_headers(a.endpoint), "X-ScreenRecall": "1"}
        with httpx.Client(base_url=a.endpoint.base_url, trust_env=False, timeout=10) as c:
            r = c.post("/api/import", files={"files": ("a.png", b"x", "image/png")}, headers=h)
            assert r.status_code == 404 and "error" in r.json()
            assert c.post("/api/search", json={"q": "x"}, headers=h).status_code == 404
            assert c.get("/api/v2/status", headers=h).status_code == 200
        assert not (cfg.data_dir / "screenrecall.sqlite3").exists()
    finally:
        a.shutdown()


def test_fake_queries_mode_keeps_skeleton(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    a = main.MainApp(main.AppOptions(home=home, port=0, tray=False, open_browser=False, legacy_web=False,
                                     fake_queries=True)).start()
    try:
        assert isinstance(a.queries, mcp_server.FakeQueries) and a.stack is None
        with httpx.Client(base_url=a.endpoint.base_url, trust_env=False, timeout=10) as c:
            r = c.get("/api/v2/status", headers=auth.bearer_headers(a.endpoint))
            assert r.status_code == 404  # 没接 v2 存储
    finally:
        a.shutdown()


def test_supervisor_python_child_matches():
    assert supervisor.python_child("m", "a")[1:] == ["-m", "m", "a"]
