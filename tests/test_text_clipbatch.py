"""CLIP 批次：线程上限、进出批次加载 / 释放、BGR 输入、不能嵌套（用假的加载器，不加载 torch 模型）。"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from screenrecall import models
from screenrecall.text import clipbatch


class FakeImage:
    instances = 0

    def __init__(self, spec, cfg):
        FakeImage.instances += 1
        self.spec, self.cfg = spec, cfg
        self.seen = []

    def encode_images(self, images):
        self.seen.extend(images)
        return np.ones((len(images), 512), dtype=np.float32)


@pytest.fixture
def threads(monkeypatch):
    state = {"n": 16, "calls": []}

    def set_(n):
        state["calls"].append(n)
        state["n"] = n

    monkeypatch.setattr(clipbatch, "_get_torch_threads", lambda: state["n"])
    monkeypatch.setattr(clipbatch, "_set_torch_threads", set_)
    return state


def test_batch_loads_caps_threads_and_releases(threads):
    cb = clipbatch.ClipBatch(clipbatch.ClipBatchConfig(threads=4, batch_size=8), loader=FakeImage)
    assert cb.model_id == models.DEFAULT_MODELS.image.model_id
    with cb as clip:
        assert threads["n"] == 4
        enc = clip._enc
        assert enc.spec == models.DEFAULT_MODELS.image and enc.cfg.batch_size == 8 and enc.cfg.threads == 4
        bgr = np.zeros((20, 30, 3), dtype=np.uint8)
        bgr[:, :, 0] = 255  # 纯蓝（BGR）
        v = clip.encode([bgr, Image.new("RGB", (10, 10))])
        assert v.shape == (2, 512) and v.dtype == np.float32
        assert enc.seen[0].getpixel((0, 0)) == (0, 0, 255)
        assert clip.encode([]).shape == (0, 512)
    assert cb._enc is None and threads["n"] == 16
    with pytest.raises(RuntimeError):
        cb.encode([bgr])


def test_no_nesting_and_lock_released_after_error(threads):
    with clipbatch.ClipBatch(loader=FakeImage):
        with pytest.raises(RuntimeError, match="已经有一个"):
            with clipbatch.ClipBatch(loader=FakeImage):
                pass

    def broken(spec, cfg):
        raise OSError("模型文件缺失")

    with pytest.raises(OSError):
        with clipbatch.ClipBatch(loader=broken):
            pass
    with clipbatch.ClipBatch(loader=FakeImage) as clip:  # 出错后锁已释放
        assert clip._enc is not None
