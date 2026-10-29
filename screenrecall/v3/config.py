"""v3 的运行配置：config.default.toml 的 [v3] 一节 + config.local.toml（本机覆盖）。

所有能调的数字都在这一节（方案"能调的数字"表 + 名单默认值 + 数据目录），键名和 V3Config 的字段同名；
没写的键用这里的默认值，写错名字的键直接报错，免得悄悄不生效。别的节（[v2.*]、旧版各节）v3 不读。

数据目录：--data-dir > 环境变量 SCREENRECALL_V3_DATA_DIR > [v3] data_dir > <仓库>/data-v3。
故意不认 v2 的 SCREENRECALL_DATA_DIR：v3 用新的库文件（DB_NAME），不能指到 v2 的数据目录上。
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..app.paths import app_home
from ..config import REPO_ROOT, _merge

DB_NAME = "recall-v3.sqlite3"      # v3 的库文件（和 v2 的 recall.sqlite3、旧版 screenrecall.sqlite3 都不同名）
SHOTS_DIR = "shots"                # 截图放在 <数据目录>/shots/YYYY-MM-DD/<shot_id>.jpg
VECTORS_DIR = "vectors"            # 可选的文字向量：<数据目录>/vectors/lines
DATA_DIR_ENV = "SCREENRECALL_V3_DATA_DIR"
DEFAULT_DATA_DIR = "data-v3"       # 相对仓库根目录

TIERS = ("record", "always", "never")   # 录 / 一直录 / 不录（不在名单里 = never）


@dataclass(frozen=True)
class V3Config:
    data_dir: Path
    home: Path

    # ---- 主进程 ----
    port: int = 8611                   # 主页端口（只在 127.0.0.1 上）；被占用时自动换一个
    open_browser: bool = True
    tray: bool = True

    # ---- 录哪些软件 ----
    # 第一次建库时写进名单"录"这一档的进程名（之后以页面上改的为准）。只是数据，不按名字写任何特殊规则
    default_record_apps: tuple[str, ...] = ("msedge.exe", "chrome.exe", "firefox.exe", "brave.exe",
                                            "opera.exe", "vivaldi.exe")
    # 无痕窗口：标题里有这些词就跳过（不分大小写）
    private_title_keywords: tuple[str, ...] = ("InPrivate", "无痕", "隐身", "Incognito", "Private Browsing")
    # 只录这些网址协议；edge:// chrome:// about: 等浏览器内部页一律跳过
    record_url_schemes: tuple[str, ...] = ("http", "https", "file")
    # 网址以这些开头也算浏览器内部页（新标签页实际打开的是 https 页面）。只是数据
    internal_url_prefixes: tuple[str, ...] = ("https://ntp.msn.com/edge/ntp", "https://ntp.msn.cn/edge/ntp",
                                              "https://www.msn.com/spartan/ntp", "https://www.google.com/_/chrome/newtab")
    max_background_windows: int = 2    # "一直录"的窗口最多同时在后台录几个

    # ---- ① 拍 ----
    fps: float = 1.0                   # 每个在录窗口最多几帧/秒
    still_ms: int = 2000               # 画面停下这么久就拍
    forced_ms: int = 10000             # 画面一直在动满这么久，强制拍一次
    title_settle_ms: int = 1000        # 同一窗口标题变了，稳定这么久才算换了页面

    # ---- 跳过 ----
    typing_quiet_ms: int = 5000        # 这个页面这么久内打过字就先不拍

    # ---- ② 读字 ----
    uia_timeout_ms: int = 1500         # 系统文字最多读这么久（后台 Edge 实测：维基 2 秒、视频页 0.7 秒；超时就 OCR，网址照样有）
    uia_max_nodes: int = 5000          # 最多遍历这么多个控件
    ocr_min_chars: int = 100           # 系统文字（正文）少于这么多字就再 OCR
    ocr_min_body_ratio: float = 0.30   # 或者正文不到读到的字的这个比例（大多是菜单按钮）
    ocr_model: str = "tiny"            # PP-OCRv6 tiny（约 0.7 秒一个窗口）
    ocr_threads: int = 2
    ocr_models_dir: str = ""           # 空 = <仓库>/models/ocr

    # ---- ⑤ 截图 ----
    jpeg_quality: int = 80
    shot_max_width: int = 1920
    fallback_shot_ms: int = 60000      # 读不出字的新画面：同一个东西这么久最多一张
    fallback_min_area: float = 0.05    # 上次拍之后变了的、又没有字的地方至少占画面这么多，才算"读不出字的新画面"
    weak_visit_ms: int = 600000        # 弱标签一直不走：这么久算新的一次
    image_days: int = 30               # 截图留几天；字一直留着
    min_free_gb: float = 5.0           # 硬盘剩不到这么多就先不存图
    clip_max_chars: int = 4000         # 复制的文字最多记这么多字

    # ---- ⑥ 时间线 ----
    merge_ms: int = 300000             # 这么久内回到同一个东西，合成一条
    away_ms: int = 300000              # 这么久没碰键盘鼠标算走开（不算进"看了多久"）

    # ---- 查找 ----
    search_merge_ms: int = 600000      # 同一个东西这么久内搜到好几次，只显示一条

    # ---- 可选（空闲时做） ----
    idle_s: float = 300.0              # 键鼠这么久没动开始空闲整理
    llm_names: bool = True             # 用 .env 里配的大模型给每样东西起名字、每小时写一句话；没配就用标题
    llm_max_calls: int = 100           # 一次空闲整理最多调用大模型几次
    vectors: bool = False              # 按意思搜（MiniLM 文字向量）："有空再做"，默认关

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    @property
    def shots_dir(self) -> Path:
        return self.data_dir / SHOTS_DIR

    @property
    def vectors_dir(self) -> Path:
        return self.data_dir / VECTORS_DIR

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def ocr_models_path(self) -> Path | None:
        return Path(self.ocr_models_dir) if self.ocr_models_dir else None


def _raw() -> dict:
    raw = tomllib.loads((REPO_ROOT / "config.default.toml").read_text(encoding="utf-8"))
    local = REPO_ROOT / "config.local.toml"
    if local.exists():
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8-sig")))
    return raw


def build(raw_v3: dict[str, Any], *, data_dir: Path, home: Path) -> V3Config:
    """按字段名填值；列表转成 tuple；不认识的键报错。测试直接用它（不读 TOML 文件）。"""
    names = {f.name for f in dataclasses.fields(V3Config)} - {"data_dir", "home"}
    unknown = sorted(set(raw_v3) - names)
    if unknown:
        raise ValueError(f"配置 [v3] 里有不认识的键：{', '.join(unknown)}")
    kwargs = {k: tuple(v) if isinstance(v, list) else v for k, v in raw_v3.items()}
    return V3Config(data_dir=data_dir, home=home, **kwargs)


def load_v3(data_dir_override: str | os.PathLike | None = None, *, home: Path | None = None,
            raw: dict | None = None) -> V3Config:
    v3 = dict((raw if raw is not None else _raw()).get("v3", {}))
    home = Path(home) if home else app_home()
    configured = v3.pop("data_dir", "")
    data_dir = Path(data_dir_override or os.environ.get(DATA_DIR_ENV) or configured or DEFAULT_DATA_DIR)
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir
    return build(v3, data_dir=data_dir.resolve(), home=home)
