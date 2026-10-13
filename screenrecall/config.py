"""运行配置：config.default.toml（提交）+ config.local.toml（本机）+ 环境变量。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CaptureConfig:
    interval_seconds: float
    recent_cache_size: int
    webp_quality: int
    webp_lossless: bool
    webp_method: int


@dataclass(frozen=True)
class OcrConfig:
    intra_op_threads: int
    inter_op_threads: int
    max_side_len: int
    max_attempts: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class Config:
    data_dir: Path
    capture: CaptureConfig
    ocr: OcrConfig

    @property
    def db_path(self) -> Path:
        return self.data_dir / "screenrecall.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(data_dir_override: str | os.PathLike | None = None) -> Config:
    raw = tomllib.loads((REPO_ROOT / "config.default.toml").read_text(encoding="utf-8"))
    local = REPO_ROOT / "config.local.toml"
    if local.exists():
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8")))

    data_dir = Path(
        data_dir_override
        or os.environ.get("SCREENRECALL_DATA_DIR")
        or raw["storage"]["data_dir"]
    )
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir

    cap, ocr = raw["capture"], raw["ocr"]
    capture = CaptureConfig(
        interval_seconds=float(cap["interval_seconds"]),
        recent_cache_size=int(cap["recent_cache_size"]),
        webp_quality=int(cap["webp_quality"]),
        webp_lossless=bool(cap["webp_lossless"]),
        webp_method=int(cap["webp_method"]),
    )
    ocr_cfg = OcrConfig(
        intra_op_threads=int(ocr["intra_op_threads"]),
        inter_op_threads=int(ocr["inter_op_threads"]),
        max_side_len=int(ocr["max_side_len"]),
        max_attempts=int(ocr["max_attempts"]),
        retry_backoff_seconds=float(ocr["retry_backoff_seconds"]),
    )
    if capture.interval_seconds <= 0:
        raise ValueError("capture.interval_seconds 必须大于 0")
    if not 0 <= capture.webp_quality <= 100:
        raise ValueError("capture.webp_quality 必须在 0–100")
    if capture.recent_cache_size < 0:
        raise ValueError("capture.recent_cache_size 不能为负")
    if ocr_cfg.max_attempts < 1:
        raise ValueError("ocr.max_attempts 至少为 1")
    return Config(data_dir=data_dir.resolve(), capture=capture, ocr=ocr_cfg)
