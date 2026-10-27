"""空闲批次里的 CLIP 图片向量：进批次时加载，限制 torch 线程，出批次时释放。

CLIP 不能去掉（课程交付的内容分类器用它的图片向量），但只对存下来的证据帧算，
只在空闲整理进程里按批跑：

    with ClipBatch() as clip:
        vecs = clip.encode(images)   # float32 [n, 512]，L2 归一化

模型、预处理、model_id 都复用 screenrecall.models（clip-ViT-B-32 @ 锁定 revision + 补边成正方形），
所以向量与旧版完全相同，分类器和已有评测照样能用。
退出时丢掉模型并回收内存；空闲整理进程做完就退出，剩下的内存随进程一起还给系统。
"""

from __future__ import annotations

import gc
import threading
from dataclasses import dataclass, replace
from typing import Callable, Sequence

import numpy as np
from PIL import Image

from .. import models

_LOCK = threading.Lock()


@dataclass(frozen=True)
class ClipBatchConfig:
    threads: int = 4  # torch 推理线程上限（16 线程的机器上留出余量给前台程序）
    batch_size: int = 16
    local_files_only: bool = True


def _get_torch_threads() -> int | None:
    try:
        import torch
    except ImportError:
        return None
    return int(torch.get_num_threads())


def _set_torch_threads(n: int) -> None:
    import torch

    torch.set_num_threads(max(1, int(n)))


def _trim_working_set() -> None:
    """把已释放的内存页还给系统（Windows：EmptyWorkingSet），任务管理器里的内存马上降下来。"""
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet(k32.GetCurrentProcess())
    except (OSError, AttributeError):
        pass


def _as_pil(img) -> Image.Image:
    if isinstance(img, Image.Image):
        return img
    a = np.asarray(img)
    if a.ndim == 3 and a.shape[2] in (3, 4):
        return Image.fromarray(np.ascontiguousarray(a[:, :, 2::-1]))  # BGR(A) -> RGB
    if a.ndim == 2:
        return Image.fromarray(a).convert("RGB")
    raise ValueError(f"不支持的图像形状 {a.shape}")


class ClipBatch:
    """一次空闲批次的 CLIP 图片编码器。不能嵌套、不能跨线程共用（同一进程同时只有一个批次）。"""

    def __init__(self, cfg: ClipBatchConfig | None = None, models_cfg: models.ModelsConfig = models.DEFAULT_MODELS,
                 *, loader: Callable[[models.ModelSpec, models.ModelsConfig], object] | None = None):
        self.cfg = cfg or ClipBatchConfig()
        self._mcfg = replace(models_cfg, threads=self.cfg.threads, batch_size=self.cfg.batch_size,
                             local_files_only=self.cfg.local_files_only)
        self._loader = loader or models.StImage
        self._enc = None
        self._prev_threads: int | None = None
        self._held = False
        self.model_id = models.image_model_id(self._mcfg)

    def __enter__(self) -> "ClipBatch":
        if not _LOCK.acquire(blocking=False):
            raise RuntimeError("同一进程里已经有一个 CLIP 批次在运行")
        self._held = True
        try:
            self._prev_threads = _get_torch_threads()
            # models._load 只在进程里第一次加载时设线程数；这里每个批次都显式设一次
            if self._prev_threads is not None:
                _set_torch_threads(self.cfg.threads)
            self._enc = self._loader(self._mcfg.image, self._mcfg)
            if self._prev_threads is None:
                self._prev_threads = _get_torch_threads()
                if self._prev_threads is not None:
                    _set_torch_threads(self.cfg.threads)
        except BaseException:
            self._release()
            raise
        return self

    def encode(self, images: Sequence) -> np.ndarray:
        if self._enc is None:
            raise RuntimeError("ClipBatch 只能在 with 块内使用")
        if not images:
            return np.zeros((0, 512), dtype=np.float32)
        return np.asarray(self._enc.encode_images([_as_pil(im) for im in images]), dtype=np.float32)

    def __exit__(self, *exc) -> None:
        self._release()

    def _release(self) -> None:
        self._enc = None
        gc.collect()
        _trim_working_set()
        if self._prev_threads is not None:
            try:
                _set_torch_threads(self._prev_threads)
            except ImportError:
                pass
            self._prev_threads = None
        if self._held:
            self._held = False
            _LOCK.release()
