"""v3 各单元之间共用的数据类型（V3_CONTRACT.md §3）。只放数据，不放逻辑；改这里要先改约定。

坐标：box 一律是 (x, y, w, h)，单位是像素。TextLine.box 在"整帧"坐标里（录制拿到的原始帧，左上角为 0）；
存进库里的截图框（ShotBox）在"存下的那张 JPEG"坐标里（可能缩小过，见 shots.encode_jpeg 返回的 scale）。
时间一律 UTC 毫秒整数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Box = tuple[int, int, int, int]  # (x, y, w, h)

# 拍的原因（snaps.reason）
REASON_STILL = "still"     # 画面停下 still_ms
REASON_FORCED = "forced"   # 一直在动满 forced_ms
REASON_COPY = "copy"       # 按了复制
REASONS = (REASON_STILL, REASON_FORCED, REASON_COPY)

# 来源种类（sources.kind）
KIND_URL = "url"           # 强标签：网址
KIND_FILE = "file"         # 强标签：本地文件（不存字）
KIND_APP = "app_title"     # 弱标签：软件名 + 窗口标题

# 截图种类（shots.kind）
SHOT_FIRST = "first"       # 强标签：这个网址 / 文件的第一张
SHOT_VISIT = "visit"       # 弱标签：这一次里新字最多的那张
SHOT_FALLBACK = "fallback" # 读不出字的新画面（每分钟最多一张）

# 灰色时段（grey_spans.reason）
GREY_UNLISTED = "unlisted" # 不在名单里的软件（带 app）：时间线上"在 XX 里（未录）" + "以后录它"
GREY_PAUSED = "paused"
GREY_LOCKED = "locked"

# control.paused_until_ms 取这个值 = 一直暂停到手动继续（托盘的 pause_until(None)）
PAUSE_FOREVER_MS = 1 << 62


@dataclass(frozen=True)
class TextLine:
    """读到的一行正文。via：'uia' 系统文字 / 'ocr'。"""
    text: str
    box: Box
    via: str


@dataclass(frozen=True)
class TextResult:
    """read.read_text 的结果。lines 只有正文（菜单按钮不要），已去重，按阅读顺序。

    via：'uia' / 'ocr' / 'uia+ocr' / 'none'（一个字都没读到）。
    url：系统文字里最大的 Document 元素的值（原样，没清理）；正文读超时了也有（读字前单独找的）；没有时 None。
    page_title：那个 Document 的名字（浏览器里是网页自己的标题）；window_title：读完系统文字时窗口的标题
    （None = 不知道；和拍的时候不一样 = 拍完到读字之间换了页面，这一拍的图和字对不上）。
    uia_chars：系统文字里全部的字数（正文 + 界面）；body_chars：其中正文的字数。
    """
    lines: tuple[TextLine, ...]
    via: str
    url: str | None = None
    uia_chars: int = 0
    body_chars: int = 0
    uia_ms: float = 0.0
    ocr_ms: float = 0.0
    status: str = "ok"         # ok / empty / error（UIA 出错且没有 OCR 结果）
    page_title: str | None = None
    window_title: str | None = None
    ocr_skipped: bool = False  # 字不够、本该 OCR，但调用方说这一拍反正不存（want_ocr 返回 False）


@dataclass(frozen=True)
class SourceLabel:
    """③ 记出处。key 在整个库里唯一（sources.key）：
    url → 清理后的网址；file → 规范化的完整路径（小写盘符、反斜杠）；app_title → f"{app.lower()}\\t{title}"。"""
    kind: str
    key: str
    app: str                   # 进程名，如 msedge.exe
    title: str                 # 清理后的窗口标题（去掉未读数、转圈符号）
    url: str | None = None     # kind == url 时的清理后网址；file 来自 file:/// 时也填原网址
    path: str | None = None    # kind == file 时的完整路径

    @property
    def strong(self) -> bool:
        return self.kind != KIND_APP


@dataclass(frozen=True)
class LinesOutcome:
    """Store.record_lines 的结果。new_ids：这次第一次进库的行；seen_ids：库里已有、只更新了时间的行。
    ids_by_norm：这次所有行（新 + 旧）的 norm → line_id，用来给截图登记框。"""
    new_ids: tuple[int, ...] = ()
    seen_ids: tuple[int, ...] = ()
    ids_by_norm: dict[str, int] = field(default_factory=dict)

    @property
    def new_count(self) -> int:
        return len(self.new_ids)


@dataclass(frozen=True)
class ShotBox:
    """截图上一行字的位置（存下的 JPEG 坐标）。"""
    line_id: int
    box: Box


@dataclass
class EncodedShot:
    """编码好的截图（shots.encode_jpeg 的结果 + 框）。boxes 已换算成 JPEG 坐标、只差 line_id 时用 norm 对应。"""
    jpeg: bytes
    width: int
    height: int
    scale: float               # JPEG 宽 / 原帧宽（≤ 1）
    boxes: list[tuple[str, Box]] = field(default_factory=list)  # (行的 norm, JPEG 坐标框)


# ---------------- 录制进程内：捕获循环 → 处理线程 的事件 ----------------
# 捕获循环（loop.py）只把事件放进队列；处理线程（pipeline.py）按顺序处理，所有写库都在处理线程里。

@dataclass
class SnapEvent:
    """① 拍了一次。frame 是 rec.frames.Frame（含 bgr / fmap）。background：一直录的窗口在后台。"""
    hwnd: int
    pid: int | None
    app: str
    title: str
    t_ms: int
    reason: str
    frame: Any
    background: bool = False
    # 上次拍这个窗口之后画面变过的地方（变了的 32 像素小块的外接框，整帧坐标；一直在变的小块不算）；
    # 这个窗口第一次拍（刚切进来）时 None = 当成整幅都是新的
    changed: Box | None = None


@dataclass
class FocusEvent:
    """前台换了（或同一窗口标题变了并稳定了）。hwnd = 0 表示现在没有要录的前台（桌面、锁屏、暂停、不在名单……）。"""
    t_ms: int
    hwnd: int = 0
    pid: int | None = None
    app: str = ""
    title: str = ""


@dataclass
class GreyEvent:
    """灰色时段开始（end=False）或结束（end=True）。reason 见 GREY_*；app 只有 unlisted 才有。"""
    t_ms: int
    reason: str
    app: str | None = None
    end: bool = False


@dataclass
class AwayEvent:
    """一段 ≥ away_ms 没碰键盘鼠标的时间（结束时才发）：从当前这一次的"看了多久"里减掉。"""
    start_ms: int
    end_ms: int


@dataclass
class CopyEvent:
    """按了复制。text 已截到 clip_max_chars；窗口是当时的前台（在名单里、没被跳过）。"""
    t_ms: int
    hwnd: int
    app: str
    title: str
    text: str
