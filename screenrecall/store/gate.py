"""向量命中的"是不是真的相关"判定（检索的语义分支用，网页和 MCP 共用，见 Queries._vector_groups）。

问题：小模型的余弦分数在不同查询、不同行之间不可比。屏幕上有很多"和什么都有点像"的行
（聊天里的"韩梅梅 10:07"、文章标题"向量检索入门"、"October 2026"），不相关的查询最高分常常落在它们身上，
而且能到 0.6-0.7，比不少真正相关的跨语言命中还高；固定门槛挡不住。

做法（hubness 修正，类似 CSLS）：
- 一组固定的泛泛的查询 PROBES（中英文、长短都有，话题刻意避开常见的屏幕内容）；
- 命中行的"凑数程度" r_h = 它和 PROBES 里最像的 k 条的平均相似度（< 0 记 0）；
- 修正分 = 余弦 - r_h；要修正分 >= floor，并且离这次查询修正分最高的那条不超过 margin；
- 查询里一个字母 / 汉字都没有（纯数字、符号）：不走语义分支，只用字面命中（"13800138000" 和日志行
  的余弦能到 0.7，数字串没有可比的"意思"）。
门槛在合成评测 experiments/search 上定（tune 一半调、heldout 一半报数，见 gate_sweep.py）。
每次检索多算 候选数 × len(PROBES) 次点积（几百 × 50），不到 1 ms；PROBES 的向量由检索服务用同一个查询模型算一次后缓存。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

PROBES: tuple[str, ...] = (
    "origami paper crane instructions", "Roman empire history", "best tennis racket for beginners",
    "how to clean a leather sofa", "polar bear population", "learn Spanish grammar", "wildfire smoke air quality",
    "chess opening strategies", "how to make a paper airplane", "ballet shoes", "skiing in the Alps",
    "lighthouse keeper", "pirate ship", "kangaroo facts", "violin strings", "camping tent", "pottery wheel",
    "surfing waves", "medieval castle", "golf swing tips", "dinosaur fossils", "camera lens aperture",
    "vintage typewriter",
    "怎么折纸鹤", "古罗马历史", "网球拍怎么选", "皮沙发怎么清洁", "北极熊的数量", "西班牙语语法", "国际象棋开局",
    "滑雪装备", "灯塔", "海盗船", "袋鼠", "小提琴", "露营帐篷", "陶艺", "冲浪", "中世纪城堡", "高尔夫",
    "恐龙化石", "相机光圈", "老式打字机",
    "hello", "something", "你好", "这个", "365", "2500", "第三页",
)


def has_letters(query: str) -> bool:
    """查询里有没有字母或汉字（纯数字、符号的查询不走语义分支）。"""
    return any(ch.isalpha() for ch in query)


@dataclass(frozen=True)
class GateConfig:
    # 合成评测 tune 一半上、不相关查询误报 <= 3% 时找到最多的一组（experiments/search/gate_sweep.py）
    floor: float = 0.44              # 修正分门槛
    margin: float = 0.10             # 离最高修正分不超过多少
    floor_with_literal: float = 0.44  # 有字面命中时，只有语义命中的组要达到的修正分
    k: int = 5                       # r_h：和 PROBES 里最像的 k 条取平均


class VectorGate:
    """probe_vectors：PROBES 用查询模型编码后的向量（行向量已归一化），[len(PROBES), dim]。"""

    def __init__(self, probe_vectors: np.ndarray, config: GateConfig = GateConfig()):
        p = np.asarray(probe_vectors, dtype=np.float32)
        if p.ndim != 2 or p.shape[0] == 0:
            raise ValueError("probe_vectors 应为非空的二维数组")
        self.probes = p
        self.config = config

    @property
    def dim(self) -> int:
        return int(self.probes.shape[1])

    def hubness(self, vecs: np.ndarray) -> np.ndarray:
        """每行向量和 PROBES 里最像的 k 条的平均相似度（< 0 记 0）。"""
        v = np.asarray(vecs, dtype=np.float32)
        if v.size == 0:
            return np.zeros(0, np.float32)
        sims = v.reshape(-1, self.dim) @ self.probes.T
        k = max(1, min(int(self.config.k), sims.shape[1]))
        top = np.partition(sims, sims.shape[1] - k, axis=1)[:, -k:]
        return np.maximum(top.mean(axis=1), 0.0)

    def select(self, scores: Sequence[float], vecs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """余弦 + 命中行的向量 → (留下的下标（按修正分从高到低）, 对应的修正分)。"""
        s = np.asarray(scores, dtype=np.float32).reshape(-1)
        if s.size == 0:
            return np.zeros(0, np.int64), np.zeros(0, np.float32)
        adj = s - self.hubness(vecs)
        cut = max(float(self.config.floor), float(adj.max()) - float(self.config.margin))
        keep = np.flatnonzero(adj >= cut)
        keep = keep[np.argsort(-adj[keep], kind="stable")]
        return keep, adj[keep]
