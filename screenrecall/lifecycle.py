"""收藏、删除、到期与配额（方案 5.2、5.4）。

规则（主方案 + 团队 2026-10-16 决定）：
- 收藏挂在"某一次出现"上：保护该出现记录及其画面的原图、文字、特征和来源，不随普通到期或配额清理；
  不能恢复收藏前已经过期或删除的原图。
- 用户明确"删除本次记录"：只移除这一次出现（连同它自己的收藏）；画面只在没有其他出现记录引用时才回收。
- 用户明确"删除此画面的全部记录"：先由 plan_frame_delete 列出将删除的出现记录与关联收藏，页面说明并确认后，
  delete_frame 才执行；确认后又出现了未展示的新收藏时拒绝执行，让页面重新确认。
- 到期与配额触发的自动清理一律保护收藏，也不淘汰已接受但 OCR 尚未处理的帧。
- 共享内容：一份画面可被多次出现引用，一个文本块可被多帧引用；只在最后一个有效引用消失后回收。
- 顺序：同一短事务里先做逻辑失效（删出现 / 标 deleted / 版本号 +1 / 取消待办 / 删 FTS 与向量 / 修订号 +1），
  提交后再删图片文件。处理中的 OCR 与向量任务写回前会校验版本号，已删数据不会重新入库。
- 这是应用数据联动删除，不承诺存储介质级不可恢复擦除。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import db, storage, vectors
from .timeutil import utc_ms

DAY_MS = 86_400_000
GIB = 1024 ** 3
MIB = 1024 ** 2

# 已接受但尚未完成 OCR 的帧：自动清理不能把它当普通旧记录淘汰（方案 5.2）
_OCR_UNFINISHED = (
    "EXISTS (SELECT 1 FROM index_jobs j WHERE j.frame_id = {f}.frame_id AND j.stage = 'ocr' "
    "AND j.status IN ('pending', 'running'))"
)
_FRAME_FAVORITED = "EXISTS (SELECT 1 FROM favorites fv WHERE fv.frame_id = {f}.frame_id)"


@dataclass(frozen=True)
class RetentionConfig:
    image_days: int = 30
    record_days: int = 90
    quota_bytes: int = 10 * GIB
    min_free_bytes: int = 2 * GIB
    reserve_bytes: int = 256 * MIB      # 写入前预留余量：不在磁盘已经耗尽后才维护
    maintenance_interval_seconds: float = 300.0
    batch: int = 200                    # 每个短写事务处理的条数


class PlanChanged(RuntimeError):
    """确认后数据有变化（出现了页面没有展示的收藏），需要重新确认。"""


# ---------------- 收藏 ----------------

def add_favorite(conn: sqlite3.Connection, occurrence_id: str, now_ms: int | None = None) -> bool:
    """收藏一次出现。画面已删除或出现记录不存在时返回 False。原图已过期仍可收藏文字与来源。"""
    now_ms = utc_ms() if now_ms is None else now_ms
    with db.write_txn(conn):
        row = conn.execute(
            "SELECT o.frame_id FROM occurrences o JOIN frames f ON f.frame_id = o.frame_id "
            "WHERE o.occurrence_id = ? AND f.image_status != 'deleted'",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "INSERT INTO favorites (occurrence_id, frame_id, created_at_ms) VALUES (?, ?, ?) "
            "ON CONFLICT (occurrence_id) DO NOTHING",
            (occurrence_id, row[0], now_ms),
        )
    return True


def remove_favorite(conn: sqlite3.Connection, occurrence_id: str) -> bool:
    """取消收藏：不立即删除任何内容，之后按普通到期与配额规则处理。"""
    with db.write_txn(conn):
        return conn.execute("DELETE FROM favorites WHERE occurrence_id = ?", (occurrence_id,)).rowcount > 0


def favorite_ids(conn: sqlite3.Connection, occurrence_ids: Iterable[str]) -> set[str]:
    ids = list(occurrence_ids)
    if not ids:
        return set()
    return {
        r[0]
        for r in conn.execute(
            "SELECT occurrence_id FROM favorites WHERE occurrence_id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        )
    }


def list_favorites(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT fv.occurrence_id, fv.frame_id, fv.created_at_ms, o.first_seen_ms, o.last_seen_ms, o.app, "
        "o.window_title, o.source_type, f.image_path, f.image_status "
        "FROM favorites fv JOIN occurrences o ON o.occurrence_id = fv.occurrence_id "
        "JOIN frames f ON f.frame_id = fv.frame_id ORDER BY fv.created_at_ms DESC"
    ).fetchall()


# ---------------- 用户删除 ----------------

@dataclass
class DeletePlan:
    """"删除此画面的全部记录"前展示给用户的影响范围。"""

    frame_id: str
    occurrences: list[sqlite3.Row]
    favorites: frozenset[str]          # 将一并删除的收藏（以出现记录 ID 标识）
    image_status: str


@dataclass
class DeleteResult:
    occurrences_removed: int = 0
    favorites_removed: int = 0
    frames_purged: int = 0
    blocks_freed: int = 0
    files_removed: int = 0
    image_paths: list[str] = field(default_factory=list)  # 提交后待删的图片


def plan_frame_delete(conn: sqlite3.Connection, frame_id: str) -> DeletePlan | None:
    f = conn.execute(
        "SELECT image_status FROM frames WHERE frame_id = ? AND image_status != 'deleted'", (frame_id,)
    ).fetchone()
    if f is None:
        return None
    occs = conn.execute(
        "SELECT occurrence_id, first_seen_ms, last_seen_ms, app, window_title, source_type FROM occurrences "
        "WHERE frame_id = ? ORDER BY first_seen_ms, occurrence_id",
        (frame_id,),
    ).fetchall()
    favs = frozenset(r[0] for r in conn.execute("SELECT occurrence_id FROM favorites WHERE frame_id = ?", (frame_id,)))
    return DeletePlan(frame_id, occs, favs, f[0])


def delete_occurrence(conn: sqlite3.Connection, data_dir: Path, occurrence_id: str) -> DeleteResult:
    """删除本次记录（用户明确操作）。其他出现仍引用该画面时只删时间与来源，不动图片、文字和向量。"""
    res = DeleteResult()
    with db.write_txn(conn):
        row = conn.execute("SELECT frame_id FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
        if row is None:
            return res
        res.favorites_removed += conn.execute(
            "DELETE FROM favorites WHERE occurrence_id = ?", (occurrence_id,)
        ).rowcount
        res.occurrences_removed += conn.execute(
            "DELETE FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)
        ).rowcount
        _purge_if_unreferenced(conn, [row[0]], res)
        db.bump_revision(conn)
    _remove_files(data_dir, res)
    return res


def delete_frame(
    conn: sqlite3.Connection, data_dir: Path, frame_id: str, *, confirmed_favorites: frozenset[str] | set[str]
) -> DeleteResult:
    """删除此画面的全部记录（用户已看过 plan_frame_delete 并确认）。

    confirmed_favorites 是确认时展示的收藏；此刻若多出未展示的收藏则抛 PlanChanged，不执行。
    确认后新增的普通出现记录属于同一画面，一并删除。
    """
    res = DeleteResult()
    with db.write_txn(conn):
        alive = conn.execute(
            "SELECT 1 FROM frames WHERE frame_id = ? AND image_status != 'deleted'", (frame_id,)
        ).fetchone()
        if alive is None:
            return res
        favs = {r[0] for r in conn.execute("SELECT occurrence_id FROM favorites WHERE frame_id = ?", (frame_id,))}
        if not favs <= set(confirmed_favorites):
            raise PlanChanged("确认后这幅画面又有新的收藏，请重新确认删除范围")
        res.favorites_removed += conn.execute("DELETE FROM favorites WHERE frame_id = ?", (frame_id,)).rowcount
        res.occurrences_removed += conn.execute("DELETE FROM occurrences WHERE frame_id = ?", (frame_id,)).rowcount
        _purge_if_unreferenced(conn, [frame_id], res)
        db.bump_revision(conn)
    _remove_files(data_dir, res)
    return res


@dataclass
class RangePlan:
    """按时间范围删除前展示的影响范围。与搜索的日期筛选一致：出现记录的 [首次, 最近] 与范围有交集即算在内。"""

    start_ms: int
    end_ms: int
    apps: tuple[str, ...]
    occurrences: int
    frames_purged: int          # 删除后不再被任何记录引用、将连同图片文字向量一起删除的画面
    frames_kept: int            # 仍被范围外（或保留的收藏）记录引用、只删范围内时间与来源的画面
    favorites: frozenset[str]   # 范围内被收藏的出现记录


def _range_sql(apps: tuple[str, ...]) -> tuple[str, list]:
    sql = "o.last_seen_ms >= ? AND o.first_seen_ms < ?"
    extra: list = []
    if apps:
        sql += " AND o.app IN (SELECT value FROM json_each(?))"
        extra.append(json.dumps(list(apps)))
    return sql, extra


def plan_range_delete(conn: sqlite3.Connection, start_ms: int, end_ms: int, apps: Iterable[str] = (),
                      *, include_favorites: bool = False) -> RangePlan:
    apps = tuple(apps)
    cond, extra = _range_sql(apps)
    rows = conn.execute(
        "SELECT o.occurrence_id, o.frame_id, EXISTS (SELECT 1 FROM favorites fv WHERE fv.occurrence_id = o.occurrence_id) "
        f"AS fav FROM occurrences o WHERE {cond}", [start_ms, end_ms, *extra],
    ).fetchall()
    favs = frozenset(r[0] for r in rows if r[2])
    doomed = {r[0] for r in rows if include_favorites or not r[2]}
    frames = {r[1] for r in rows if r[0] in doomed}
    purged = sum(
        all(o[0] in doomed for o in conn.execute("SELECT occurrence_id FROM occurrences WHERE frame_id = ?", (fid,)))
        for fid in frames
    )
    return RangePlan(start_ms, end_ms, apps, len(doomed), purged, len(frames) - purged, favs)


def delete_range(
    conn: sqlite3.Connection, data_dir: Path, start_ms: int, end_ms: int, apps: Iterable[str] = (), *,
    include_favorites: bool = False, confirmed_favorites: frozenset[str] | set[str] = frozenset(), batch: int = 500,
) -> DeleteResult:
    """按时间范围删除（用户明确操作，先看过 plan_range_delete）。

    默认保留范围内的收藏记录；只有用户勾选 include_favorites 时才删，且范围内的收藏必须都在
    confirmed_favorites 里，否则抛 PlanChanged。画面只在没有任何剩余记录引用时回收。
    """
    apps = tuple(apps)
    cond, extra = _range_sql(apps)
    params = [start_ms, end_ms, *extra]
    if include_favorites:
        now_favs = {r[0] for r in conn.execute(
            "SELECT fv.occurrence_id FROM favorites fv JOIN occurrences o ON o.occurrence_id = fv.occurrence_id "
            f"WHERE {cond}", params)}
        if not now_favs <= set(confirmed_favorites):
            raise PlanChanged("确认后范围内又有新的收藏，请重新确认删除范围")
    fav_filter = "" if include_favorites else (
        " AND NOT EXISTS (SELECT 1 FROM favorites fv WHERE fv.occurrence_id = o.occurrence_id)")
    total = DeleteResult()
    while True:
        res = DeleteResult()
        with db.write_txn(conn):
            rows = conn.execute(
                f"SELECT o.occurrence_id, o.frame_id FROM occurrences o WHERE {cond}{fav_filter} LIMIT ?",
                [*params, batch],
            ).fetchall()
            if not rows:
                break
            ids = [(r[0],) for r in rows]
            res.favorites_removed = sum(
                conn.execute("DELETE FROM favorites WHERE occurrence_id = ?", i).rowcount for i in ids)
            conn.executemany("DELETE FROM occurrences WHERE occurrence_id = ?", ids)
            res.occurrences_removed = len(ids)
            _purge_if_unreferenced(conn, [r[1] for r in rows], res)
            db.bump_revision(conn)
        _remove_files(data_dir, res)
        for k in ("occurrences_removed", "favorites_removed", "frames_purged", "blocks_freed", "files_removed"):
            setattr(total, k, getattr(total, k) + getattr(res, k))
    return total


# ---------------- 内部：回收 ----------------

def _purge_if_unreferenced(conn: sqlite3.Connection, frame_ids: Iterable[str], res: DeleteResult) -> None:
    """在调用方事务内：没有任何出现记录（因而也没有收藏）引用的画面做逻辑删除并回收派生数据。"""
    freed_blocks: set[int] = set()
    for fid in set(frame_ids):
        if conn.execute("SELECT 1 FROM occurrences WHERE frame_id = ? LIMIT 1", (fid,)).fetchone():
            continue
        f = conn.execute(
            "SELECT image_path, image_status FROM frames WHERE frame_id = ? AND image_status != 'deleted'", (fid,)
        ).fetchone()
        if f is None:
            continue
        # 1) 逻辑失效：版本号 +1 让在途任务写回失败；取消待办
        conn.execute(
            "UPDATE frames SET image_status = 'deleted', record_version = record_version + 1, "
            "text_embed_status = 'skipped', image_embed_status = 'skipped' WHERE frame_id = ?",
            (fid,),
        )
        conn.execute(
            "UPDATE index_jobs SET status = 'cancelled', worker_token = NULL, updated_at_ms = ?, "
            "last_error = '记录已删除' WHERE frame_id = ? AND status IN ('pending', 'running', 'failed')",
            (utc_ms(), fid),
        )
        # 2) 文字、FTS、坐标
        t = conn.execute("SELECT text_id FROM frame_text WHERE frame_id = ?", (fid,)).fetchone()
        if t is not None:
            conn.execute("DELETE FROM ocr_fts WHERE rowid = ?", (t[0],))
            conn.execute("DELETE FROM frame_text WHERE text_id = ?", (t[0],))
        conn.execute("DELETE FROM ocr_lines WHERE frame_id = ?", (fid,))
        # 3) 文本块关联与整帧向量；共享块只在最后一个引用消失后回收
        freed_blocks.update(
            int(r[0]) for r in conn.execute("SELECT block_id FROM frame_blocks WHERE frame_id = ?", (fid,))
        )
        conn.execute("DELETE FROM frame_blocks WHERE frame_id = ?", (fid,))
        conn.execute("DELETE FROM embeddings WHERE target_kind = 'frame' AND target_id = ?", (fid,))
        conn.execute("DELETE FROM frame_labels WHERE frame_id = ?", (fid,))
        res.frames_purged += 1
        if f["image_status"] in ("ok", "missing"):
            res.image_paths.append(f["image_path"])
    if freed_blocks:
        res.blocks_freed += vectors.gc_blocks(conn, freed_blocks)


def _remove_files(data_dir: Path, res: DeleteResult) -> None:
    """事务提交后再删文件；删不掉的留给 sweep_dead_files。"""
    for rel in res.image_paths:
        p = storage.resolve(data_dir, rel)
        try:
            if p.exists():
                p.unlink()
                res.files_removed += 1
        except OSError:
            pass
    res.image_paths = []


def sweep_dead_files(conn: sqlite3.Connection, data_dir: Path) -> int:
    """删除已逻辑删除或已过期画面仍残留的图片文件（例如上次删文件时进程退出）。"""
    n = 0
    for r in conn.execute("SELECT image_path FROM frames WHERE image_status IN ('deleted', 'expired')").fetchall():
        p = storage.resolve(data_dir, r[0])
        try:
            if p.exists():
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def _expire_images(conn: sqlite3.Connection, frame_ids: list[str]) -> list[str]:
    """在调用方事务内把原图标为过期（文字与特征保留），返回待删文件。"""
    paths = []
    for fid in frame_ids:
        r = conn.execute(
            "UPDATE frames SET image_status = 'expired', "
            "image_embed_status = CASE WHEN image_embed_status = 'pending' THEN 'skipped' ELSE image_embed_status END "
            "WHERE frame_id = ? AND image_status = 'ok' RETURNING image_path",
            (fid,),
        ).fetchone()
        if r is not None:
            paths.append(r[0])
            conn.execute(
                "UPDATE index_jobs SET status = 'cancelled', worker_token = NULL, updated_at_ms = ?, "
                "last_error = '原图已过期' WHERE frame_id = ? AND stage = 'image_embed' "
                "AND status IN ('pending', 'running', 'failed')",
                (utc_ms(), fid),
            )
    return paths


# ---------------- 到期 ----------------

@dataclass
class ExpireResult:
    images_expired: int = 0
    occurrences_removed: int = 0
    frames_purged: int = 0
    blocks_freed: int = 0
    files_removed: int = 0


def expire(conn: sqlite3.Connection, data_dir: Path, cfg: RetentionConfig, now_ms: int | None = None) -> ExpireResult:
    """普通到期：90 天前的非收藏出现记录删除；不再被近 30 天记录或收藏引用的原图过期。"""
    now_ms = utc_ms() if now_ms is None else now_ms
    out = ExpireResult()
    record_cut = now_ms - cfg.record_days * DAY_MS
    image_cut = now_ms - cfg.image_days * DAY_MS

    # 1) 出现记录（文字与特征随画面一起，在最后一个记录消失后回收）
    while True:
        with db.write_txn(conn):
            rows = conn.execute(
                "SELECT o.occurrence_id, o.frame_id FROM occurrences o "
                "WHERE o.last_seen_ms < ? "
                "AND NOT EXISTS (SELECT 1 FROM favorites fv WHERE fv.occurrence_id = o.occurrence_id) "
                "AND NOT " + _OCR_UNFINISHED.format(f="o") + " LIMIT ?",
                (record_cut, cfg.batch),
            ).fetchall()
            if not rows:
                break
            res = DeleteResult()
            conn.executemany("DELETE FROM occurrences WHERE occurrence_id = ?", [(r[0],) for r in rows])
            res.occurrences_removed = len(rows)
            _purge_if_unreferenced(conn, [r[1] for r in rows], res)
            db.bump_revision(conn)
        _remove_files(data_dir, res)
        out.occurrences_removed += res.occurrences_removed
        out.frames_purged += res.frames_purged
        out.blocks_freed += res.blocks_freed
        out.files_removed += res.files_removed

    # 2) 原图
    while True:
        with db.write_txn(conn):
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT f.frame_id FROM frames f WHERE f.image_status = 'ok' "
                    "AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.frame_id = f.frame_id AND o.last_seen_ms >= ?) "
                    "AND NOT " + _FRAME_FAVORITED.format(f="f") + " AND NOT " + _OCR_UNFINISHED.format(f="f")
                    + " LIMIT ?",
                    (image_cut, cfg.batch),
                )
            ]
            if not ids:
                break
            paths = _expire_images(conn, ids)
            db.bump_revision(conn)
        res = DeleteResult(image_paths=paths)
        _remove_files(data_dir, res)
        out.images_expired += len(paths)
        out.files_removed += res.files_removed
    return out


# ---------------- 配额 ----------------

def measure_usage(data_dir: Path) -> int:
    """运行数据的实际文件占用：图片、数据库（含 -wal / -shm）、任务数据、日志与缓存。模型权重不在此目录。"""
    total = 0
    for p in data_dir.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _free_bytes(data_dir: Path) -> int:
    return shutil.disk_usage(data_dir).free


@dataclass
class QuotaResult:
    usage_bytes: int
    free_bytes: int
    needed_bytes: int               # 仍需释放的字节；0 表示满足配额与最低剩余空间
    steps: list[str] = field(default_factory=list)
    images_expired_early: int = 0
    occurrences_removed_early: int = 0
    paused: bool = False
    reason: str | None = None


def _needed(cfg: RetentionConfig, usage: int, free: int) -> int:
    return max(0, usage + cfg.reserve_bytes - cfg.quota_bytes, cfg.min_free_bytes + cfg.reserve_bytes - free)


def enforce_quota(
    conn: sqlite3.Connection,
    data_dir: Path,
    cfg: RetentionConfig,
    now_ms: int | None = None,
    *,
    free_fn: Callable[[Path], int] = _free_bytes,
    usage_fn: Callable[[Path], int] = measure_usage,
) -> QuotaResult:
    """配额优先于保留天数。依次：过期数据 → 可重建缓存 → 最旧非收藏原图 → 最旧非收藏记录；仍不足则要求暂停采集。"""
    now_ms = utc_ms() if now_ms is None else now_ms

    def measure() -> tuple[int, int, int]:
        u, f = usage_fn(data_dir), free_fn(data_dir)
        return u, f, _needed(cfg, u, f)

    usage, free, need = measure()
    out = QuotaResult(usage, free, need)
    if need == 0:
        return out

    expire(conn, data_dir, cfg, now_ms)
    out.steps.append("清理到期数据")
    usage, free, need = measure()

    if need > 0:
        cache = data_dir / "cache"
        if cache.exists():
            for child in cache.iterdir():
                if child.name == "orphans":  # 启动核查移入的未登记图片不是可重建缓存
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        out.steps.append("清理可重建缓存")
        usage, free, need = measure()

    if need > 0:
        out.steps.append("提前清理最旧的非收藏原图")
        while need > 0:
            with db.write_txn(conn):
                rows = conn.execute(
                    "SELECT f.frame_id, f.image_bytes FROM frames f WHERE f.image_status = 'ok' "
                    "AND NOT " + _FRAME_FAVORITED.format(f="f") + " AND NOT " + _OCR_UNFINISHED.format(f="f")
                    + " ORDER BY (SELECT MAX(o.last_seen_ms) FROM occurrences o WHERE o.frame_id = f.frame_id), "
                    "f.captured_at_ms LIMIT ?",
                    (cfg.batch,),
                ).fetchall()
                if not rows:
                    break
                picked, acc = [], 0
                for r in rows:  # 按估计字节只取够用的部分，避免多删
                    picked.append(r[0])
                    acc += int(r[1])
                    if acc >= need:
                        break
                paths = _expire_images(conn, picked)
                db.bump_revision(conn)
            res = DeleteResult(image_paths=paths)
            _remove_files(data_dir, res)
            out.images_expired_early += len(paths)
            usage, free, need = measure()

    if need > 0:
        out.steps.append("提前清理最旧的非收藏记录")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        while need > 0:
            before = usage
            with db.write_txn(conn):
                rows = conn.execute(
                    "SELECT o.occurrence_id, o.frame_id FROM occurrences o "
                    "WHERE NOT EXISTS (SELECT 1 FROM favorites fv WHERE fv.occurrence_id = o.occurrence_id) "
                    "AND NOT " + _OCR_UNFINISHED.format(f="o") + " ORDER BY o.last_seen_ms, o.occurrence_id LIMIT ?",
                    (cfg.batch,),
                ).fetchall()
                if not rows:
                    break
                res = DeleteResult()
                conn.executemany("DELETE FROM occurrences WHERE occurrence_id = ?", [(r[0],) for r in rows])
                res.occurrences_removed = len(rows)
                _purge_if_unreferenced(conn, [r[1] for r in rows], res)
                db.bump_revision(conn)
            _remove_files(data_dir, res)
            out.occurrences_removed_early += res.occurrences_removed
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            usage, free, need = measure()
            if usage >= before:
                # 删除记录不一定缩小数据库文件（以实际占用为准）：没有效果就停，不继续删光历史
                break

    out.usage_bytes, out.free_bytes, out.needed_bytes = usage, free, need
    if need > 0:
        out.paused = True
        protected = conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]
        out.reason = (
            f"存储空间不足，已暂停采集：运行数据 {usage / GIB:.2f} GiB / 配额 {cfg.quota_bytes / GIB:.2f} GiB，"
            f"磁盘剩余 {free / GIB:.2f} GiB（最低 {cfg.min_free_bytes / GIB:.2f} GiB）。"
            + (f"剩余数据含 {protected} 条收藏，不会自动清理；" if protected else "")
            + "请取消部分收藏、手动删除记录或调整配额后重新开启录制。"
        )
    return out


# ---------------- 维护（后台定时调用）与状态 ----------------

MAINT_KEY = "maintenance"


def run_maintenance(
    conn: sqlite3.Connection, data_dir: Path, cfg: RetentionConfig, now_ms: int | None = None, **quota_kw
) -> dict:
    now_ms = utc_ms() if now_ms is None else now_ms
    exp = expire(conn, data_dir, cfg, now_ms)
    q = enforce_quota(conn, data_dir, cfg, now_ms, **quota_kw)
    report = {
        "at_ms": now_ms,
        "images_expired": exp.images_expired,
        "occurrences_removed": exp.occurrences_removed,
        "frames_purged": exp.frames_purged,
        "usage_bytes": q.usage_bytes,
        "free_bytes": q.free_bytes,
        "quota_bytes": cfg.quota_bytes,
        "needed_bytes": q.needed_bytes,
        "quota_steps": q.steps,
        "images_expired_early": q.images_expired_early,
        "occurrences_removed_early": q.occurrences_removed_early,
        "paused": q.paused,
        "reason": q.reason,
    }
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (MAINT_KEY, json.dumps(report, ensure_ascii=False)),
        )
    return report


def last_maintenance(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (MAINT_KEY,)).fetchone()
    return json.loads(row[0]) if row else None
