"""后台启动前的环境检查（方案 2.3）：缺什么就明确指出，不把启动失败显示成正常录制。"""

from __future__ import annotations

import platform
import sqlite3
import sys
import time
from dataclasses import dataclass, field

from .config import Config


@dataclass
class Check:
    name: str
    level: str  # 'ok' | 'warn' | 'fail'
    detail: str


@dataclass
class EnvReport:
    checks: list[Check] = field(default_factory=list)
    engine: object | None = None  # 通过检查后的 OcrEngine，直接交给后台复用

    @property
    def ok(self) -> bool:
        return all(c.level != "fail" for c in self.checks)

    def render(self) -> str:
        mark = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}
        return "\n".join(f"{mark[c.level]} {c.name}: {c.detail}" for c in self.checks)


def run_checks(cfg: Config, dpi_mode: str) -> EnvReport:
    rep = EnvReport()
    add = lambda name, level, detail: rep.checks.append(Check(name, level, detail))  # noqa: E731

    add("Windows", "ok" if sys.platform == "win32" else "fail", platform.platform())
    add("Python >= 3.11", "ok" if sys.version_info >= (3, 11) else "fail", sys.version.split()[0])
    add(
        "DPI 感知",
        "ok" if dpi_mode.startswith("per_monitor") else "warn",
        dpi_mode + ("" if dpi_mode.startswith("per_monitor") else "（缩放非 100% 时截图可能模糊或错位）"),
    )

    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.data_dir / ".write-test"
        probe.write_bytes(b"ok")
        probe.unlink()
        add("数据目录可写", "ok", str(cfg.data_dir))
    except OSError as exc:
        add("数据目录可写", "fail", f"{cfg.data_dir}：{exc}")

    try:
        mem = sqlite3.connect(":memory:")
        mem.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize = 'trigram case_sensitive 1')")
        mem.execute("SELECT json_each.value FROM json_each('[1]')").fetchall()
        mem.close()
        add("SQLite FTS5 trigram / JSON", "ok", sqlite3.sqlite_version)
    except sqlite3.Error as exc:
        add("SQLite FTS5 trigram / JSON", "fail", f"{sqlite3.sqlite_version}：{exc}（需要 3.34+ 且启用 FTS5）")

    try:
        from PIL import features

        add("Pillow WebP", "ok" if features.check("webp") else "fail", "可编码" if features.check("webp") else "缺少 WebP 支持")
    except ImportError as exc:
        add("Pillow WebP", "fail", str(exc))

    try:
        from . import winapi

        locked = winapi.is_input_desktop_locked()
        add("输入桌面", "warn" if locked else "ok", "当前判断为锁屏/安全桌面，将不会采集" if locked else "可访问")
    except Exception as exc:  # noqa: BLE001
        add("输入桌面", "fail", f"{type(exc).__name__}: {exc}")

    try:
        from PIL import Image, ImageDraw, ImageFont

        from .ocr import OcrEngine

        t0 = time.perf_counter()
        engine = OcrEngine(cfg.ocr)
        load_ms = int((time.perf_counter() - t0) * 1000)
        img = Image.new("RGB", (360, 90), "white")
        ImageDraw.Draw(img).text((12, 20), "OCR 2026", fill="black", font=ImageFont.load_default(size=40))
        t1 = time.perf_counter()
        boxes = engine.recognize(img)
        run_ms = int((time.perf_counter() - t1) * 1000)
        text = " ".join(b.text for b in boxes)
        level = "ok" if "2026" in text else "warn"
        add("RapidOCR", level, f"{engine.version}；加载 {load_ms} ms，冒烟识别 {run_ms} ms → {text!r}")
        rep.engine = engine
    except Exception as exc:  # noqa: BLE001
        add("RapidOCR", "fail", f"{type(exc).__name__}: {exc}")

    return rep
