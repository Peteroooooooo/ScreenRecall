"""ONNX 文本编码器与 sentence-transformers（torch）逐条对照：补齐的中英混合、长短不一的批次。

要求 cos 最小值 ≥ 0.9999，每条查询的前 10 近邻完全相同。需要本机已缓存两套文件（torch 权重 + onnx）。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from screenrecall import models
from screenrecall.text import encoder

pytestmark = pytest.mark.real


def _corpus(n: int = 160) -> list[str]:
    rng = random.Random(11)
    zh = ["今天下午三点开组会", "请把实验数据整理一下", "服务器磁盘快满了", "周五之前交课程报告", "图书馆三楼还有位置",
          "录屏软件每秒只截一张图", "搜索结果按最近看到的时间排序", "老师说作业可以延期"]
    en = ["meeting at 3pm", "please review the pull request", "the build failed on step 7", "error 0x80070005 access denied",
          "OCR runs only when idle", "latency p95 is 120 ms", "git push origin feature/text"]
    out = ["", "a", "微信", "组会"]
    while len(out) < n:
        k = rng.randint(1, 30)  # 有的很长：超过 128 token 会被截断
        out.append(" ".join(rng.choice(zh + en) for _ in range(k)))
    return out


@pytest.fixture(scope="module")
def both():
    try:
        onnx = encoder.OnnxTextEncoder(batch_size=16)
    except encoder.EncoderModelMissing as exc:
        pytest.skip(str(exc))
    if models.available(models.DEFAULT_MODELS):
        pytest.skip("本机没有 torch 版模型")
    st = models.StText(models.DEFAULT_MODELS.text, models.DEFAULT_MODELS)
    return onnx, st


def test_vectors_match_torch(both):
    onnx, st = both
    texts = _corpus()
    a = onnx.encode(texts)
    b = st.encode(texts)
    assert a.shape == b.shape == (len(texts), 384) and a.dtype == np.float32
    cos = (a * b).sum(axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
    print(f"cos min={cos.min():.6f} mean={cos.mean():.6f}")
    assert cos.min() >= 0.9999
    sims_a, sims_b = a @ a.T, b @ b.T
    top_a = np.argsort(-sims_a, axis=1)[:, :10]
    top_b = np.argsort(-sims_b, axis=1)[:, :10]
    overlap = np.mean([len(set(x) & set(y)) / 10 for x, y in zip(top_a, top_b)])
    print(f"top-10 overlap={overlap:.4f}")
    assert overlap == 1.0


def test_identity_and_tokens_match_torch(both):
    onnx, st = both
    assert onnx.model_id == st.model_id
    assert onnx.max_tokens == st.max_tokens == 128
    for t in _corpus(40):
        assert onnx.count_tokens(t) == st.count_tokens(t)
