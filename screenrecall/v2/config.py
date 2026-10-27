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
    # 钉住的窗口在后台录到的内容：多久没有新内容才算另一个片段（聊天窗口隔几分钟来一条消息，
    # 用前台的 2 分钟会切成一串很短的片段）
    background_piece_gap_ms: int = 600_000
    summaries: bool = True         # 空闲时用大模型写片段 / 天概括
    clip: bool = True              # 空闲时给证据图算 CLIP 向量（内容分类器要用）
    # 以下给空闲整理进程（screenrecall.v2.worker）
    chunk_lines: int = 6           # 文字向量的一个段落最多几行（同一块里阅读顺序相邻）
    chunk_tokens: int = 100        # 一个段落最多多少 token（模型上限 128）
    llm_min_interval_s: float = 1.0  # 两次大模型调用至少间隔多久
    llm_max_calls: int = 200       # 一次空闲整理最多调用大模型几次
    llm_reasoning_effort: str = "none"  # 思考强度；空 = 用模型默认
    classifier_dir: str = "results/classifier/v1"  # 内容分类器（CLIP 向量 + 线性模型）；空 = 不预测
    frame_report_days: int = 30    # 只统计（不删除）早于这么多天、没标"保留"的证据帧，留给以后的保留期设置
    encoder_threads: int = 4       # 空闲时算文字向量的线程数（实测 3000 行：2 线程 215 行/秒，4 线程 373，8 线程 517）


@dataclass(frozen=True)
class RetentionSettings:
    """保留期与空间上限（隐私与删除页可改，存在 user_settings.json，比这里优先）。由空闲整理进程执行（work_frames）。"""
    image_days: int = 30           # 截图保留几天；0 = 一直保留。过期的截图删掉，文字还在
    text_days: int = 90            # 文字记录保留几天；0 = 一直保留。过期的按隐私删除的方式整段删掉
    storage_cap_gb: float = 0.0    # 数据目录（数据库 + 截图 + 向量）最多占多少 GB；0 = 不限。超了从最旧的截图删起
    dedupe_frames: bool = True     # 删掉多余的旧截图（它的每一行后面另一张留着的截图里都有）
    dedupe_min_age_s: float = 1800.0  # 存下不到这么久的截图先不看（录制进程可能还在往上面登记行）
    cap_check_s: float = 600.0     # 主进程多久查一次空间上限


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
    retention: RetentionSettings = field(default_factory=RetentionSettings)
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


MAX_RETENTION_DAYS = 3650
MAX_CAP_GB = 100_000.0


def check_user_setting(key: str, v: Any) -> Any:
    """校验一项页面设置，返回规范化的值；不对时抛 ValueError（中文说明）。
    image_days / text_days：0 = 一直保留；storage_cap_gb：0 = 不限。"""
    num = isinstance(v, (int, float)) and not isinstance(v, bool)
    if key == "idle_minutes":
        if not num or not 1 <= v <= 24 * 60:
            raise ValueError("idle_minutes 应在 1 到 1440 之间")
        return float(v)
    if key == "ocr_model":
        if v not in OCR_MODELS:
            raise ValueError("ocr_model 只能是 tiny 或 small")
        return v
    if key in ("image_days", "text_days"):
        if not num or int(v) != v or not 0 <= v <= MAX_RETENTION_DAYS:
            raise ValueError(f"{key} 应为 0（一直保留）到 {MAX_RETENTION_DAYS} 的整数")
        return int(v)
    if key == "storage_cap_gb":
        if not num or not 0 <= v <= MAX_CAP_GB:
            raise ValueError("storage_cap_gb 应为 0（不限）或正数")
        return float(v)
    raise ValueError(f"不认识的设置：{key}")


USER_SETTING_KEYS = ("idle_minutes", "ocr_model", "image_days", "text_days", "storage_cap_gb")


def read_user_settings(data_dir: str | os.PathLike) -> dict[str, Any]:
    """{idle_minutes, ocr_model, image_days, text_days, storage_cap_gb} 中写过的几项；
    文件不存在或某项内容不对时，那一项当没写（用配置里的默认值）。"""
    try:
        raw = json.loads(user_settings_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, Any] = {}
    if isinstance(raw, dict):
        for k in USER_SETTING_KEYS:
            if k in raw:
                try:
                    out[k] = check_user_setting(k, raw[k])
                except ValueError:
                    pass
    return out


def write_user_settings(data_dir: str | os.PathLike, updates: dict[str, Any]) -> dict[str, Any]:
    """合并写入（只有本人能读，原子替换）；返回写入后的全部设置。"""
    from ..app.paths import write_private_file

    cur = read_user_settings(data_dir)
    for k, v in updates.items():
        cur[k] = check_user_setting(k, v)
    write_private_file(user_settings_path(data_dir), json.dumps(cur, ensure_ascii=False).encode("utf-8"))
    return cur


def apply_user_settings(cfg: "V2Config", us: dict[str, Any] | None = None) -> "V2Config":
    """把页面上改的设置套到配置上（us 为空时现读 user_settings.json）。主进程每次用保留期设置前也调一次。"""
    us = read_user_settings(cfg.data_dir) if us is None else us
    if "idle_minutes" in us:
        cfg = dataclasses.replace(cfg, idle=dataclasses.replace(cfg.idle, threshold_s=us["idle_minutes"] * 60.0))
    if "ocr_model" in us:
        cfg = dataclasses.replace(cfg, ocr=dataclasses.replace(cfg.ocr, model=us["ocr_model"]))
    ret = {k: us[k] for k in ("image_days", "text_days", "storage_cap_gb") if k in us}
    if ret:
        cfg = dataclasses.replace(cfg, retention=dataclasses.replace(cfg.retention, **ret))
    return cfg


_apply_user_settings = apply_user_settings


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
        "retention": RetentionSettings,
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
