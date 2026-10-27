"""空闲整理进程测试用的替身：假 OCR、假文本编码器、假大模型、假 CLIP、假分类器，以及造数据的小工具。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from screenrecall.app.llm import LlmReply
from screenrecall.store import schema
from screenrecall.store.writer import LineIn, Writer
from screenrecall.text.ocr2 import OcrLine
from screenrecall.timeutil import utc_ms
from screenrecall.v2.config import load_v2


def make_cfg(tmp_path: Path, *, idle=None, ocr=None, **top):
    cfg = load_v2(tmp_path / "data", home=tmp_path / "home")
    if idle:
        cfg = replace(cfg, idle=replace(cfg.idle, **idle))
    if ocr:
        cfg = replace(cfg, ocr=replace(cfg.ocr, **ocr))
    if top:
        cfg = replace(cfg, **top)
    return cfg


def db(cfg) -> sqlite3.Connection:
    c = sqlite3.connect(str(schema.db_path(cfg.data_dir)))
    c.row_factory = sqlite3.Row
    return c


def L(text, y, x=10, h=20, w=400):
    return LineIn(text, x, y, x + w, y + h)


def put_lines(w: Writer, app, title, t, texts, *, block=None, y0=0, step=30):
    sid = w.segment_for(app, title, t).result(5)
    b = block or w.block_new(sid, t).result(5)
    ids = w.record_lines(b, t, (y0 - 5, y0 + step * len(texts) + 5),
                         [L(x, y0 + i * step) for i, x in enumerate(texts)]).result(5)
    return sid, b, ids


# ---------------- OCR ----------------

class FakeOcr:
    """按调用次序返回预先给的行（裁剪图坐标）。raise_exc 不为空时抛它。"""

    def __init__(self, lines=(("你好 OCR", 0.95, (5, 4, 120, 24)),), raise_exc=None):
        self.lines = list(lines)
        self.calls: list[tuple[int, int]] = []
        self.raise_exc = raise_exc

    def recognize(self, img, *, crop=False):
        assert crop is True
        self.calls.append(img.shape[:2])
        if self.raise_exc is not None:
            raise self.raise_exc
        out = []
        for text, conf, (x0, y0, x1, y1) in self.lines:
            out.append(OcrLine(text, conf, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], x0, y0, x1, y1))
        return out


# ---------------- 文本编码器 ----------------

DIM = 384


def text_vec(text: str) -> np.ndarray:
    """确定性的"语义"向量：每个字符散列到一个维度，计数后归一化（字面越像越近）。"""
    v = np.zeros(DIM, np.float32)
    for ch in text:
        v[int(hashlib.md5(ch.encode()).hexdigest(), 16) % DIM] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v


class FakeEncoder:
    model_id = "fake-text"
    dim = DIM

    def __init__(self):
        self.batches: list[list[str]] = []

    def count_tokens(self, text):
        return len(text) + 2

    def encode(self, texts):
        self.batches.append(list(texts))
        return np.stack([text_vec(t) for t in texts]) if texts else np.zeros((0, DIM), np.float32)


# ---------------- 大模型 ----------------

class FakeLlm:
    model = "fake-model"

    def __init__(self, reply=None, *, delay=0.0, fail=None):
        self.calls: list[list[dict]] = []
        self.kwargs: list[dict] = []
        self.reply = reply
        self.delay = delay
        self.fail = fail

    def chat(self, messages, **kw):
        self.calls.append(messages)
        self.kwargs.append(kw)
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        sysmsg = messages[0]["content"]
        if self.reply is not None:
            text = self.reply
        elif '"task"' in sysmsg:
            text = '好的：```json\n{"summary": "在写周报", "task": "写周报"}\n```'
        else:
            text = json.dumps({"summary": "今天主要在写周报。"}, ensure_ascii=False)
        return LlmReply(text=text, model="fake-model", endpoint_host="llm.test", elapsed_s=0.01)


# ---------------- CLIP ----------------

class FakeClip:
    model_id = "fake-clip"

    def __init__(self):
        self.entered = 0
        self.exited = 0
        self.encoded = 0

    def __call__(self):
        return self

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1

    def encode(self, images):
        self.encoded += len(images)
        out = []
        for im in images:
            v = np.zeros(512, np.float32)
            v[: 3] = np.asarray(im, np.float32).reshape(-1, 3).mean(axis=0) + 1
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


class FakeClf:
    version = "clf-v"

    def predict(self, X):
        return ["code" if x[0] > x[1] else "web" for x in X], np.full(len(X), 0.9)


def now_ms() -> int:
    return utc_ms()
