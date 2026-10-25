"""运行配置：config.default.toml（提交）+ config.local.toml（本机）+ 环境变量。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .lifecycle import GIB, MIB, RetentionConfig
from .models import DEFAULT_MODELS, ModelsConfig, ModelSpec

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CaptureConfig:
    interval_seconds: float
    recent_cache_size: int
    webp_quality: int
    webp_lossless: bool
    webp_method: int
    stable_change_ratio: float = 0.01
    stable_timeout_seconds: float = 10.0
    debug_interval_seconds: float = 1.0
    debug_min_save_gap_seconds: float = 2.0
    debug_max_minutes: float = 30.0
    debug_exit_backlog: int = 50
    snapshot_hotkey: str = ""


@dataclass(frozen=True)
class RulesConfig:
    """允许/排除规则（方案 1.1）。进程名比较前统一小写。"""

    allowed_processes: tuple[str, ...] = ()
    excluded_processes: tuple[str, ...] = ()
    excluded_title_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class OcrConfig:
    intra_op_threads: int
    inter_op_threads: int
    max_side_len: int
    max_attempts: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class IndexConfig:
    ocr_high_water: int = 50
    embed_max_attempts: int = 3
    embed_retry_backoff_seconds: float = 10.0


@dataclass(frozen=True)
class SearchConfig:
    rrf_k: int = 60
    branch_top: int = 50


@dataclass(frozen=True)
class ClassifyConfig:
    model_dir: str = ""  # 训练产物目录（model.npz + card.json）；空表示不做预测，页面显示"待分类"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    capture: CaptureConfig
    ocr: OcrConfig
    rules: RulesConfig = RulesConfig()
    models: ModelsConfig = DEFAULT_MODELS
    index: IndexConfig = IndexConfig()
    search: SearchConfig = SearchConfig()
    retention: RetentionConfig = RetentionConfig()
    classify: ClassifyConfig = ClassifyConfig()

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
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8-sig")))

    data_dir = Path(
        data_dir_override
        or os.environ.get("SCREENRECALL_DATA_DIR")
        or raw["storage"]["data_dir"]
    )
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    # 数据目录自带的覆盖（最后合并）：只作用于这一份数据，用于独立的删除 / 配额测试库，不影响正式数据
    per_dir = data_dir / "config.toml"
    if per_dir.exists():
        override = tomllib.loads(per_dir.read_text(encoding="utf-8-sig"))  # utf-8-sig：容忍 PowerShell 5.1 写出的 BOM
        override.pop("storage", None)
        raw = _merge(raw, override)

    cap, ocr = raw["capture"], raw["ocr"]
    capture = CaptureConfig(
        interval_seconds=float(cap["interval_seconds"]),
        recent_cache_size=int(cap["recent_cache_size"]),
        webp_quality=int(cap["webp_quality"]),
        webp_lossless=bool(cap["webp_lossless"]),
        webp_method=int(cap["webp_method"]),
        stable_change_ratio=float(cap.get("stable_change_ratio", 0.01)),
        stable_timeout_seconds=float(cap.get("stable_timeout_seconds", 10.0)),
        debug_interval_seconds=float(cap.get("debug_interval_seconds", 1.0)),
        debug_min_save_gap_seconds=float(cap.get("debug_min_save_gap_seconds", 2.0)),
        debug_max_minutes=float(cap.get("debug_max_minutes", 30.0)),
        debug_exit_backlog=int(cap.get("debug_exit_backlog", 50)),
        snapshot_hotkey=str(cap.get("snapshot_hotkey", "")).strip(),
    )
    rules_raw = raw.get("rules", {})
    rules = RulesConfig(
        allowed_processes=tuple(str(x).strip().lower() for x in rules_raw.get("allowed_processes", []) if str(x).strip()),
        excluded_processes=tuple(str(x).strip().lower() for x in rules_raw.get("excluded_processes", []) if str(x).strip()),
        excluded_title_keywords=tuple(
            str(x).strip() for x in rules_raw.get("excluded_title_keywords", []) if str(x).strip()
        ),
    )
    ocr_cfg = OcrConfig(
        intra_op_threads=int(ocr["intra_op_threads"]),
        inter_op_threads=int(ocr["inter_op_threads"]),
        max_side_len=int(ocr["max_side_len"]),
        max_attempts=int(ocr["max_attempts"]),
        retry_backoff_seconds=float(ocr["retry_backoff_seconds"]),
    )
    m = raw.get("models", {})
    d = DEFAULT_MODELS

    def spec(prefix: str, default: ModelSpec) -> ModelSpec:
        return ModelSpec(str(m.get(f"{prefix}_repo", default.repo)), str(m.get(f"{prefix}_revision", default.revision)))

    models = ModelsConfig(
        text=spec("text", d.text),
        image=spec("image", d.image),
        clip_text=spec("clip_text", d.clip_text),
        threads=int(m.get("threads", d.threads)),
        batch_size=int(m.get("batch_size", d.batch_size)),
        local_files_only=bool(m.get("local_files_only", d.local_files_only)),
        enabled=bool(m.get("enabled", d.enabled)),
    )
    ix, sr = raw.get("index", {}), raw.get("search", {})
    index = IndexConfig(
        ocr_high_water=int(ix.get("ocr_high_water", 50)),
        embed_max_attempts=int(ix.get("embed_max_attempts", 3)),
        embed_retry_backoff_seconds=float(ix.get("embed_retry_backoff_seconds", 10.0)),
    )
    search = SearchConfig(rrf_k=int(sr.get("rrf_k", 60)), branch_top=int(sr.get("branch_top", 50)))
    rt = raw.get("retention", {})
    retention = RetentionConfig(
        image_days=int(rt.get("image_days", 30)),
        record_days=int(rt.get("record_days", 90)),
        quota_bytes=int(float(rt.get("quota_gib", 10)) * GIB),
        min_free_bytes=int(float(rt.get("min_free_gib", 2)) * GIB),
        reserve_bytes=int(float(rt.get("reserve_mib", 256)) * MIB),
        maintenance_interval_seconds=float(rt.get("maintenance_interval_seconds", 300)),
    )
    if not 0 < retention.image_days <= retention.record_days:
        raise ValueError("retention.image_days 必须大于 0 且不超过 record_days")
    if retention.quota_bytes <= 0 or retention.min_free_bytes < 0 or retention.reserve_bytes < 0:
        raise ValueError("retention.quota_gib 必须大于 0，min_free_gib、reserve_mib 不能为负")
    if models.threads < 1 or models.batch_size < 1 or index.embed_max_attempts < 1:
        raise ValueError("models.threads、models.batch_size、index.embed_max_attempts 至少为 1")
    if capture.interval_seconds <= 0:
        raise ValueError("capture.interval_seconds 必须大于 0")
    if not 0 <= capture.webp_quality <= 100:
        raise ValueError("capture.webp_quality 必须在 0–100")
    if not 0 <= capture.stable_change_ratio < 1:
        raise ValueError("capture.stable_change_ratio 必须在 [0, 1)")
    if capture.stable_timeout_seconds <= 0 or capture.debug_interval_seconds <= 0:
        raise ValueError("capture.stable_timeout_seconds 与 debug_interval_seconds 必须大于 0")
    if capture.debug_min_save_gap_seconds < 0 or capture.debug_max_minutes <= 0:
        raise ValueError("capture.debug_min_save_gap_seconds 不能为负，debug_max_minutes 必须大于 0")
    if capture.recent_cache_size < 0:
        raise ValueError("capture.recent_cache_size 不能为负")
    if ocr_cfg.max_attempts < 1:
        raise ValueError("ocr.max_attempts 至少为 1")
    return Config(
        data_dir=data_dir.resolve(), capture=capture, ocr=ocr_cfg, rules=rules,
        models=models, index=index, search=search, retention=retention,
        classify=ClassifyConfig(model_dir=str(raw.get("classify", {}).get("model_dir", "")).strip()),
    )
