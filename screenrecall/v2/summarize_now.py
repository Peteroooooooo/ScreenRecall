"""页面上点"重新生成"（片段概括 / 当天概括）时，在主进程的后台线程里马上写，不等电脑空闲。

- 和空闲整理进程用同一套代码（work_llm.LlmStep：同样的提示词、屏幕文字当不可信数据整体编码成 JSON、
  命中"对 Agent 隐藏"或录制排除规则的不发给大模型、写之前核对用到的行还在）；
- 一次只写一个：请求排进队列（同一个目标只排一次），一个后台线程按顺序做；
- 片段：先登记空闲整理的待办（主进程中途退出，空闲时照样会写），再在这里领走、马上写；
- 当天：先把这一天还没写好的片段概括写了（最多 MAX_DAY_PIECES 个），再从片段概括写天概括；
  这一天已经没有能写的片段概括（都删了 / 都不能发）时，把清空了的旧天概括整行删掉；
- 写完（成功或失败）都加一次 revision，开着的页面据此刷新。
日志只记数量和错误类型，不记屏幕内容。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Callable

from ..timeutil import local_dates_to_utc_range
from .work_common import Ctx, StopFlag, Stopped
from .work_llm import PIECE_JOB, LlmStep

log = logging.getLogger("screenrecall.v2.summarize_now")
MAX_DAY_PIECES = 30
FAILED_KEEP = 200


def _noop(_conn) -> None:
    return None


def _delete_empty_day(conn, key: str) -> int:
    return conn.execute("DELETE FROM summaries WHERE level = 'day' AND key = ? AND text = ''", (key,)).rowcount


class SummaryRunner:
    """target = ("piece", piece_id) 或 ("day", "YYYY-MM-DD")。"""

    def __init__(self, cfg, writer, llm_factory: Callable[[StopFlag], object], *, clock: Callable[[], int]):
        self.cfg, self.writer, self.clock = cfg, writer, clock
        self._llm_factory = llm_factory
        self._stop = StopFlag()
        self._q: deque = deque()
        self._running: tuple | None = None
        self._failed: dict[tuple, str] = {}
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None

    # ---------- 对外 ----------

    def submit(self, target: tuple) -> str:
        """排队；返回 'running' / 'queued'（已在做或已在队列里也一样返回）。"""
        target = self._norm(target)
        with self._cv:
            self._failed.pop(target, None)
            if self._running == target:
                return "running"
            if target not in self._q:
                self._q.append(target)
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="summary-now", daemon=True)
                self._thread.start()
            self._cv.notify_all()
            return "queued"

    def state(self, target: tuple) -> str | None:
        """'running' / 'queued' / 'failed'（这次马上写没写成）/ None。"""
        target = self._norm(target)
        with self._cv:
            if self._running == target:
                return "running"
            if target in self._q:
                return "queued"
            return "failed" if target in self._failed else None

    def busy(self) -> bool:
        with self._cv:
            return self._running is not None or bool(self._q)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """测试用：等队列做完。"""
        with self._cv:
            return self._cv.wait_for(lambda: self._running is None and not self._q, timeout)

    def close(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cv:
            self._q.clear()
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

    @staticmethod
    def _norm(target: tuple) -> tuple:
        kind, key = target
        return (kind, int(key)) if kind == "piece" else (kind, str(key))

    # ---------- 后台线程 ----------

    def _loop(self) -> None:
        ctx = Ctx(self.cfg, self.writer, self._stop, clock=self.clock)
        try:
            while not self._stop.is_set():
                with self._cv:
                    if not self._q:
                        self._running = None
                        self._cv.notify_all()
                        self._cv.wait(30.0)
                        if not self._q:
                            self._thread = None  # 空闲 30 秒：线程退出，下次 submit 再开一个
                            return
                    target = self._q.popleft()
                    self._running = target
                ok, why = False, "error"
                try:
                    ok, why = self._run(ctx, target)
                except Stopped:
                    return
                except Exception as exc:  # noqa: BLE001 - 一个目标出错不影响后面的
                    log.warning("马上写概括出错：%s", type(exc).__name__)
                finally:
                    with self._cv:
                        self._running = None
                        if not ok and why != "no_text":  # 没有可写的不算失败：页面按库里的情况说
                            self._failed[target] = why
                            while len(self._failed) > FAILED_KEEP:
                                self._failed.pop(next(iter(self._failed)))
                        self._cv.notify_all()
                    try:
                        self.writer.call(_noop).result(30)  # 页面据 revision 刷新
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            ctx.close()
            with self._cv:
                self._running = None
                if self._thread is threading.current_thread():
                    self._thread = None
                self._cv.notify_all()

    def _step(self, ctx: Ctx) -> LlmStep:
        step = LlmStep(ctx, lambda: self._llm_factory(self._stop))
        if step.client() is None:
            raise _NoLlm()
        return step

    def _run(self, ctx: Ctx, target: tuple) -> tuple[bool, str]:
        kind, key = target
        try:
            step = self._step(ctx)
        except _NoLlm:
            return False, "no_llm"
        if kind == "piece":
            return self._piece(ctx, step, int(key))
        return self._day(ctx, step, str(key))

    def _piece_row(self, ctx: Ctx, pid: int):
        return ctx.conn.execute(
            "SELECT p.*, s.status AS s_status FROM pieces p LEFT JOIN summaries s ON s.level = 'piece' "
            "AND s.key = CAST(p.piece_id AS TEXT) WHERE p.piece_id = ?", (pid,)).fetchone()

    def _piece(self, ctx: Ctx, step: LlmStep, pid: int) -> tuple[bool, str]:
        p = self._piece_row(ctx, pid)
        if p is None:
            return False, "gone"
        if ctx.private().match(p["app"], p["title"]):
            return False, "private"
        # LlmStep._piece：登记待办 → 领走（running，空闲整理进程不会同时写这一段）→ 写 → 完成 / 失败退避
        ok = step._piece(p)
        if not ok:
            # 失败已按空闲整理的规则记在待办里（退避后重试）；这次马上写算没写成
            return False, "failed"
        return True, "ok"

    def _day(self, ctx: Ctx, step: LlmStep, key: str) -> tuple[bool, str]:
        d = datetime.strptime(key, "%Y-%m-%d").date()
        lo, hi = local_dates_to_utc_range(d, d)
        private = ctx.private()
        # 这一天还没写好（没有 / 被删过标 stale）的片段概括先写；进行中的片段也写（用户要的是现在的情况）
        rows = ctx.conn.execute(
            "SELECT p.*, s.status AS s_status, j.status AS j_status, j.updated_ms AS j_updated FROM pieces p "
            "LEFT JOIN summaries s ON s.level = 'piece' AND s.key = CAST(p.piece_id AS TEXT) "
            "LEFT JOIN jobs j ON j.kind = ? AND j.target = CAST(p.piece_id AS TEXT) "
            "WHERE p.start_ms >= ? AND p.start_ms < ? AND (s.summary_id IS NULL OR s.status = 'stale') "
            "ORDER BY p.end_ms DESC LIMIT ?", (PIECE_JOB, lo, hi, MAX_DAY_PIECES * 2)).fetchall()
        done = 0
        for p in rows:
            if done >= MAX_DAY_PIECES:
                break
            ctx.stop.check()
            if private.match(p["app"], p["title"]):
                continue
            if p["j_status"] in ("done", "failed") and int(p["j_updated"] or 0) >= int(p["updated_ms"]) and (
                    p["s_status"] is None or p["j_status"] == "failed"):
                continue  # 写过了：没有字可写，或多次失败；片段之后没变
            step._piece(p)
            done += 1
        if step._day(key):
            return True, "ok"
        has = ctx.conn.execute(
            "SELECT p.app, p.title FROM pieces p JOIN summaries s ON s.level = 'piece' "
            "AND s.key = CAST(p.piece_id AS TEXT) AND s.status = 'ok' AND s.text != '' "
            "WHERE p.start_ms >= ? AND p.start_ms < ?", (lo, hi)).fetchall()
        if not any(not private.match(r["app"], r["title"]) for r in has):
            # 没有能写进天概括的片段概括（都删了、都没有字、都不能发）：清空了的旧天概括不再留着
            self.writer.call(_delete_empty_day, key).result(30)
            return False, "no_text"
        return False, "failed"


class _NoLlm(Exception):
    pass
