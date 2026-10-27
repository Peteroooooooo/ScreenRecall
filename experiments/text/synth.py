"""合成测试图：PIL 画的中英混排文字（聊天 1600x1000、截图小块 720x400、密集文档 2082x1491）。

内容全是程序生成的，不含任何真实屏幕内容。返回 (BGR 图, 逐行真值 [(文字, 左, 上)])。
"""

from __future__ import annotations

import random

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_ZH = r"C:\Windows\Fonts\msyh.ttc"
FONT_SONG = r"C:\Windows\Fonts\simsun.ttc"

ZH = [
    "今天下午三点在会议室开组会", "请把最新的实验数据整理一下", "这个版本的接口还没有上线",
    "周五之前要交课程报告", "图书馆三楼的自习室还有位置", "明天早上八点半出发去机场",
    "数据库迁移脚本已经跑完了", "老师说作业可以延期两天", "我们先把需求文档过一遍",
    "晚饭想吃火锅还是烤肉", "服务器的磁盘快满了需要清理", "录屏软件每秒只截一张图",
    "这段代码在边界情况下会出错", "下周二的面试改到线上进行", "记得备份一下本地的配置文件",
    "搜索结果按最近一次看到的时间排序", "新版本的识别速度快了很多", "把截图里的文字存进索引",
]
EN = [
    "Meeting at 3pm in room 4-02", "please review the pull request", "deadline is Friday 23:59",
    "the build failed on step 7", "see report_v2.pdf for details", "latency p95 is 120 ms",
    "Search results are grouped by segment", "OCR runs only when the PC is idle",
    "error 0x80070005 access denied", "git push origin feature/text", "Total: 1,234.56 SGD",
]
NAMES = ["张三", "李四", "王五", "Alice", "Bob", "陈老师", "项目组"]


def _sentence(rng: random.Random) -> str:
    k = rng.random()
    if k < 0.45:
        return rng.choice(ZH)
    if k < 0.7:
        return rng.choice(EN)
    return f"{rng.choice(ZH)} {rng.choice(EN)}"


def _bgr(im: Image.Image) -> np.ndarray:
    return np.ascontiguousarray(np.asarray(im.convert("RGB"))[:, :, ::-1])


def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> list[str]:
    """按像素宽度逐字折行（聊天气泡里的折行方式），行首行尾的空格去掉。"""
    lines, cur = [], ""
    for ch in text:
        if cur and d.textlength(cur + ch, font=font) > max_w:
            lines.append(cur.strip())
            cur = ch
        else:
            cur += ch
    if cur.strip():
        lines.append(cur.strip())
    return [ln for ln in lines if ln]


def chat(seed: int, size: tuple[int, int] = (1600, 1000), font_px: int = 21) -> tuple[np.ndarray, list[tuple[str, int, int]]]:
    rng = random.Random(seed)
    w, h = size
    im = Image.new("RGB", size, (237, 237, 237))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_ZH, font_px)
    fs = ImageFont.truetype(FONT_ZH, font_px - 5)
    truth: list[tuple[str, int, int]] = []
    y = 20
    while y < h - 3 * font_px:
        right = rng.random() < 0.4
        name = rng.choice(NAMES)
        stamp = f"{rng.randint(8, 22):02d}:{rng.randint(0, 59):02d}"
        meta = f"{name} {stamp}"
        x = w - 60 - int(d.textlength(meta, font=fs)) if right else 60
        d.text((x, y), meta, fill=(120, 120, 120), font=fs)
        truth.append((meta, x, y))
        y += font_px + 6
        texts: list[str] = []
        for _ in range(rng.choice([1, 1, 2])):
            texts.extend(_wrap(d, _sentence(rng), f, int(w * 0.62)))
        n = len(texts)
        if y + n * (font_px + 10) + 14 > h - 4:
            break  # 画不下的气泡不画，真值只含画出来的字
        tw = max(int(d.textlength(t, font=f)) for t in texts)
        bh = n * (font_px + 10) + 14
        bx = w - 60 - tw - 28 if right else 60
        d.rounded_rectangle((bx, y, bx + tw + 28, y + bh), 8, fill=(149, 236, 105) if right else (255, 255, 255))
        for i, t in enumerate(texts):
            d.text((bx + 14, y + 8 + i * (font_px + 10)), t, fill=(20, 20, 20), font=f)
            truth.append((t, bx + 14, y + 8 + i * (font_px + 10)))
        y += bh + 22
    return _bgr(im), truth


def crop(seed: int) -> tuple[np.ndarray, list[tuple[str, int, int]]]:
    return chat(seed, (720, 400), 20)


def dense(seed: int, size: tuple[int, int] = (2082, 1491), font_px: int = 18) -> tuple[np.ndarray, list[tuple[str, int, int]]]:
    rng = random.Random(seed)
    w, h = size
    im = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(FONT_SONG if seed % 2 else FONT_ZH, font_px)
    truth: list[tuple[str, int, int]] = []
    y = 40
    col_w = (w - 120) // 2
    for col in range(2):
        x0 = 40 + col * (col_w + 40)
        y = 40
        while y < h - 2 * font_px:
            line = ""
            while True:
                nxt = (line + " " if line and rng.random() < 0.3 else line) + _sentence(rng)
                if d.textlength(nxt, font=f) > col_w:
                    break
                line = nxt
            if not line:
                line = _sentence(rng)
            d.text((x0, y), line, fill=(0, 0, 0), font=f)
            truth.append((line, x0, y))
            y += int(font_px * 1.6)
    return _bgr(im), truth


KINDS = {"chat_1600x1000": chat, "crop_720x400": crop, "dense_2082x1491": dense}
