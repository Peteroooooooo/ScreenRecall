"""记录节奏：决定一次"与已保存内容不同"的观察现在是否保存（方案 4.1）。

本模块是纯逻辑，不调用窗口 API、不读写数据库，时间由调用方传入单调时钟读数，
便于用注入时钟验证限频、等待和到期（方案 4.1 末段的验收要求）。

两件事分开判断（v1.2）：
- 稳定判断只用于"结束等待"：相邻两次观察（同一捕获上下文）的变化像素比例低于阈值即视为稳定；
- 精确复用由调用方按原始像素完全一致判断，本模块不参与。
  因此单个数字改变即使低于阈值，也会作为新内容保存，不会被"小变化"过滤。

普通模式：
- 开启、恢复或切换目标后的第一次观察直接保存；
- 之后出现变化：若与上一次观察相比已稳定则保存，否则进入等待；
- 从等待起点累计 stable_timeout 秒后，首个到期采样点保存当前帧；持续变化不重置等待起点。
排错模式：
- 不等稳定；距上次自动保存不足 min_save_gap 秒时只保留最新候选（HOLD），到期后再保存。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NORMAL = "normal"
DEBUG = "debug"
MODES = (NORMAL, DEBUG)

# observe() 的返回值
SAVE = "save"  # 现在保存这次观察
WAIT = "wait"  # 普通模式等待稳定：不保存，但已观察的变化须记中断
HOLD = "hold"  # 排错模式限频：作为最新候选保留，允许时再保存
NONE = "none"  # 与已保存内容相同，无需保存


@dataclass(frozen=True)
class CadenceParams:
    stable_change_ratio: float = 0.01
    stable_timeout_s: float = 10.0
    debug_min_save_gap_s: float = 2.0

    @classmethod
    def from_capture_config(cls, cap) -> "CadenceParams":
        return cls(
            stable_change_ratio=cap.stable_change_ratio,
            stable_timeout_s=cap.stable_timeout_seconds,
            debug_min_save_gap_s=cap.debug_min_save_gap_seconds,
        )


def change_ratio(a: bytes, b: bytes, width: int, height: int) -> float:
    """两幅同尺寸紧凑 BGR 图中，任一通道不同的像素所占比例。"""
    n = width * height
    if n <= 0 or len(a) != n * 3 or len(b) != n * 3:
        return 1.0
    pa = np.frombuffer(a, dtype=np.uint8).reshape(n, 3)
    pb = np.frombuffer(b, dtype=np.uint8).reshape(n, 3)
    return float(np.count_nonzero((pa != pb).any(axis=1))) / n


class Cadence:
    """单个录制目标的节奏状态。调用方须在同一把锁或同一线程内使用。"""

    def __init__(self, params: CadenceParams):
        self.params = params
        self.mode = NORMAL
        self.last_auto_save: float | None = None  # 跨录制段保留：排错模式仍遵守自动保存间隔
        self.reset(direct=True)

    # ---------- 状态切换 ----------

    def reset(self, *, direct: bool) -> None:
        """开启、恢复或切换目标：重建比较基线。direct=True 时普通模式下一次变化直接保存。"""
        self.direct = direct
        self.last_obs: tuple[tuple, bytes] | None = None
        self.wait_start: float | None = None

    def rebaseline(self) -> None:
        """锁屏、最小化等缺口之后：旧观察不再可比，但不直接保存，按等稳规则重新开始。"""
        self.last_obs = None
        self.wait_start = None

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(mode)
        if mode != self.mode:
            self.mode = mode
            self.wait_start = None

    # ---------- 判断 ----------

    def debug_allowed(self, now: float) -> bool:
        return self.last_auto_save is None or now - self.last_auto_save >= self.params.debug_min_save_gap_s

    def waiting_since(self) -> float | None:
        return self.wait_start

    def observe(self, now: float, ctx: tuple, bgr: bytes, width: int, height: int, *, changed: bool) -> str:
        """记录一次观察并给出决定。

        ctx：比较上下文（窗口、进程、宽、高）；不同上下文之间不计算变化比例。
        changed：这次观察与当前已保存内容不同，且不能精确复用缓存中的内容。
        """
        prev = self.last_obs
        self.last_obs = (ctx, bgr)
        if not changed:
            self.wait_start = None
            return NONE

        if self.mode == DEBUG:
            return SAVE if self.debug_allowed(now) else HOLD

        if self.direct:
            return SAVE
        if prev is not None and prev[0] == ctx:
            if change_ratio(prev[1], bgr, width, height) < self.params.stable_change_ratio:
                return SAVE
        if self.wait_start is None:
            self.wait_start = now
            return WAIT
        if now - self.wait_start >= self.params.stable_timeout_s:
            return SAVE
        return WAIT

    def on_saved(self, now: float) -> None:
        """一次自动保存已被接受（图片落盘且登记提交）。"""
        self.direct = False
        self.wait_start = None
        self.last_auto_save = now

    def on_settled(self) -> None:
        """当前内容已由补拍或复用确定：结束等待，不影响自动保存限频。"""
        self.direct = False
        self.wait_start = None
