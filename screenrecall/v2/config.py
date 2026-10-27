"""v2 的运行配置：config.default.toml 的 [v2.*] 各节 + config.local.toml（本机覆盖）+ 环境变量。

各模块的参数本来就定义成自己的 dataclass（RecorderConfig、UiaConfig、WriterConfig……），这里只负责把
TOML 里同名的键填进去；没写的键用 dataclass 默认值，写错名字的键直接报错，免得悄悄不生效。

数据目录：--data-dir > SCREENRECALL_DATA_DIR > [v2] data_dir > %LOCALAPPDATA%\\ScreenRecall\\data。
老的 screenrecall.sqlite3（冻结评测用）在老数据目录里，v2 不读也不写它。
"""

from __future__ import annotations

import dataclasses
import json
import os
import tomllib
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, _merge
from ..app.paths import app_home
from ..rec.core import RecorderConfig
from ..store.writer import WriterConfig
from ..text.clipbatch import ClipBatchConfig
from ..text.uia import UiaConfig


@dataclass(frozen=True)
class OcrSettings:
    model: str = "tiny"            # tiny（默认）/ small（更准更慢，留给第二遍）
    second_pass: bool = True       # tiny 置信度低的区域再用 small 读一遍
    second_pass_below: float = 0.80
    threads: int = 2
    models_dir: str = ""           # 空 = <仓库>/models/ocr
    batch_regions: int = 4         # 一次领几个待识别区域


@dataclass(frozen=True)
class EncoderSettings:
    threads: int = 2
    batch_size: int = 16
    idle_unload_s: float = 300.0   # 主进程里的查询模型多久不用就卸载


@dataclass(frozen=True)
class IdleSettings:
    threshold_s: float = 300.0     # 键鼠多久没动算空闲（锁屏立即算）
    piece_gap_ms: int = 120_000    # 同应用同标题、离开不超过这么久，算同一个片段
    summaries: bool = True         # 空闲时用大模型写片段 / 天概括
    clip: bool = True              # 空闲时给证据图算 CLIP 向量（内容分类器要用）
    # 以下给空闲整理进程（screenrecall.v2.worker）
    chunk_lines: int = 3           # 文字向量：每行一个，另外相邻最多这么多行拼一个短段落（同一块里阅读顺序相邻；1 = 不要段落）
    chunk_tokens: int = 100        # 一个段落最多多少 token（模型上限 128）
    llm_min_interval_s: float = 1.0  # 两次大模型调用至少间隔多久
    llm_max_calls: int = 200       # 一次空闲整理最多调用大模型几次
    llm_reasoning_effort: str = "none"  # 思考强度；空 = 用模型默认
    classifier_dir: str = "results/classifier/v1"  # 内容分类器（CLIP 向量 + 线性模型）；空 = 不预测
    frame_report_days: int = 30    # 只统计（不删除）早于这么多天、没标"保留"的证据帧，留给以后的保留期设置
    encoder_threads: int = 4       # 空闲时算文字向量的线程数（实测 3000 行：2 线程 215 行/秒，4 线程 373，8 线程 517）


@dataclass(frozen=True)
class AppSettings:
    port: int = 8611
    open_browser: bool = True
    tray: bool = True


@dataclass(frozen=True)
class V2Config:
    data_dir: Path
    home: Path
    app: AppSettings = field(default_factory=AppSettings)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    uia: UiaConfig = field(default_factory=UiaConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    encoder: EncoderSettings = field(default_factory=EncoderSettings)
    clip: ClipBatchConfig = field(default_factory=ClipBatchConfig)
    idle: IdleSettings = field(default_factory=IdleSettings)
    # 第一次建库时写进 control 的默认排除规则（之后以页面上改的为准）
    default_excluded_processes: tuple[str, ...] = ()
    default_excluded_title_keywords: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        from ..store.schema import DB_NAME
        return self.data_dir / DB_NAME

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def ocr_models_dir(self) -> Path | None:
        return Path(self.ocr.models_dir) if self.ocr.models_dir else None


def _build(cls: type, raw: dict[str, Any], where: str):
    """按 dataclass 字段名填值；嵌套的 dataclass 递归；tuple 字段把列表转成 tuple。"""
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - names)
    if unknown:
        raise ValueError(f"配置 [{where}] 里有不认识的键：{', '.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        hint = hints.get(key)
        if dataclasses.is_dataclass(hint) and isinstance(value, dict):
            kwargs[key] = _build(hint, value, f"{where}.{key}")
        elif isinstance(value, list):
            kwargs[key] = tuple(value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def _raw() -> dict:
    raw = tomllib.loads((REPO_ROOT / "config.default.toml").read_text(encoding="utf-8"))
    local = REPO_ROOT / "config.local.toml"
    if local.exists():
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8-sig")))
    return raw


# ---------------- 页面上改的设置（<数据目录>/user_settings.json） ----------------
# 设置页能改、又不在 control 表里的几项。放在数据目录里：录制进程、空闲整理进程用 load_v2(数据目录)
# 读配置时自动套上，不用另外传。比 config.default.toml / config.local.toml 优先。
USER_SETTINGS_FILE = "user_settings.json"
OCR_MODELS = ("tiny", "small")


def user_settings_path(data_dir: str | os.PathLike) -> Path:
    return Path(data_dir) / USER_SETTINGS_FILE


def read_user_settings(data_dir: str | os.PathLike) -> dict[str, Any]:
    """{idle_minutes: float, ocr_model: 'tiny'|'small'} 中写过的几项；文件不存在或内容不对时返回空 dict。"""
    try:
        raw = json.loads(user_settings_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Any] = {}
    if isinstance(raw, dict):
        m = raw.get("idle_minutes")
        if isinstance(m, (int, float)) and not isinstance(m, bool) and 1 <= m <= 24 * 60:
            out["idle_minutes"] = float(m)
        if raw.get("ocr_model") in OCR_MODELS:
            out["ocr_model"] = raw["ocr_model"]
    return out


def write_user_settings(data_dir: str | os.PathLike, updates: dict[str, Any]) -> dict[str, Any]:
    """合并写入（只有本人能读，原子替换）；返回写入后的全部设置。"""
    from ..app.paths import write_private_file

    cur = read_user_settings(data_dir)
    for k, v in updates.items():
        if k == "idle_minutes":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 24 * 60:
                raise ValueError("idle_minutes 应在 1 到 1440 之间")
            cur[k] = float(v)
        elif k == "ocr_model":
            if v not in OCR_MODELS:
                raise ValueError("ocr_model 只能是 tiny 或 small")
            cur[k] = v
        else:
            raise ValueError(f"不认识的设置：{k}")
    write_private_file(user_settings_path(data_dir), json.dumps(cur, ensure_ascii=False).encode("utf-8"))
    return cur


def _apply_user_settings(cfg: "V2Config") -> "V2Config":
    us = read_user_settings(cfg.data_dir)
    if "idle_minutes" in us:
        cfg = dataclasses.replace(cfg, idle=dataclasses.replace(cfg.idle, threshold_s=us["idle_minutes"] * 60.0))
    if "ocr_model" in us:
        cfg = dataclasses.replace(cfg, ocr=dataclasses.replace(cfg.ocr, model=us["ocr_model"]))
    return cfg


def load_v2(data_dir_override: str | os.PathLike | None = None, *, home: Path | None = None) -> V2Config:
    raw = _raw()
    v2 = dict(raw.get("v2", {}))
    home = Path(home) if home else app_home()
    data_dir = Path(
        data_dir_override
        or os.environ.get("SCREENRECALL_DATA_DIR")
        or v2.pop("data_dir", "")
        or home / "data"
    )
    v2.pop("data_dir", None)
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    sections = {
        "app": AppSettings, "recorder": RecorderConfig, "uia": UiaConfig, "writer": WriterConfig,
        "ocr": OcrSettings, "encoder": EncoderSettings, "clip": ClipBatchConfig, "idle": IdleSettings,
    }
    unknown = sorted(set(v2) - set(sections))
    if unknown:
        raise ValueError(f"配置 [v2] 里有不认识的节或键：{', '.join(unknown)}")
    built = {name: _build(cls, v2.get(name, {}), f"v2.{name}") for name, cls in sections.items()}
    rules = raw.get("rules", {})
    return _apply_user_settings(V2Config(
        data_dir=data_dir.resolve(), home=home, **built,
        default_excluded_processes=tuple(rules.get("excluded_processes", ())),
        default_excluded_title_keywords=tuple(rules.get("excluded_title_keywords", ())),
    ))
