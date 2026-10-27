"""主进程：现在就整理（启动空闲整理子进程）、空间上限定时检查、--home、马上写概括的接线。"""
from __future__ import annotations

import sys
import time

import httpx
import numpy as np
import pytest

from screenrecall.app import auth, main, supervisor
from screenrecall.store.writer import Writer
from screenrecall.v2 import config as v2config
from screenrecall.v2.config import load_v2
from screenrecall.v2.summarize_now import SummaryRunner


def _app(tmp_path, monkeypatch, children=()):
    # 不看真的键鼠空闲：这台电脑正好空闲时空闲监视会自己拉起整理进程
    monkeypatch.setattr(main.idle.IdleWatcher, "start", lambda self: self)
    home = tmp_path / "home"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    cfg = load_v2(tmp_path / "data", home=home)
    opts = main.AppOptions(home=home, port=0, tray=False, open_browser=False, legacy_web=False, store=True, cfg=cfg,
                           children=list(children))
    return main.MainApp(opts).start()


def test_idle_run_now_starts_the_idle_worker_child(tmp_path, monkeypatch):
    # 替身子进程：读到 stdin 关闭就退出（和真的空闲整理进程一样被主进程叫停）
    stub = supervisor.ChildSpec(main.IDLE_CHILD, [sys.executable, "-c", "import sys; sys.stdin.read()"],
                                restart="on-failure", autostart=False, below_normal=True)
    a = _app(tmp_path, monkeypatch, [stub])
    try:
        assert isinstance(a.summaries, SummaryRunner) and a.storage_cap is not None
        h = {**auth.bearer_headers(a.endpoint), "X-ScreenRecall": "1"}
        with httpx.Client(base_url=a.endpoint.base_url, trust_env=False, timeout=10) as c:
            assert c.get("/api/v2/status", headers=h).json()["idle_work"] == "idle"
            s = c.post("/api/v2/idle/run", json={}, headers=h).json()
            assert s["idle_work"] == "running" and a.sup.pid(main.IDLE_CHILD) is not None
            # 再点一次：已经在跑，不会开第二个
            pid = a.sup.pid(main.IDLE_CHILD)
            assert c.post("/api/v2/idle/run", json={}, headers=h).json()["idle_work"] == "running"
            assert a.sup.pid(main.IDLE_CHILD) == pid
    finally:
        a.shutdown()
    assert a.sup.pid(main.IDLE_CHILD) is None


def test_idle_run_now_without_children_is_503(tmp_path, monkeypatch):
    a = _app(tmp_path, monkeypatch)
    try:
        h = {**auth.bearer_headers(a.endpoint), "X-ScreenRecall": "1"}
        with httpx.Client(base_url=a.endpoint.base_url, trust_env=False, timeout=10) as c:
            assert c.post("/api/v2/idle/run", json={}, headers=h).status_code == 503
    finally:
        a.shutdown()


def test_storage_cap_check_in_main(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    cfg = load_v2(tmp_path / "data", home=home)
    now = int(time.time() * 1000)
    with Writer(cfg.data_dir) as w:
        for i in range(3):
            w.save_frame(np.random.default_rng(i).integers(0, 255, (60, 80, 3), dtype=np.uint8),
                         captured_ms=now - 3_600_000 + i)
        w.flush()
    from screenrecall.v2.adapters import StoreStack

    stack = StoreStack(cfg)
    try:
        clock = [0.0]
        cap = main.StorageCap(stack, clock=lambda: clock[0])
        assert cap.tick() == 0  # 启动后先等一分钟
        clock[0] = 61.0
        assert cap.tick() == 0  # 没设上限
        v2config.write_user_settings(cfg.data_dir, {"storage_cap_gb": 1e-9})  # 页面上改了：下一次就用
        clock[0] = 62.0
        assert cap.tick() == 0  # 还没到下一次检查
        clock[0] = 61.0 + 600
        assert cap.tick() == 3
        cap.close()
    finally:
        stack.close()


def test_home_flag_sets_the_app_home(tmp_path, monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_run(self):
        seen["home"] = self.home
        seen["data"] = self.data_dir
        raise Stop()

    monkeypatch.setenv("SCREENRECALL_HOME", str(tmp_path / "other"))  # 测试结束时还原
    monkeypatch.delenv("SCREENRECALL_DATA_DIR", raising=False)
    monkeypatch.setattr(main.MainApp, "run", fake_run)
    monkeypatch.setattr(main, "_setup_logging", lambda home: None)
    with pytest.raises(Stop):
        main.main(["--home", str(tmp_path / "h"), "--no-children", "--no-tray", "--no-browser"])
    assert seen["home"] == (tmp_path / "h").resolve() and seen["data"] == (tmp_path / "h" / "data").resolve()
