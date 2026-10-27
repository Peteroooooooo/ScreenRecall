"""v2 录制快路径测试用的合成画面：PIL 画出中英文混排的长文档，再按滚动位置裁出"窗口画面"。

不使用任何真实屏幕内容。字体优先用系统自带的微软雅黑，找不到时退回 PIL 默认字体。
"""

from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_WORDS_ZH = "屏幕 记忆 窗口 滚动 课程 项目 数据 模型 搜索 证据 时间 应用 文字 识别 组会 报告 进度 实验 结果 讨论".split()
_WORDS_EN = "screen recall window scroll course project data model search evidence time text query block".split()


@lru_cache(maxsize=8)
def font(size: int = 22):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\arial.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def sentence(rng: random.Random, n: int = 12) -> str:
    out = []
    for _ in range(n):
        out.append(rng.choice(_WORDS_ZH) if rng.random() < 0.6 else rng.choice(_WORDS_EN))
    return " ".join(out) + f" {rng.randint(0, 99999)}"


@lru_cache(maxsize=8)
def document(width: int = 900, height: int = 6000, seed: int = 1, line_h: int = 34, size: int = 22) -> np.ndarray:
    """一张很长的 BGR 文档图（只读，调用方要改请先 copy）。"""
    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f = font(size)
    y = 10
    while y < height - line_h:
        if rng.random() < 0.12:
            y += line_h  # 空行
            continue
        d.text((20, y), sentence(rng), fill=(20, 20, 20), font=f)
        y += line_h
    arr = np.asarray(img)[:, :, ::-1].copy()
    arr.setflags(write=False)
    return arr


def header(width: int, height: int = 60, text: str = "合成窗口 - 标题栏") -> np.ndarray:
    img = Image.new("RGB", (width, height), (235, 238, 242))
    ImageDraw.Draw(img).text((16, 14), text, fill=(30, 30, 30), font=font(20))
    return np.asarray(img)[:, :, ::-1].copy()


def view(doc: np.ndarray, top: int, vh: int, head: np.ndarray | None = None) -> np.ndarray:
    """窗口画面：固定标题栏 + 文档从 top 开始的 vh 行。"""
    body = doc[top: top + vh]
    if head is None:
        return np.ascontiguousarray(body)
    return np.ascontiguousarray(np.vstack([head, body]))


def write_text(img: np.ndarray, x: int, y: int, text: str, size: int = 22) -> np.ndarray:
    """在 BGR 图上原地写一段字（模拟打字、内容原地变化），返回新图。"""
    pil = Image.fromarray(img[:, :, ::-1].copy())
    d = ImageDraw.Draw(pil)
    d.rectangle((x, y, x + 600, y + size + 8), fill=(255, 255, 255))
    d.text((x, y), text, fill=(10, 10, 10), font=font(size))
    return np.asarray(pil)[:, :, ::-1].copy()


def paint_box(img: np.ndarray, x: int, y: int, w: int, h: int, seed: int) -> np.ndarray:
    """在一块矩形里填随机色块（模拟视频、时钟）。"""
    out = img.copy()
    rng = np.random.default_rng(seed)
    out[y:y + h, x:x + w] = rng.integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    return out
