"""文本向量：MiniLM（paraphrase-multilingual-MiniLM-L12-v2）用 onnxruntime 推理。

模型仓库在锁定 revision 下自带官方导出的 onnx/model.onnx；用它 + tokenizers 分词，
再做和 sentence-transformers 一样的均值池化 + L2 归一化，向量与 torch 版一致（cos ≥ 0.9999），
model_id 与 screenrecall.models 的文本模型完全相同，已有的评测结果照样有效。
比起 torch：加载约 1.3 s（torch 约 14 s），进程内存少约 0.3 GB，不需要装 torch。

准备模型（联网一次）：python -m screenrecall.text.encoder --download；之后默认只用本地缓存。
本机 CPU 有 AVX2、没有 AVX-512：用 fp32 的 model.onnx，不用仓库里的量化版本（那些是给别的指令集的）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from ..models import DEFAULT_MODELS, ModelSpec

ONNX_FILE = "onnx/model.onnx"
ALLOW_PATTERNS = [
    ONNX_FILE, "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json",
    "sentence_bert_config.json", "modules.json", "1_Pooling/config.json",
]
DIM = 384


class EncoderModelMissing(RuntimeError):
    pass


def model_dir(spec: ModelSpec = DEFAULT_MODELS.text, *, local_files_only: bool = True) -> Path:
    """锁定 revision 的模型快照目录（只含 ONNX 推理需要的文件）。"""
    from huggingface_hub import snapshot_download

    try:
        p = snapshot_download(spec.repo, revision=spec.revision, allow_patterns=ALLOW_PATTERNS,
                              local_files_only=local_files_only)
    except Exception as exc:
        raise EncoderModelMissing(
            f"本机没有文本模型 {spec.model_id} 的 ONNX 文件。请先联网运行一次："
            f"python -m screenrecall.text.encoder --download（{type(exc).__name__}）"
        ) from exc
    p = Path(p)
    if not (p / ONNX_FILE).is_file() or not (p / "tokenizer.json").is_file():
        raise EncoderModelMissing(
            f"文本模型 {spec.model_id} 的缓存不完整（缺 {ONNX_FILE} 或 tokenizer.json）。"
            f"请联网运行：python -m screenrecall.text.encoder --download"
        )
    return p


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class OnnxTextEncoder:
    """与 screenrecall.models.StText 同接口：model_id / max_tokens / dim / count_tokens / encode。"""

    def __init__(self, spec: ModelSpec = DEFAULT_MODELS.text, *, threads: int = 2, batch_size: int = 16,
                 local_files_only: bool = True, mem_arena: bool = True):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        d = model_dir(spec, local_files_only=local_files_only)
        st_cfg = _read_json(d / "sentence_bert_config.json")
        pool = _read_json(d / "1_Pooling" / "config.json")
        if pool and not pool.get("pooling_mode_mean_tokens", True):
            raise EncoderModelMissing("该模型不是均值池化，不能用这个 ONNX 编码器")
        self.max_tokens = int(st_cfg.get("max_seq_length") or 128)
        self.model_id = spec.model_id
        self.batch_size = max(1, int(batch_size))
        self.dim = DIM

        # 只建一个分词器：25 万词表的 XLM-R 分词器每个实例约占 250 MiB
        self._tok = Tokenizer.from_file(str(d / "tokenizer.json"))
        self._tok.enable_truncation(max_length=self.max_tokens)
        pad_token = "<pad>"
        pad_id = self._tok.token_to_id(pad_token)
        if pad_id is None:
            pad_id = 1
        self._tok.enable_padding(pad_id=pad_id, pad_token=pad_token)

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, int(threads))
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        # 内存池：开着推理略快；关掉常驻内存少约 90 MiB（见 results/text/encoder_bench.json）
        so.enable_cpu_mem_arena = bool(mem_arena)
        self._sess = ort.InferenceSession(str(d / ONNX_FILE), sess_options=so, providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}
        self._lock = threading.Lock()  # 多会话共享同一实例时串行推理

    def count_tokens(self, text: str) -> int:
        """含首尾特殊符号、不截断的 token 数（与 StText.count_tokens 一致）。"""
        with self._lock:
            self._tok.no_truncation()
            try:
                return len(self._tok.encode(text, add_special_tokens=True).ids)
            finally:
                self._tok.enable_truncation(max_length=self.max_tokens)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        texts = [t if isinstance(t, str) else str(t) for t in texts]
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        if not texts:
            return out
        # 按长度排序再分批：每批补齐到批内最长，省去大量补齐位的计算（sentence-transformers 也这样做）
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
        with self._lock:
            for s in range(0, len(order), self.batch_size):
                idx = order[s:s + self.batch_size]
                out[idx] = self._encode_batch([texts[i] for i in idx])
        return out

    def _encode_batch(self, batch: list[str]) -> np.ndarray:
        encs = self._tok.encode_batch(batch, add_special_tokens=True)
        ids = np.asarray([e.ids for e in encs], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in encs], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._inputs:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._sess.run(None, feeds)[0]  # [b, L, 384]
        m = mask[:, :, None].astype(np.float32)
        summed = (hidden * m).sum(axis=1)
        counts = np.clip(m.sum(axis=1), 1e-9, None)
        vec = summed / counts
        norms = np.linalg.norm(vec, axis=1, keepdims=True)
        return (vec / np.clip(norms, 1e-12, None)).astype(np.float32)


class LazyTextEncoder:
    """主进程查询用：第一次 encode 时加载，闲置 idle_unload_s 秒后卸载释放内存。

    实测（10 轮加载 / 卸载）：工作集 880 MiB -> 卸载后约 85 MiB；但进程的已提交内存（private）
    停在约 550 MiB 不再增长（分配器留着的堆，系统可以换出），不随轮数增加。
    """

    def __init__(self, idle_unload_s: float = 300.0, **kwargs):
        self._kwargs = kwargs
        self._idle = float(idle_unload_s)
        self._enc: OnnxTextEncoder | None = None
        self._last = 0.0
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._max_tokens: int | None = None
        spec = kwargs.get("spec", DEFAULT_MODELS.text)
        self.model_id = spec.model_id
        self.dim = DIM

    @property
    def loaded(self) -> bool:
        return self._enc is not None

    def _get(self) -> OnnxTextEncoder:
        with self._lock:
            if self._enc is None:
                self._enc = OnnxTextEncoder(**self._kwargs)
                self._max_tokens = self._enc.max_tokens
            self._last = time.monotonic()
            self._arm()
            return self._enc

    @property
    def max_tokens(self) -> int:
        # 只读一个配置值：加载过一次就记住，卸载后再问不重新加载模型
        if self._max_tokens is None:
            self._get()
        return int(self._max_tokens)

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        return self._get().encode(texts)

    def count_tokens(self, text: str) -> int:
        return self._get().count_tokens(text)

    def unload(self) -> None:
        with self._lock:
            self._enc = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _arm(self) -> None:
        if self._idle <= 0 or self._timer is not None:
            return
        self._timer = threading.Timer(self._idle, self._maybe_unload)
        self._timer.daemon = True
        self._timer.start()

    def _maybe_unload(self) -> None:
        with self._lock:
            self._timer = None
            if self._enc is None:
                return
            if time.monotonic() - self._last >= self._idle:
                self._enc = None
                return
        with self._lock:
            self._arm()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.text.encoder")
    ap.add_argument("--download", action="store_true", help="联网下载锁定 revision 的 ONNX 模型与分词器")
    args = ap.parse_args(argv)
    if args.download:
        print(model_dir(local_files_only=False))
    try:
        model_dir(local_files_only=True)
    except EncoderModelMissing as exc:
        print(exc)
        return 1
    print("文本模型（ONNX）齐全")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    sys.exit(main())
