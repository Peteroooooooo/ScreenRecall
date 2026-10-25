"""失败待办的人工重试（方案 3.4 第 5 条"单帧失败标记并允许重试，不阻塞后续待办"）。

自动重试由各 worker 自己做（OCR 按 max_attempts 退避，向量按 embed_max_attempts）；用尽后待办停在 failed。
这里给页面和命令行一个明确的人工入口：把仍然有意义的失败待办改回 pending、清零尝试次数，worker 下一轮
（OCR 每秒轮询）自然领取，不需要重启后台。

只重试"重做可能成功"的：画面没被删除、原图仍可用、待办的目标版本仍是画面当前版本。
图片已缺失 / 已到期 / 已删除的失败不能靠重试恢复，列为不可重试并说明原因，不改它们。
OCR 重试成功后，ocr worker 会按新文字重新登记文本 / 全图向量待办（vectors.register_embed_jobs）。

命令行：python -m screenrecall.retry --data-dir DIR [--list] [--stage ocr|text_embed|image_embed]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field

from . import db
from .timeutil import utc_ms

STAGES = ("ocr", "text_embed", "image_embed")
_STATUS_COL = {"ocr": "ocr_status", "text_embed": "text_embed_status", "image_embed": "image_embed_status"}


@dataclass
class FailedJob:
    job_id: int
    frame_id: str
    stage: str
    attempts: int
    last_error: str
    updated_at_ms: int
    captured_at_ms: int
    app: str
    window_title: str
    retryable: bool
    why_not: str

    @property
    def error_code(self) -> str:
        return (self.last_error or "").split(":", 1)[0].strip() or "unknown"


@dataclass
class RetryResult:
    requeued: Counter = field(default_factory=Counter)      # stage → 数量
    not_retryable: Counter = field(default_factory=Counter)  # 原因 → 数量

    @property
    def total(self) -> int:
        return sum(self.requeued.values())


_LIST_SQL = """
SELECT j.job_id, j.frame_id, j.stage, j.attempts, COALESCE(j.last_error, '') AS last_error, j.updated_at_ms,
       j.target_version, f.captured_at_ms, f.image_status, f.record_version,
       (SELECT o.app FROM occurrences o WHERE o.frame_id = f.frame_id ORDER BY o.last_seen_ms DESC LIMIT 1) AS app,
       (SELECT o.window_title FROM occurrences o WHERE o.frame_id = f.frame_id
        ORDER BY o.last_seen_ms DESC LIMIT 1) AS window_title
FROM index_jobs j JOIN frames f ON f.frame_id = j.frame_id
WHERE j.status = 'failed' {stage}
ORDER BY j.updated_at_ms DESC, j.job_id DESC
"""


def _why_not(row: sqlite3.Row) -> str:
    if row["image_status"] != "ok":
        return {"missing": "原图文件缺失", "expired": "原图已到期清理", "deleted": "画面已删除"}.get(
            row["image_status"], f"原图状态 {row['image_status']}")
    if row["record_version"] != row["target_version"]:
        return "画面已更新，旧待办作废"
    return ""


def failed_jobs(conn: sqlite3.Connection, *, stage: str | None = None, limit: int | None = None) -> list[FailedJob]:
    if stage is not None and stage not in STAGES:
        raise ValueError(f"stage 只能是 {STAGES}")
    sql = _LIST_SQL.format(stage="AND j.stage = ?" if stage else "") + (" LIMIT ?" if limit else "")
    params: list = ([stage] if stage else []) + ([limit] if limit else [])
    out = []
    for r in conn.execute(sql, params):
        why = _why_not(r)
        out.append(FailedJob(r["job_id"], r["frame_id"], r["stage"], r["attempts"], r["last_error"],
                             r["updated_at_ms"], r["captured_at_ms"], r["app"] or "", r["window_title"] or "",
                             not why, why))
    return out


def summary(conn: sqlite3.Connection) -> dict:
    """{stage: {"retryable": n, "not_retryable": n, "errors": {错误码: n}}}，给页面显示。"""
    out: dict = {}
    for j in failed_jobs(conn):
        s = out.setdefault(j.stage, {"retryable": 0, "not_retryable": 0, "errors": Counter()})
        s["retryable" if j.retryable else "not_retryable"] += 1
        s["errors"][j.error_code] += 1
    return out


def retry_failed(conn: sqlite3.Connection, *, stage: str | None = None,
                 job_ids: list[int] | None = None) -> RetryResult:
    """把可重试的失败待办改回 pending（尝试次数清零、立即可领），并把画面上对应阶段的状态改回 pending。

    整个操作在一个写事务里完成；检查与修改在同一事务中，期间画面被删除的待办不会被改回。
    """
    res = RetryResult()
    now = utc_ms()
    wanted = set(job_ids) if job_ids is not None else None
    with db.write_txn(conn):
        for j in failed_jobs(conn, stage=stage):
            if wanted is not None and j.job_id not in wanted:
                continue
            if not j.retryable:
                res.not_retryable[j.why_not] += 1
                continue
            n = conn.execute(
                "UPDATE index_jobs SET status = 'pending', attempts = 0, worker_token = NULL, not_before_ms = 0, "
                "updated_at_ms = ?, last_error = '人工重试：' || COALESCE(last_error, '') "
                "WHERE job_id = ? AND status = 'failed'",
                (now, j.job_id),
            ).rowcount
            if not n:
                continue
            conn.execute(f"UPDATE frames SET {_STATUS_COL[j.stage]} = 'pending' WHERE frame_id = ? "
                         f"AND {_STATUS_COL[j.stage]} = 'failed'", (j.frame_id,))
            res.requeued[j.stage] += 1
    return res


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m screenrecall.retry", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir")
    ap.add_argument("--stage", choices=STAGES)
    ap.add_argument("--list", action="store_true", help="只列出失败待办，不重试")
    args = ap.parse_args(argv)
    cfg = load_config(args.data_dir)
    conn = db.connect(cfg.db_path)
    try:
        jobs = failed_jobs(conn, stage=args.stage)
        for j in jobs[:50]:
            flag = "可重试" if j.retryable else f"不可重试（{j.why_not}）"
            print(f"{j.stage:12s} {j.frame_id[:8]} 尝试 {j.attempts} 次 {flag}：{j.last_error[:120]}")
        if len(jobs) > 50:
            print(f"……共 {len(jobs)} 条")
        if args.list or not jobs:
            print(f"失败待办 {len(jobs)} 条，其中可重试 {sum(j.retryable for j in jobs)} 条")
            return 0
        res = retry_failed(conn, stage=args.stage)
        print(f"已改回待处理 {res.total} 条 {dict(res.requeued)}；不可重试 {dict(res.not_retryable)}。"
              "后台运行时会自动领取；没运行时用 python -m screenrecall.indexer 跑完")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
