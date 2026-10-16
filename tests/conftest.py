"""测试公共设施：在非 Windows 平台用假的 winapi 替身驱动 Recorder，并提供注入时钟。

这些测试验证采集节奏、复用、中断标记、模式与补拍的状态逻辑和数据库结果；
真实窗口捕获、DPI 与快捷键注册仍需在 Windows 上做端到端验证。
"""

from __future__ import annotations

import logging
import sys
import threading
import types
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------- 假 winapi ----------

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    process: str
    pid: int
    width: int
    height: int


@dataclass(frozen=True)
class Capture:
    bgr: bytes
    width: int
    height: int
    method: str
    rect: tuple
    dpi: int | None


class CaptureError(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class FakeScreen:
    """可编程的"屏幕"：测试改 pixels / title / locked / error，Recorder 通过假 winapi 读取。"""

    def __init__(self):
        self.hwnd = 100
        self.process = "notepad.exe"
        self.title = "test.txt - 记事本"
        self.width, self.height = 40, 30
        self.pixels = make_image(0)
        self.locked = False
        self.error: str | None = None
        self.alive = True
        self.captures = 0

    def info(self) -> WindowInfo:
        return WindowInfo(self.hwnd, self.title, self.process, 1, self.width, self.height)



def _install_fake_winapi() -> None:
    mod = types.ModuleType("screenrecall.winapi")
    mod.WindowInfo = WindowInfo
    mod.Capture = Capture
    mod.CaptureError = CaptureError

    def describe_window(hwnd):
        if not SCREEN.alive or hwnd != SCREEN.hwnd:
            return None
        return SCREEN.info()

    def capture_window(hwnd):
        if SCREEN.error:
            raise CaptureError(SCREEN.error)
        SCREEN.captures += 1
        h, w = SCREEN.pixels.shape[:2]
        return Capture(SCREEN.pixels.tobytes(), w, h, "print_window", (0, 0, w, h), 96)

    mod.describe_window = describe_window
    mod.capture_window = capture_window
    mod.is_input_desktop_locked = lambda: SCREEN.locked
    mod.list_windows = lambda: [SCREEN.info()]
    sys.modules["screenrecall.winapi"] = mod
    import screenrecall

    screenrecall.winapi = mod


_install_fake_winapi()

from screenrecall import control, db  # noqa: E402
from screenrecall.capture import Recorder  # noqa: E402
from screenrecall.config import load_config  # noqa: E402


def make_image(seed: int, w: int = 40, h: int = 30) -> np.ndarray:
    """确定性的随机 BGR 测试图。不同 seed 几乎所有像素都不同（大变化）。"""
    return np.random.default_rng(seed).integers(0, 255, size=(h, w, 3), dtype=np.uint8)


def tweak(img: np.ndarray, n_pixels: int, delta: int = 1) -> np.ndarray:
    """在左上角改动 n 个像素（模拟单个数字变化、光标闪烁等小变化）。"""
    out = img.copy()
    flat = out.reshape(-1, 3)
    flat[:n_pixels, 0] = (flat[:n_pixels, 0].astype(np.int16) + delta) % 256
    return out


SCREEN = FakeScreen()


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Harness:
    def __init__(self, tmp_path: Path, **capture_overrides):
        cfg = load_config(tmp_path / "data")
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        if capture_overrides:
            cfg = replace(cfg, capture=replace(cfg.capture, **capture_overrides))
        self.cfg = cfg
        self.conn = db.connect(cfg.db_path)
        db.init_schema(self.conn)
        control.on_daemon_start(self.conn, 1)
        self.clock = FakeClock()
        self.rec = Recorder(cfg, threading.Event(), threading.Event(), logging.getLogger("test"), clock=self.clock)
        self.screen = SCREEN

    # 页面侧动作 + 后台应用
    def request(self, desired: str = "recording", mode: str = "normal") -> None:
        s = self.screen
        control.send_request(self.conn, desired, hwnd=s.hwnd, process=s.process, title=s.title, mode=mode)
        self.rec.apply_request(self.conn, control.read(self.conn))

    def start(self, mode: str = "normal") -> None:
        self.request("recording", mode)

    def tick(self, seconds: float | None = None) -> None:
        """推进时钟一个采样间隔（或指定秒数）后执行一次自动采样。"""
        self.clock.advance(self.rec._interval() if seconds is None else seconds)
        self.rec.sample(self.conn)

    def show(self, img: np.ndarray) -> None:
        self.screen.pixels = img

    # 查询
    def frames(self) -> list:
        return self.conn.execute("SELECT * FROM frames ORDER BY captured_at_ms, rowid").fetchall()

    def occurrences(self) -> list:
        return self.conn.execute("SELECT * FROM occurrences ORDER BY first_seen_ms, rowid").fetchall()

    def gaps(self, reason: str | None = None) -> list:
        if reason:
            return self.conn.execute("SELECT * FROM gaps WHERE reason = ? ORDER BY gap_id", (reason,)).fetchall()
        return self.conn.execute("SELECT * FROM gaps ORDER BY gap_id").fetchall()

    def ctrl(self):
        return control.read(self.conn)


def _reset_screen_and_time(monkeypatch) -> None:
    SCREEN.__dict__.update(FakeScreen().__dict__)
    # 存档时间用毫秒；让每次 utc_ms 单调递增 1 秒，便于按时间排序断言
    counter = {"ms": 1_700_000_000_000}

    def fake_utc_ms():
        counter["ms"] += 1000
        return counter["ms"]

    import screenrecall.capture as capture_mod
    import screenrecall.control as control_mod

    monkeypatch.setattr(capture_mod, "utc_ms", fake_utc_ms)
    monkeypatch.setattr(control_mod, "utc_ms", fake_utc_ms)


@pytest.fixture
def harness_factory(tmp_path, monkeypatch):
    made: list[Harness] = []

    def make(**overrides) -> Harness:
        _reset_screen_and_time(monkeypatch)
        hn = Harness(tmp_path, **overrides)
        made.append(hn)
        return hn

    yield make
    for hn in made:
        hn.conn.close()


@pytest.fixture
def h(harness_factory) -> Harness:
    return harness_factory()
