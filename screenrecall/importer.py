"""批量导入静态截图（方案 6.8）。

规则：
- 只接收 PNG、JPEG、WebP 静态图片；按实际解码出的格式判断，不只看扩展名；动图、过大或过小的图片拒收；
- 逐文件验证可解码、尺寸合理、磁盘余量（写入后至少剩 MIN_FREE_BYTES），通过后进入与实时截图相同的
  落盘（WebP 原子写入）、OCR 待办与后续向量流水线；
- 每个批次一条 import 录制段（sessions.source_type = 'import'），出现记录 source_type = 'import'，窗口标题记文件名；
- 捕获时间只用用户明确提供的值；没有时记导入时间并标"时间未知"（capture_meta.time_known = false），
  不用文件修改时间冒充捕获时间；
- 按传入顺序处理，逐项返回成功 / 失败 / 跳过及原因；损坏图片不阻塞后续文件；
- 幂等：批次指纹 = 按顺序的（文件名，内容指纹）。重交同一批次时已成功的文件跳过，失败的文件重试，
  因此失败后可以安全重试。内容指纹只用于导入幂等，不替代实时截图的原始像素精确复用；
- 导入批次内的先后顺序不是连续屏幕历史（证据浏览会这样说明）。

命令行：python -m screenrecall.importer <图片或目录> [...] [--label 名称] [--captured "2026-10-15 14:30"]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image, UnidentifiedImageError

from . import db, storage
from .config import Config, load_config
from .timeutil import fmt_local, local_tz, utc_ms

SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
FORMATS = {"PNG", "JPEG", "WEBP"}
MIN_SIDE = 16
MAX_SIDE = 16384
MAX_PIXELS = 64_000_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024**3  # 方案 6.4：磁盘至少剩余 2 GiB

OK, FAILED, SKIPPED = "ok", "failed", "skipped"


@dataclass(frozen=True)
class ImportFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class FileMeta:
    """逐文件的来源信息（数据集清单提供）。None 表示未知，不用文件名或修改时间猜。"""
    captured_at_ms: int | None = None
    app: str | None = None
    window_title: str | None = None


@dataclass(frozen=True)
class ItemResult:
    seq: int
    name: str
    status: str
    reason: str | None = None
    frame_id: str | None = None


@dataclass
class BatchResult:
    batch_id: str
    session_id: str
    time_known: bool
    resubmitted: bool  # 同一批次之前提交过
    items: list[ItemResult] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for i in self.items if i.status == status)


def content_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def batch_fingerprint(files: list[ImportFile]) -> str:
    h = hashlib.blake2b(digest_size=16)
    for f in files:
        h.update(f.name.encode("utf-8") + b"\0" + content_hash(f.data).encode() + b"\n")
    return h.hexdigest()


def _decode(f: ImportFile) -> Image.Image:
    """返回 RGB 图像；不合格时抛出 ValueError（原因可直接展示）。"""
    if Path(f.name).suffix.lower() not in SUFFIXES:
        raise ValueError("不支持的文件类型，只接收 PNG、JPEG、WebP")
    if not f.data:
        raise ValueError("文件为空")
    if len(f.data) > MAX_FILE_BYTES:
        raise ValueError(f"文件超过 {MAX_FILE_BYTES // 1024 // 1024} MiB")
    try:
        with Image.open(io.BytesIO(f.data)) as im:
            if im.format not in FORMATS:
                raise ValueError(f"实际格式为 {im.format}，只接收 PNG、JPEG、WebP")
            if getattr(im, "is_animated", False):
                raise ValueError("动图不支持，只接收静态图片")
            w, h = im.size
            if min(w, h) < MIN_SIDE or max(w, h) > MAX_SIDE or w * h > MAX_PIXELS:
                raise ValueError(f"尺寸 {w}×{h} 不在允许范围（边长 {MIN_SIDE}–{MAX_SIDE}，最多 {MAX_PIXELS:,} 像素）")
            im.load()
            return im.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError(f"无法解码：{type(exc).__name__}") from exc


def _bgr(img: Image.Image) -> bytes:
    r, g, b = img.split()
    return Image.merge("RGB", (b, g, r)).tobytes()


def _find_or_create_batch(conn, fp: str, label: str | None, time_known: bool, now: int):
    row = conn.execute("SELECT batch_id, session_id FROM import_batches WHERE fingerprint = ?", (fp,)).fetchone()
    if row:
        prev = {
            r["seq"]: r for r in conn.execute("SELECT * FROM import_items WHERE batch_id = ?", (row["batch_id"],))
        }
        return row["batch_id"], row["session_id"], True, prev
    batch_id, session_id = db.new_id(), db.new_id()
    offset, name = local_tz()
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO sessions (session_id, started_at_ms, ended_at_ms, end_reason, source_type, target_title, "
            "tz_offset_min, tz_name) VALUES (?, ?, ?, 'import', 'import', ?, ?, ?)",
            (session_id, now, now, label or "批量导入", offset, name),
        )
        conn.execute(
            "INSERT INTO import_batches (batch_id, fingerprint, label, session_id, time_known, created_at_ms, "
            "updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (batch_id, fp, label, session_id, int(time_known), now, now),
        )
    return batch_id, session_id, False, {}


def _record(conn, batch_id: str, seq: int, f: ImportFile, h: str, status: str, reason: str | None,
            frame_id: str | None, now: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO import_items (batch_id, seq, file_name, content_hash, status, reason, frame_id, "
        "updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (batch_id, seq, f.name, h, status, reason, frame_id, now),
    )


def import_batch(
    conn: sqlite3.Connection,
    cfg: Config,
    files: list[ImportFile],
    *,
    label: str | None = None,
    captured_at_ms: int | None = None,
    metas: list[FileMeta | None] | None = None,
    free_bytes: Callable[[Path], int] | None = None,
) -> BatchResult:
    """导入一批图片。captured_at_ms 是用户提供的可信捕获时间（整批共用）；None 表示时间未知。

    metas 与 files 一一对应，给出逐文件的捕获时间、应用和窗口标题；某项的 captured_at_ms 为 None 时
    退回整批的 captured_at_ms，两者都没有就记"时间未知"。整批的 time_known 只有每个文件都已知时才为真。
    """
    if metas is not None and len(metas) != len(files):
        raise ValueError("metas 必须与 files 一一对应")
    metas = metas or [None] * len(files)
    db.init_schema(conn)  # 页面可能先于新版后台打开数据库
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    now = utc_ms()
    file_times = [(m.captured_at_ms if m and m.captured_at_ms is not None else captured_at_ms) for m in metas]
    time_known = all(t is not None for t in file_times) if files else captured_at_ms is not None
    batch_id, session_id, resubmitted, prev = _find_or_create_batch(
        conn, batch_fingerprint(files), label, time_known, now
    )
    result = BatchResult(batch_id, session_id, time_known, resubmitted)
    seen: dict[str, int] = {}
    capcfg = cfg.capture
    fmt = "webp-lossless" if capcfg.webp_lossless else f"webp-q{capcfg.webp_quality}"

    for seq, f in enumerate(files):
        h = content_hash(f.data)
        old = prev.get(seq)
        if old is not None and old["status"] == OK:
            result.items.append(ItemResult(seq, f.name, SKIPPED, "本批次之前已导入", old["frame_id"]))
            seen.setdefault(h, seq)
            continue
        if h in seen:
            reason = f"与本批次第 {seen[h] + 1} 个文件内容相同"
            with db.write_txn(conn):
                _record(conn, batch_id, seq, f, h, SKIPPED, reason, None, utc_ms())
            result.items.append(ItemResult(seq, f.name, SKIPPED, reason))
            continue
        seen[h] = seq
        try:
            img = _decode(f)
            bgr = _bgr(img)
            payload = storage.encode_webp(
                bgr, img.width, img.height,
                quality=capcfg.webp_quality, lossless=capcfg.webp_lossless, method=capcfg.webp_method,
            )
            if free_bytes(cfg.data_dir) - len(payload) < MIN_FREE_BYTES:
                raise ValueError("磁盘剩余空间不足 2 GiB，未写入")
        except ValueError as exc:
            with db.write_txn(conn):
                _record(conn, batch_id, seq, f, h, FAILED, str(exc), None, utc_ms())
            result.items.append(ItemResult(seq, f.name, FAILED, str(exc)))
            continue

        frame_id, occ_id = db.new_id(), db.new_id()
        m = metas[seq]
        f_time = file_times[seq]
        f_known = f_time is not None
        observed = f_time if f_known else utc_ms()
        rel = storage.frame_rel_path(frame_id, observed)
        meta = json.dumps(
            {"method": "import", "trigger": "import", "batch_id": batch_id, "file": f.name,
             "source_format": Path(f.name).suffix.lower().lstrip("."), "time_known": f_known, "dpi": None},
            ensure_ascii=False,
        )
        nbytes = storage.write_atomic(cfg.data_dir, rel, payload, (img.width, img.height))
        try:
            with db.write_txn(conn):
                t = utc_ms()
                conn.execute(
                    "INSERT INTO frames (frame_id, image_path, width, height, captured_at_ms, accepted_at_ms, "
                    "pixel_hash, image_format, image_bytes, capture_meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (frame_id, rel, img.width, img.height, observed, t,
                     hashlib.blake2b(bgr, digest_size=16).hexdigest(), fmt, nbytes, meta),
                )
                conn.execute(
                    "INSERT INTO occurrences (occurrence_id, frame_id, session_id, first_seen_ms, last_seen_ms, "
                    "app, window_title, source_type) VALUES (?, ?, ?, ?, ?, ?, ?, 'import')",
                    (occ_id, frame_id, session_id, observed, observed, (m.app if m else None) or None,
                     (m.window_title if m else None) or f.name),
                )
                conn.execute(
                    "INSERT INTO index_jobs (frame_id, stage, status, target_version, created_at_ms, updated_at_ms) "
                    "VALUES (?, 'ocr', 'pending', 1, ?, ?)",
                    (frame_id, t, t),
                )
                _record(conn, batch_id, seq, f, h, OK, None, frame_id, t)
                conn.execute("UPDATE import_batches SET updated_at_ms = ? WHERE batch_id = ?", (t, batch_id))
        except BaseException:
            storage.discard(cfg.data_dir, rel)  # 未登记的文件不算接受
            raise
        result.items.append(ItemResult(seq, f.name, OK, None, frame_id))
    return result


def is_time_known(capture_meta: dict) -> bool:
    """实时截图没有这个键，视为时间可信；导入时只有用户提供了时间才可信。"""
    return capture_meta.get("time_known", True) is not False


def time_unknown_frames(conn: sqlite3.Connection, frame_ids: list[str]) -> set[str]:
    """其中捕获时间未知（导入且用户没给时间）的帧。"""
    if not frame_ids:
        return set()
    return {
        r[0] for r in conn.execute(
            "SELECT frame_id FROM frames WHERE frame_id IN (SELECT value FROM json_each(?)) "
            "AND json_extract(capture_meta, '$.time_known') = 0",
            (json.dumps(frame_ids),),
        )
    }


def _collect(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(c for c in p.iterdir() if c.is_file() and c.suffix.lower() in SUFFIXES))
        else:
            out.append(p)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="批量导入 PNG / JPEG / WebP 截图")
    ap.add_argument("paths", nargs="+", help="图片文件或目录（目录内按文件名排序）")
    ap.add_argument("--label", help="批次名称")
    ap.add_argument("--captured", help='可信捕获时间（本地时间），如 "2026-10-15 14:30"；不给则标"时间未知"')
    args = ap.parse_args(argv)
    captured = int(datetime.fromisoformat(args.captured).timestamp() * 1000) if args.captured else None
    cfg = load_config()
    files = [ImportFile(p.name, p.read_bytes()) for p in _collect(args.paths)]
    conn = db.connect(cfg.db_path)
    try:
        res = import_batch(conn, cfg, files, label=args.label, captured_at_ms=captured)
    finally:
        conn.close()
    print(f"批次 {res.batch_id}{'（重新提交）' if res.resubmitted else ''}："
          f"成功 {res.count(OK)}、失败 {res.count(FAILED)}、跳过 {res.count(SKIPPED)}；"
          f"捕获时间 {fmt_local(captured) if captured else '未知（记导入时间）'}")
    for i in res.items:
        print(f"  {i.seq + 1:>3}. {i.name}: {i.status}{' — ' + i.reason if i.reason else ''}")
    print("OCR 与向量由后台补算，请保持 python -m screenrecall.daemon 运行。")
    return 0 if res.count(FAILED) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
