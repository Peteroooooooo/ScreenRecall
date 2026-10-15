"""模型清单与编码器（提案 4.1；方案 5.1）。

固定一套默认模型并锁定 revision；向量身份 = repo@revision + 预处理版本，换任何一项都会形成新的特征集。
- 文本语义：paraphrase-multilingual-MiniLM-L12-v2（384 维），后台编码文本块、页面编码文字查询；
- 全图：clip-ViT-B-32 图像编码器（512 维），每张唯一截图一份，保持比例补边后送入；
- 画面查询：clip-ViT-B-32-multilingual-v1，把中英文描述映射到同一图像空间（只与 CLIP 图像向量比较）。

sentence-transformers / torch 延迟导入：P0 路径与单元测试不需要它们。
准备模型（联网一次）：python -m screenrecall.models --download；之后默认离线加载（local_files_only）。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np
from PIL import Image

IMAGE_PREPROCESS = "pad-square-rgb128-v1"
# 只下载 sentence-transformers 实际加载的文件（跳过 onnx/openvino/旧 .bin 等重复权重，省约 2 GB）
ALLOW_PATTERNS = [
    "*.json", "*.txt", "*.model", "*.safetensors", "sentencepiece*", "tokenizer*",
    "1_Pooling/*", "2_Dense/*", "0_CLIPModel/*",
]
_PAD_COLOR = (128, 128, 128)


@dataclass(frozen=True)
class ModelSpec:
    repo: str
    revision: str

    @property
    def model_id(self) -> str:
        return f"{self.repo}@{self.revision[:12]}"


@dataclass(frozen=True)
class ModelsConfig:
    text: ModelSpec
    image: ModelSpec
    clip_text: ModelSpec
    threads: int = 2  # 每个进程的 torch 推理线程数（方案 2.5）
    batch_size: int = 16
    local_files_only: bool = True
    enabled: bool = True


DEFAULT_MODELS = ModelsConfig(
    text=ModelSpec("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"),
    image=ModelSpec("sentence-transformers/clip-ViT-B-32", "327ab6726d33c0e22f920c83f2ff9e4bd38ca37f"),
    clip_text=ModelSpec("sentence-transformers/clip-ViT-B-32-multilingual-v1", "58edf8cada9e398793dca955574a48cbb7f18be2"),
)


class TextEncoder(Protocol):
    model_id: str
    max_tokens: int

    def count_tokens(self, text: str) -> int: ...
    def encode(self, texts: Sequence[str]) -> np.ndarray: ...


class ImageEncoder(Protocol):
    model_id: str

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray: ...


def pad_to_square(image: Image.Image) -> Image.Image:
    """保持比例补边成正方形（固定灰色），模型内部再缩放；原图本身不被修改。"""
    image = image.convert("RGB")
    w, h = image.size
    side = max(w, h)
    if w == h:
        return image
    canvas = Image.new("RGB", (side, side), _PAD_COLOR)
    canvas.paste(image, ((side - w) // 2, (side - h) // 2))
    return canvas


_torch_lock = threading.Lock()
_threads_set = False


def _load(spec: ModelSpec, cfg: ModelsConfig):
    global _threads_set
    if cfg.local_files_only:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import torch
    from sentence_transformers import SentenceTransformer

    with _torch_lock:
        if not _threads_set:
            torch.set_num_threads(max(1, cfg.threads))
            _threads_set = True
    return SentenceTransformer(spec.repo, revision=spec.revision, device="cpu", local_files_only=cfg.local_files_only)


class StText:
    """sentence-transformers 文本编码器。encode 返回 L2 归一化 float32。"""

    def __init__(self, spec: ModelSpec, cfg: ModelsConfig):
        self._m = _load(spec, cfg)
        self._lock = threading.Lock()  # 页面多会话共享同一实例时串行推理
        self.model_id = spec.model_id
        self.max_tokens = int(self._m.max_seq_length or 128)
        self.batch_size = cfg.batch_size
        get_dim = getattr(self._m, "get_embedding_dimension", None) or self._m.get_sentence_embedding_dimension
        self.dim = int(get_dim() or 0)

    def count_tokens(self, text: str) -> int:
        tok = getattr(self._m, "tokenizer", None)
        if tok is None:
            return len(text.split()) + 2
        return len(tok(text, add_special_tokens=True, truncation=False)["input_ids"])

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        with self._lock:
            vec = self._m.encode(list(texts), batch_size=self.batch_size, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vec, dtype=np.float32)


class StImage:
    def __init__(self, spec: ModelSpec, cfg: ModelsConfig):
        self._m = _load(spec, cfg)
        self._lock = threading.Lock()
        self.model_id = spec.model_id
        self.batch_size = cfg.batch_size

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        prepared = [pad_to_square(im) for im in images]
        with self._lock:
            vec = self._m.encode(prepared, batch_size=self.batch_size, normalize_embeddings=True,
                                 convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vec, dtype=np.float32)


def image_model_id(cfg: ModelsConfig) -> str:
    """全图特征集身份：图像编码器；画面查询必须用与之配对的多语言 CLIP 文本编码器。"""
    return cfg.image.model_id


def download(cfg: ModelsConfig = DEFAULT_MODELS) -> list[str]:
    from huggingface_hub import snapshot_download

    out = []
    for spec in (cfg.text, cfg.image, cfg.clip_text):
        path = snapshot_download(spec.repo, revision=spec.revision, allow_patterns=ALLOW_PATTERNS)
        out.append(f"{spec.model_id} -> {path}")
    return out


def available(cfg: ModelsConfig) -> list[str]:
    """检查三套模型是否已在本机缓存；返回缺失项说明（空列表表示齐全）。"""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return ["未安装 sentence-transformers / huggingface_hub（pip install -r requirements.txt）"]
    missing = []
    for spec in (cfg.text, cfg.image, cfg.clip_text):
        try:
            snapshot_download(spec.repo, revision=spec.revision, allow_patterns=ALLOW_PATTERNS, local_files_only=True)
        except Exception:
            missing.append(spec.model_id)
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.models")
    ap.add_argument("--download", action="store_true", help="联网下载锁定版本的三套模型")
    args = ap.parse_args(argv)
    from .config import load_config

    cfg = load_config().models
    if args.download:
        for line in download(cfg):
            print(line)
    missing = available(cfg)
    print("模型齐全" if not missing else "缺少：" + "、".join(missing))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
