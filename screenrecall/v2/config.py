"""v2 的运行配置：config.default.toml 的 [v2.*] 各节 + config.local.toml（本机覆盖）+ 环境变量。

各模块的参数本来就定义成自己的 dataclass（RecorderConfig、UiaConfig、WriterConfig……），这里只负责把
TOML 里同名的键填进去；没写的键用 dataclass 默认值，写错名字的键直接报错，免得悄悄不生效。

数据目录：--data-dir > SCREENRECALL_DATA_DIR > [v2] data_dir > %LOCALAPPDATA%\\ScreenRecall\\data。
老的 screenrecall.sqlite3（冻结评测用）在老数据目录里，v2 不读也不写它。
"""

from __future__ import annotations

import dataclasses
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
    use_gpu: bool = False          # OCR 用显卡（需要单独装 onnxruntime-directml，默认关）


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
    return V2Config(
        data_dir=data_dir.resolve(), home=home, **built,
        default_excluded_processes=tuple(rules.get("excluded_processes", ())),
        default_excluded_title_keywords=tuple(rules.get("excluded_title_keywords", ())),
    )
