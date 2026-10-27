"""/api/v2 与 MCP 适配层测试共用的数据：临时数据目录 + 真实写线程 Writer 写入的合成内容（不含任何真实屏幕内容）。

本文件没有测试，只提供 build_env()。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from screenrecall.app import auth, server
from screenrecall.store.writer import LineIn
from screenrecall.timeutil import utc_ms
from screenrecall.v2 import web
from screenrecall.v2.adapters import StoreStack
from screenrecall.v2.config import load_v2
from screenrecall.v2.searchsvc import SearchService

PORT = 18733
TOKEN = "t" * 43
BASE = f"http://127.0.0.1:{PORT}"
POST_H = {"X-ScreenRecall": "1"}
APP = "notepad.exe"
TITLE = "report.txt - 记事本"
SENTINEL = "SENTINEL-ZX81-DELETE-ME"


def day_base() -> int:
    """今天本地 10:00（UTC 毫秒）：合成数据都放在今天，测试不受运行时刻影响。"""
    d = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


class FakeAutostart:
    def __init__(self):
        self.enabled = False
        self.commands: list = []

    def is_enabled(self):
        return self.enabled

    def enable(self, command=None):
        self.enabled = True
        self.commands.append(command)
        return command or "cmd"

    def disable(self):
        was, self.enabled = self.enabled, False
        return was


@dataclass
class Win:
    hwnd: int
    title: str
    process: str
    pid: int = 1
    width: int = 800
    height: int = 600


@dataclass
class Env:
    tmp: Path
    home: Path
    data: Path
    stack: StoreStack
    ctx: web.V2Context
    guard: auth.AuthGuard
    nonces: auth.NonceStore
    ids: dict = field(default_factory=dict)
    settings_seen: list = field(default_factory=list)


def synthetic_frame(w: int = 640, h: int = 400, seed: int = 1) -> np.ndarray:
    img = np.full((h, w, 3), 245, np.uint8)
    rng = np.random.default_rng(seed)
    for i in range(6):
        y = 30 + i * 50
        img[y:y + 14, 20:20 + int(rng.integers(200, 560))] = 30  # 假装是几行字
    return img


def fill(stack: StoreStack, t0: int) -> dict:
    """一个片段：两块（没拼上）、文字行、划过、带待 OCR 区域的证据帧、概括、两条重叠的锁屏缺口。"""
    w = stack.writer
    sid = w.segment_for(APP, TITLE, t0).result(5)
    b1 = w.block_new(sid, t0).result(5)
    texts = ["Quarterly REPORT draft", "第二行 组会安排在周三", "ＦＵＬＬＷＩＤＴＨ Text 全角"]
    lines = [LineIn(x, 20, 30 + i * 50, 600, 44 + i * 50) for i, x in enumerate(texts)]
    ids_fut = w.record_lines(b1, t0, (0, 400), lines)
    fid = w.save_frame(synthetic_frame(), captured_ms=t0, segment_id=sid, block_id=b1, pos_y=0, app=APP,
                       title=TITLE, line_ids=ids_fut, line_rects=[(ln.x0, ln.y0, ln.x1, ln.y1) for ln in lines],
                       ocr_rects=[(0, 300, 640, 400)])
    line_ids = ids_fut.result(5)
    w.skim(sid, b1, t0 + 2_000, t0 + 3_000, 400, 1600).result(5)
    b2 = w.block_new(sid, t0 + 5_000).result(5)
    l2 = w.record_lines(b2, t0 + 5_000, (0, 400), [LineIn("another block line", 20, 30, 600, 44)]).result(5)
    pid = w.piece_put(t0 - 1_000, t0 + 10 * 60_000, app=APP, title=TITLE, segment_ids=[sid],
                      task_label="写报告").result(5)
    w.summary_put("piece", str(pid), "整理季度报告\n第二行细节", model="test-model").result(5)
    w.gap(t0 - 30 * 60_000, t0 - 20 * 60_000, "locked", hwnd=1).result(5)
    w.gap(t0 - 25 * 60_000, t0 - 15 * 60_000, "locked", hwnd=2).result(5)
    w.gap(t0 + 60_000, t0 + 120_000, "minimised", hwnd=3).result(5)  # 在片段里面：不算整天的空档
    w.activity_start(t0, hwnd=10, pid=1, process=APP, title=TITLE).result(5)
    w.activity_end(t0 + 10 * 60_000).result(5)
    w.flush()
    return {"segment": sid, "blocks": (b1, b2), "lines": line_ids, "line2": l2[0], "frame": fid, "piece": pid,
            "piece_start": t0 - 1_000}


def build_env(tmp_path: Path, monkeypatch, *, search: SearchService | None = None, fill_data: bool = True,
              clock=utc_ms) -> Env:
    home, data = tmp_path / "home", tmp_path / "data"
    monkeypatch.setenv("SCREENRECALL_HOME", str(home))
    monkeypatch.delenv("SCREENRECALL_DATA_DIR", raising=False)
    home.mkdir(parents=True, exist_ok=True)
    cfg = load_v2(data, home=home)
    stack = StoreStack(cfg, search=search or SearchService(cfg.data_dir, model_check=lambda: False), clock=clock)
    stack.seed_control()
    nonces = auth.NonceStore()
    holder: dict = {}
    seen: list = []

    def rotate() -> str:
        token = "n" * 43
        holder["guard"].set_token(token)
        return auth.session_cookie(PORT, token)

    ctx = web.V2Context(stack, home, rotate_token=rotate, autostart=FakeAutostart(),
                        list_windows=lambda: [Win(11, "Synthetic window", "tk.exe"), Win(12, "", "hidden.exe")],
                        llm_env_files=[home / ".env"], llm_environ={}, on_settings=seen.append)
    guard = server.build_app(port=PORT, token=TOKEN, nonces=nonces, extra_routes=web.routes(ctx))
    holder["guard"] = guard
    env = Env(tmp_path, home, Path(cfg.data_dir), stack, ctx, guard, nonces, settings_seen=seen)
    if fill_data:
        env.ids = fill(stack, day_base())
    return env


def login(client, env: Env) -> None:
    r = client.get(f"/auth?n={env.nonces.mint()}")
    assert r.status_code == 303


def hours_ago(h: float) -> int:
    return int((datetime.now() - timedelta(hours=h)).timestamp() * 1000)
