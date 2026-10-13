"""图片文件：编码、可靠落盘与启动核查（方案 3.5、4.3、6.5）。

接受顺序：同目录临时文件写入并 fsync → 解码校验 → 原子改名 → SQLite 短事务登记。
SQLite 无法与文件系统组成跨介质原子事务，因此启动时核对两边是否一致。
"""

from __future__ import annotations

import io
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from . import db

TMP_PREFIX = ".tmp-"


def frame_rel_path(frame_id: str, captured_at_ms: int) -> str:
    d = datetime.fromtimestamp(captured_at_ms / 1000, tz=timezone.utc)
    return f"frames/{d:%Y/%m/%d}/{frame_id}.webp"


def resolve(data_dir: Path, rel: str) -> Path:
    return data_dir.joinpath(*rel.split("/"))


def encode_webp(bgr: bytes, width: int, height: int, *, quality: int, lossless: bool, method: int) -> bytes:
    img = Image.frombytes("RGB", (width, height), bgr, "raw", "BGR")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, lossless=lossless, method=method)
    return buf.getvalue()


def write_atomic(data_dir: Path, rel: str, payload: bytes, expect_size: tuple[int, int]) -> int:
    """写入并校验图片，成功后原子改名为正式文件名。返回字节数。"""
    final = resolve(data_dir, rel)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(TMP_PREFIX + final.name)
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        with Image.open(tmp) as im:
            im.load()
            if im.size != expect_size:
                raise OSError(f"解码尺寸 {im.size} 与期望 {expect_size} 不符")
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(payload)


def discard(data_dir: Path, rel: str) -> None:
    """删除未被接受的图片（登记失败时调用）。删不掉留给启动核查。"""
    try:
        resolve(data_dir, rel).unlink(missing_ok=True)
    except OSError:
        pass


def reconcile(conn: sqlite3.Connection, data_dir: Path, log: logging.Logger) -> dict[str, int]:
    """启动核查（此时没有活动写入）：
    - 删除残留临时文件；
    - 未登记的正式图片移到 cache/orphans，不直接删除，避免路径比对出错时误删；
    - 已登记但文件缺失的记录标为 missing，未完成的 OCR 待办标为失败。
    """
    stats = {"tmp_removed": 0, "orphans_moved": 0, "missing_marked": 0}
    frames_root = data_dir / "frames"
    known = {row[0].lower() for row in conn.execute("SELECT image_path FROM frames")}
    if frames_root.exists():
        for p in [p for p in frames_root.rglob("*") if p.is_file()]:
            if p.name.startswith(TMP_PREFIX):
                p.unlink(missing_ok=True)
                stats["tmp_removed"] += 1
                continue
            if p.relative_to(data_dir).as_posix().lower() in known:
                continue
            dest = data_dir / "cache" / "orphans" / p.relative_to(frames_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, dest)
            stats["orphans_moved"] += 1
            log.warning("未登记的图片已移入 %s", dest)

    missing = [
        row["frame_id"]
        for row in conn.execute("SELECT frame_id, image_path FROM frames WHERE image_status = 'ok'").fetchall()
        if not resolve(data_dir, row["image_path"]).exists()
    ]
    if missing:
        params = [(fid,) for fid in missing]
        with db.write_txn(conn):
            conn.executemany("UPDATE frames SET image_status = 'missing' WHERE frame_id = ?", params)
            conn.executemany(
                "UPDATE frames SET ocr_status = 'failed' WHERE frame_id = ? AND ocr_status = 'pending'",
                params,
            )
            conn.executemany(
                "UPDATE index_jobs SET status = 'failed', worker_token = NULL, "
                "last_error = 'image_missing: 启动核查发现图片缺失' "
                "WHERE frame_id = ? AND status IN ('pending', 'running')",
                params,
            )
        stats["missing_marked"] = len(missing)
        log.warning("%d 条记录的图片文件缺失，已标记", len(missing))
    return stats
