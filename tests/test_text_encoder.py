"""ONNX 文本编码器：形状、归一化、批次与顺序无关、token 计数、截断、懒加载卸载。

需要本机已缓存锁定 revision 的 onnx/model.onnx（python -m screenrecall.text.encoder --download），否则跳过。
与 torch 版逐条对照在 tests_win/test_encoder_equiv.py（加载 torch 慢，不放在快速单元测试里）。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from screenrecall import models
from screenrecall.text import encoder

try:
    encoder.model_dir()
    _HAVE = True
except encoder.EncoderModelMissing:
    _HAVE = False

needs_model = pytest.mark.skipif(not _HAVE, reason="本机未缓存文本模型的 ONNX 文件")


@pytest.fixture(scope="module")
def enc():
    return encoder.OnnxTextEncoder(batch_size=4)


def test_missing_model_error_is_clear(monkeypatch):
    spec = models.ModelSpec("sentence-transformers/does-not-exist-sr-test", "0" * 40)
    with pytest.raises(encoder.EncoderModelMissing, match="--download"):
        encoder.model_dir(spec)


@needs_model
def test_identity_matches_legacy_model_id(enc):
    assert enc.model_id == models.DEFAULT_MODELS.text.model_id
    assert enc.max_tokens == 128 and enc.dim == 384


@needs_model
def test_shape_norm_and_order(enc):
    texts = ["今天下午三点开组会", "meeting at 3pm", "", "组会", "请把 report_v2.pdf 发到群里 " * 3]
    v = enc.encode(texts)
    assert v.shape == (5, 384) and v.dtype == np.float32
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    # 分批、补齐、排序都不改变结果：逐条编码与整批一致
    single = np.vstack([enc.encode([t]) for t in texts])
    assert np.abs(single - v).max() < 1e-4
    assert enc.encode([]).shape == (0, 384)
    # 语义：中英同义句比无关句更近
    a, b, c = enc.encode(["今天下午开会", "meeting this afternoon", "磁盘快满了"])
    assert a @ b > a @ c


@needs_model
def test_count_tokens_and_truncation(enc):
    assert enc.count_tokens("hello") >= 3
    long = "录屏软件每秒只截一张图 " * 40
    assert enc.count_tokens(long) > 128
    # 超过 128 token 的部分被截断：后面再加内容不改变向量
    v1, v2 = enc.encode([long, long + " 完全不同的结尾 different ending"])
    assert float(v1 @ v2) > 0.99999


@needs_model
def test_lazy_encoder_loads_and_unloads():
    lazy = encoder.LazyTextEncoder(idle_unload_s=0.3)
    assert not lazy.loaded and lazy.model_id == models.DEFAULT_MODELS.text.model_id
    assert lazy.encode(["你好"]).shape == (1, 384)
    assert lazy.loaded
    end = time.monotonic() + 5
    while lazy.loaded and time.monotonic() < end:
        time.sleep(0.05)
    assert not lazy.loaded
    lazy.unload()
