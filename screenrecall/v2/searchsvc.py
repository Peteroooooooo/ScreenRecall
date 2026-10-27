"""主进程的检索服务：词法 + 语义（向量）两路，给网页 /api/v2/search 和 MCP 的 search 共用。

- 查询模型 LazyTextEncoder：第一次语义检索时加载，闲置 cfg.encoder.idle_unload_s 秒后卸载；
- 文字向量库 <data_dir>/vectors/text（空闲整理进程写，这里只读）：每次检索增量刷新（只读新增的行和墓碑）；
- 模型没下载、向量库还不存在或是空的、编码出错：只走词法分支，结果里 semantic = False；
- 向量命中余弦低于 min_vector_score 的不要（在融合和 limit 截断之前就丢掉）：
  随便输个词向量分支总能凑出 k 条，太不相关的放进结果只会干扰，还会把字面命中挤出前 limit。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from ..store.queries import Queries
from ..store.vectors import VectorStore

log = logging.getLogger("screenrecall.v2.search")

TEXT_VECTORS = ("vectors", "text")
IMAGE_VECTORS = ("vectors", "image")
MIN_VECTOR_SCORE = 0.45
MODEL_CHECK_TTL_S = 60.0


def text_vectors_dir(data_dir: Path | str) -> Path:
    return Path(data_dir).joinpath(*TEXT_VECTORS)


def image_vectors_dir(data_dir: Path | str) -> Path:
    return Path(data_dir).joinpath(*IMAGE_VECTORS)


class _ScoreFloor:
    """包一层向量库：search() 只返回余弦 >= floor 的命中。

    门槛必须在 Queries.search 融合、按 limit 截断之前生效：截断之后再丢低分的纯语义组，
    会把本来排得进前 limit 的字面命中挤掉（结果比实际少）。"""

    def __init__(self, store: VectorStore, floor: float):
        self.store, self.floor = store, floor

    def search(self, qv, **kw):
        return [h for h in self.store.search(qv, **kw) if float(h.score) >= self.floor]

    def __getattr__(self, name: str):
        return getattr(self.store, name)


def _default_model_check() -> bool:
    from ..text import encoder as enc

    try:
        enc.model_dir(local_files_only=True)
        return True
    except enc.EncoderModelMissing:
        return False


class SearchService:
    """线程安全：网页的多个请求线程、MCP 共用一个实例。"""

    def __init__(self, data_dir: Path | str, *, idle_unload_s: float = 300.0, threads: int = 2,
                 batch_size: int = 16, encoder: Any = None, vectors: VectorStore | None = None,
                 model_check: Callable[[], bool] | None = None, min_vector_score: float = MIN_VECTOR_SCORE,
                 clock: Callable[[], int] | None = None):
        self.data_dir = Path(data_dir)
        self.min_vector_score = float(min_vector_score)
        self._enc_kwargs = {"idle_unload_s": idle_unload_s, "threads": threads, "batch_size": batch_size}
        self._encoder = encoder              # 测试注入；否则第一次需要时建 LazyTextEncoder
        self._vectors = vectors
        self._model_check = model_check or _default_model_check
        self._model_ok: bool | None = None if encoder is None else True
        self._model_checked_at = 0.0
        self._lock = threading.Lock()
        kw = {"clock": clock} if clock else {}
        self.queries = Queries(self.data_dir, text_vectors=None, **kw)

    def close(self) -> None:
        self.queries.close()
        with self._lock:
            if self._vectors is not None:
                self._vectors.close()
            enc, self._encoder = self._encoder, None
        unload = getattr(enc, "unload", None)
        if callable(unload):
            unload()

    # ---------------- 语义分支是否可用 ----------------

    def _model_available(self) -> bool:
        now = time.monotonic()
        if self._model_ok is None or (not self._model_ok and now - self._model_checked_at > MODEL_CHECK_TTL_S):
            try:
                self._model_ok = bool(self._model_check())
            except Exception:  # noqa: BLE001 - 检查失败就当没有模型
                log.warning("检查查询模型失败", exc_info=True)
                self._model_ok = False
            self._model_checked_at = now
        return bool(self._model_ok)

    def _get_vectors(self) -> VectorStore | None:
        with self._lock:
            if self._vectors is None:
                root = text_vectors_dir(self.data_dir)
                if not (root / "manifest.json").is_file():
                    return None
                try:
                    dim = int(json.loads((root / "manifest.json").read_text(encoding="utf-8"))["dim"])
                    self._vectors = VectorStore(root, dim, readonly=True)
                except Exception:  # noqa: BLE001 - 向量库坏了不影响词法检索
                    log.warning("打开文字向量库失败", exc_info=True)
                    return None
            return self._vectors

    def _get_encoder(self):
        with self._lock:
            if self._encoder is None:
                from ..text.encoder import LazyTextEncoder

                self._encoder = LazyTextEncoder(**self._enc_kwargs)
            return self._encoder

    def semantic_ready(self) -> bool:
        """模型在、向量库里有东西：这次检索会走语义分支。"""
        if not self._model_available():
            return False
        vs = self._get_vectors()
        if vs is None:
            return False
        try:
            vs.refresh()
            return len(vs) > 0
        except Exception:  # noqa: BLE001
            log.warning("刷新文字向量库失败", exc_info=True)
            return False

    @property
    def encoder_loaded(self) -> bool:
        return bool(getattr(self._encoder, "loaded", False))

    # ---------------- 检索 ----------------

    def search(self, query: str, *, time_from: int | None = None, time_to: int | None = None,
               app: str | None = None, task: str | None = None, limit: int = 20, per_group: int = 3,
               exclude_apps: Sequence[str] = (), exclude_title_keywords: Sequence[str] = ()) -> dict:
        """Queries.search 的结果（dict），另加 semantic: bool。"""
        semantic = False
        encoder = None
        if query.strip() and self.semantic_ready():
            enc = self._get_encoder()
            dim = self._vectors.dim if self._vectors is not None else None

            def encoder(text: str) -> np.ndarray:  # noqa: F811 - 只给 Queries 用
                v = np.asarray(enc.encode([text]), dtype=np.float32).reshape(-1)
                if dim is not None and v.size != dim:
                    raise ValueError(f"查询向量维度 {v.size} 与向量库 {dim} 不符")
                return v

            self.queries.text_vectors = _ScoreFloor(self._vectors, self.min_vector_score)
            semantic = True
        kwargs = dict(time_from=time_from, time_to=time_to, app=app, task=task, limit=limit, per_group=per_group,
                      exclude_apps=tuple(exclude_apps), exclude_title_keywords=tuple(exclude_title_keywords))
        try:
            out = self.queries.search(query, encoder=encoder, **kwargs) if semantic else self._lexical(query, **kwargs)
        except Exception:  # noqa: BLE001 - 语义分支出错（模型加载失败等）：退回只用词法
            if not semantic:
                raise
            log.warning("语义检索失败，只用词法检索", exc_info=True)
            out, semantic = self._lexical(query, **kwargs), False
        out["semantic"] = semantic
        return out

    def _lexical(self, query: str, **kwargs) -> dict:
        # 不给 encoder（self.queries.encoder 也是 None）：Queries 只走全文索引，不管 text_vectors 有没有设置
        return self.queries.search(query, **kwargs)
