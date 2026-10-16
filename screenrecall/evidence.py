"""证据浏览：围绕用户选中的那次出现，在同一录制段内前后浏览（方案 5.2、6.1）。

规则：
- 时间线只由选中出现所在的 session 构成，按 first_seen_ms 排序；不会因为同一帧在别的
  session 也出现过，就把其他录制段拼进来当成连续录像；
- 相邻两次出现之间若有已知缺口（锁屏、最小化、取不到画面、排除规则、变化未保存等），
  在跨过去时明确提示；缺口未结束（ended_at_ms 为空）时视为延续到下一次出现或录制段结束；
- 走到录制段的头或尾时说明段的开始 / 结束原因（暂停、切换窗口、后台退出……），不自动跳到
  别的录制段；
- 原图不可用（过期、缺失、删除）的出现仍保留在时间线上，页面据此说明，不生成虚构预览；
- 并排比较只放两张原图，不计算表格或文字差异。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

GAP_LABELS = {
    "locked": "屏幕锁定",
    "minimized": "窗口最小化",
    "hidden": "窗口隐藏",
    "blank_capture": "取不到窗口内容",
    "empty_rect": "窗口尺寸为空",
    "rect_failed": "无法读取窗口位置",
    "gdi_failed": "截图调用失败",
    "capture_error": "截图出错",
    "excluded": "命中排除规则",
    "unsaved_change": "画面有变化但未保存",
}

SESSION_END_LABELS = {
    "paused": "用户暂停",
    "switch_target": "切换了录制窗口",
    "target_invalid": "目标窗口失效",
    "target_excluded": "目标窗口命中排除规则",
    "window_gone": "目标窗口已关闭",
    "storage_full": "存储空间不足，已暂停",
    "daemon_stop": "后台退出",
    "import": "导入批次",
}


@dataclass(frozen=True)
class Gap:
    gap_id: int
    started_at_ms: int
    ended_at_ms: int | None
    reason: str
    detail: str | None = None

    @property
    def label(self) -> str:
        return GAP_LABELS.get(self.reason, self.reason)


@dataclass(frozen=True)
class Step:
    """时间线上的一次出现，以及它对应画面的可用状态。"""

    occurrence_id: str
    frame_id: str
    first_seen_ms: int
    last_seen_ms: int
    observations: int
    app: str | None
    window_title: str | None
    source_type: str
    image_path: str
    image_status: str  # ok / missing / expired（deleted 的帧不进入时间线）
    width: int
    height: int


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    started_at_ms: int
    ended_at_ms: int | None
    end_reason: str | None
    target_process: str | None
    target_title: str | None
    is_active: bool  # 后台当前仍在这一段录制
    source_type: str = "live"  # live 实时录制 / import 导入批次

    @property
    def is_import(self) -> bool:
        return self.source_type == "import"

    @property
    def end_label(self) -> str:
        if self.ended_at_ms is None:
            return "仍在录制" if self.is_active else "录制段未正常结束（后台可能异常退出）"
        return SESSION_END_LABELS.get(self.end_reason or "", self.end_reason or "已结束")


@dataclass
class Timeline:
    session: SessionInfo
    steps: list[Step]
    # between[i] 是 steps[i] 与 steps[i+1] 之间的缺口；head / tail 是段首到第一步、最后一步到段尾的缺口
    between: list[list[Gap]]
    head: list[Gap] = field(default_factory=list)
    tail: list[Gap] = field(default_factory=list)

    def index_of(self, occurrence_id: str) -> int | None:
        for i, s in enumerate(self.steps):
            if s.occurrence_id == occurrence_id:
                return i
        return None


@dataclass(frozen=True)
class Context:
    """选中出现的前后关系，供页面直接渲染。"""

    timeline: Timeline
    index: int
    current: Step
    prev: Step | None
    next: Step | None
    gaps_before: list[Gap]  # prev 与 current 之间；prev 为空时是段首到 current
    gaps_after: list[Gap]  # current 与 next 之间；next 为空时是 current 到段尾

    @property
    def at_session_start(self) -> bool:
        return self.prev is None

    @property
    def at_session_end(self) -> bool:
        return self.next is None


def _overlapping(gaps: list[Gap], lo: int, hi: int | None) -> list[Gap]:
    """与开区间 (lo, hi) 有交集的缺口；hi 为 None 表示一直到段尾。"""
    out = []
    for g in gaps:
        starts_before_hi = hi is None or g.started_at_ms < hi
        ends_after_lo = g.ended_at_ms is None or g.ended_at_ms > lo
        if starts_before_hi and ends_after_lo:
            out.append(g)
    return out


def load_timeline(conn: sqlite3.Connection, session_id: str) -> Timeline | None:
    s = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if s is None:
        return None
    active = conn.execute("SELECT active_session_id FROM control WHERE id = 1").fetchone()
    session = SessionInfo(
        session_id=s["session_id"],
        started_at_ms=s["started_at_ms"],
        ended_at_ms=s["ended_at_ms"],
        end_reason=s["end_reason"],
        target_process=s["target_process"],
        target_title=s["target_title"],
        is_active=bool(active and active["active_session_id"] == session_id),
        source_type=s["source_type"],
    )
    steps = [
        Step(
            occurrence_id=r["occurrence_id"],
            frame_id=r["frame_id"],
            first_seen_ms=r["first_seen_ms"],
            last_seen_ms=r["last_seen_ms"],
            observations=r["observations"],
            app=r["app"],
            window_title=r["window_title"],
            source_type=r["source_type"],
            image_path=r["image_path"],
            image_status=r["image_status"],
            width=r["width"],
            height=r["height"],
        )
        for r in conn.execute(
            "SELECT o.*, f.image_path, f.image_status, f.width, f.height "
            "FROM occurrences o JOIN frames f ON f.frame_id = o.frame_id "
            "WHERE o.session_id = ? AND f.image_status != 'deleted' "
            "ORDER BY o.first_seen_ms, o.occurrence_id",
            (session_id,),
        )
    ]
    if session.is_import:
        # 导入批次按文件顺序排列；时间可能是同一个导入时间，不能据此排序（方案 6.8）
        seq = {
            r[0]: r[1] for r in conn.execute(
                "SELECT i.frame_id, i.seq FROM import_items i JOIN import_batches b ON b.batch_id = i.batch_id "
                "WHERE b.session_id = ? AND i.frame_id IS NOT NULL", (session_id,)
            )
        }
        steps.sort(key=lambda st: (seq.get(st.frame_id, 1 << 30), st.first_seen_ms, st.occurrence_id))
    gaps = [
        Gap(r["gap_id"], r["started_at_ms"], r["ended_at_ms"], r["reason"], r["detail"])
        for r in conn.execute(
            "SELECT * FROM gaps WHERE session_id = ? ORDER BY started_at_ms, gap_id", (session_id,)
        )
    ]
    between = [_overlapping(gaps, a.last_seen_ms, b.first_seen_ms) for a, b in zip(steps, steps[1:])]
    if steps:
        head = _overlapping(gaps, session.started_at_ms - 1, steps[0].first_seen_ms)
        tail = _overlapping(gaps, steps[-1].last_seen_ms, session.ended_at_ms)
    else:
        head, tail = list(gaps), []
    return Timeline(session, steps, between, head, tail)


def context(conn: sqlite3.Connection, occurrence_id: str) -> Context | None:
    """选中出现在其录制段内的前后关系；出现不存在或其画面已删除时返回 None。"""
    r = conn.execute("SELECT session_id FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
    if r is None:
        return None
    tl = load_timeline(conn, r["session_id"])
    if tl is None:
        return None
    i = tl.index_of(occurrence_id)
    if i is None:
        return None
    prev = tl.steps[i - 1] if i > 0 else None
    nxt = tl.steps[i + 1] if i + 1 < len(tl.steps) else None
    return Context(
        timeline=tl,
        index=i,
        current=tl.steps[i],
        prev=prev,
        next=nxt,
        gaps_before=tl.between[i - 1] if prev else tl.head,
        gaps_after=tl.between[i] if nxt else tl.tail,
    )


def describe_gaps(gaps: list[Gap]) -> str:
    """把一组缺口合成一句提示；同类缺口合并计数（unsaved_change 往往成串出现）。"""
    if not gaps:
        return ""
    counts: dict[str, int] = {}
    for g in gaps:
        counts[g.label] = counts.get(g.label, 0) + 1
    parts = [f"{label}×{n}" if n > 1 else label for label, n in counts.items()]
    return "中间有已知缺口：" + "、".join(parts) + "。这段时间的画面没有保存，不能当成连续录像。"
