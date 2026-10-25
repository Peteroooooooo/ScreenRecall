"""把一个数据目录（源库）里的帧整体并入另一个数据目录（目标库），保留 frame_id。

用途：肖天奕在 Windows 上用后台实时录制的帧（data/）并入曹贺乔截图导入的数据集库（data-dataset/），
合成一个库做检索实验和分类。保留 frame_id，所以两边已有的标注表和查询真值不用改。

只做"搬运"，不重算：OCR 文字、OCR 行、FTS、文本块、向量都原样复制。为保证能直接复制，先检查两库
schema 版本、OCR 版本、分块/规范化版本、向量模型与预处理版本完全一致，且 frame_id、图片路径、像素指纹、
录制段、出现记录没有任何重叠；任何一项不满足就拒绝合并，不改目标库。

    python datasets/merge_library.py --src data --dst data-dataset            # 只检查，不写入
    python datasets/merge_library.py --src data --dst data-dataset --apply    # 写入（先自行备份目标库）

写入在一个事务里完成；图片文件先复制（已存在且字节相同则跳过），再提交数据库。目标库 meta 里追加一条
merge_log 记录来源、帧数和时间。运行前请停止两个数据目录上的后台和页面。
"""
from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screenrecall import db, storage  # noqa: E402

DB_NAME = "screenrecall.sqlite3"
# 这些版本串两库必须一致，复制过去的文字和向量才和目标库可比
VERSION_QUERIES = {
    "schema": "SELECT value FROM meta WHERE key = 'schema_version'",
    "ocr": "SELECT DISTINCT ocr_version FROM frame_text",
    "blocks": "SELECT DISTINCT chunker_version || '/' || norm_version FROM text_blocks",
    "embeddings": "SELECT DISTINCT target_kind || '|' || branch || '|' || model_id || '|' || preprocess_version "
                  "|| '|' || dim || '|' || dtype FROM embeddings",
}
OVERLAP_QUERIES = {
    "frame_id": "SELECT frame_id FROM frames",
    "image_path": "SELECT image_path FROM frames",
    "pixel_hash": "SELECT pixel_hash FROM frames",
    "session_id": "SELECT session_id FROM sessions",
    "occurrence_id": "SELECT occurrence_id FROM occurrences",
}


def _set(conn: sqlite3.Connection, q: str) -> set:
    return {r[0] for r in conn.execute(q)}


def check(src: sqlite3.Connection, dst: sqlite3.Connection) -> list[str]:
    problems = []
    for name, q in VERSION_QUERIES.items():
        a, b = _set(src, q), _set(dst, q)
        if name == "schema" and a != b:
            problems.append(f"schema 版本不同：源 {sorted(a)}，目标 {sorted(b)}")
        elif name != "schema" and b and not a <= b:
            problems.append(f"{name} 版本不一致：源多出 {sorted(a - b)}（目标 {sorted(b)}）")
    for name, q in OVERLAP_QUERIES.items():
        n = len(_set(src, q) & _set(dst, q))
        if n:
            problems.append(f"{name} 有 {n} 个重叠")
    bad = src.execute(
        "SELECT COUNT(*) FROM frames WHERE image_status = 'ok' AND "
        "(ocr_status != 'done' OR text_embed_status NOT IN ('done', 'skipped') "
        "OR image_embed_status NOT IN ('done', 'skipped'))").fetchone()[0]
    if bad:
        problems.append(f"源库有 {bad} 帧还没索引完（OCR / 向量），先跑完再合并")
    busy = src.execute("SELECT COUNT(*) FROM index_jobs WHERE status IN ('pending', 'running')").fetchone()[0]
    if busy:
        problems.append(f"源库还有 {busy} 个待办 / 进行中的索引任务")
    return problems


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _copy_rows(src, dst, table: str, where: str = "", drop: tuple[str, ...] = ()) -> int:
    cols = [c for c in _cols(src, table) if c not in drop]
    rows = src.execute(f'SELECT {", ".join(cols)} FROM "{table}" {where}').fetchall()
    dst.executemany(f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                    [tuple(r) for r in rows])
    return len(rows)


def merge(src_dir: Path, dst_dir: Path, apply: bool) -> dict:
    src = db.connect(src_dir / DB_NAME)
    dst = db.connect(dst_dir / DB_NAME)
    try:
        problems = check(src, dst)
        frames = src.execute("SELECT frame_id, image_path, image_status FROM frames").fetchall()
        missing = [f["image_path"] for f in frames
                   if f["image_status"] == "ok" and not storage.resolve(src_dir, f["image_path"]).exists()]
        if missing:
            problems.append(f"源库有 {len(missing)} 张原图文件缺失，例如 {missing[0]}")
        plan = {"src": str(src_dir), "dst": str(dst_dir), "frames": len(frames),
                "dst_frames_before": dst.execute("SELECT COUNT(*) FROM frames").fetchone()[0],
                "problems": problems}
        if problems or not apply:
            return plan

        # 1. 图片文件（先于数据库提交：失败时库不变，多出的文件不被引用）
        copied = skipped = 0
        for f in frames:
            if f["image_status"] != "ok":
                continue
            sp, dp = storage.resolve(src_dir, f["image_path"]), storage.resolve(dst_dir, f["image_path"])
            if dp.exists():
                if not filecmp.cmp(sp, dp, shallow=False):
                    raise RuntimeError(f"目标已有同名但内容不同的图片：{dp}")
                skipped += 1
                continue
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sp, dp)
            copied += 1

        # 2. 数据库（一个事务）
        n = {}
        with db.write_txn(dst):
            n["sessions"] = _copy_rows(src, dst, "sessions")
            n["gaps"] = _copy_rows(src, dst, "gaps", drop=("gap_id",))
            n["frames"] = _copy_rows(src, dst, "frames")
            n["occurrences"] = _copy_rows(src, dst, "occurrences")
            n["ocr_lines"] = _copy_rows(src, dst, "ocr_lines")
            n["favorites"] = _copy_rows(src, dst, "favorites")
            n["frame_labels"] = _copy_rows(src, dst, "frame_labels")
            n["index_jobs"] = _copy_rows(src, dst, "index_jobs", drop=("job_id",))
            # frame_text 的 text_id 是自增主键，FTS 的 rowid 跟着它，换新号
            t_cols = [c for c in _cols(src, "frame_text") if c != "text_id"]
            n["frame_text"] = 0
            for t in src.execute(f'SELECT {", ".join(t_cols)} FROM frame_text').fetchall():
                cur = dst.execute(f'INSERT INTO frame_text ({", ".join(t_cols)}) VALUES '
                                  f'({", ".join("?" * len(t_cols))})', tuple(t))
                dst.execute("INSERT INTO ocr_fts (rowid, text_fold) VALUES (?, ?)", (cur.lastrowid, t["text_fold"]))
                n["frame_text"] += 1
            # 文本块按 block_key 去重：目标已有同一块就复用它的 block_id 和向量
            b_cols = [c for c in _cols(src, "text_blocks") if c != "block_id"]
            remap, reused = {}, 0
            for b in src.execute(f'SELECT block_id, {", ".join(b_cols)} FROM text_blocks').fetchall():
                hit = dst.execute("SELECT block_id FROM text_blocks WHERE block_key = ?", (b["block_key"],)).fetchone()
                if hit:
                    remap[b["block_id"]] = hit[0]
                    reused += 1
                    continue
                cur = dst.execute(f'INSERT INTO text_blocks ({", ".join(b_cols)}) VALUES '
                                  f'({", ".join("?" * len(b_cols))})', tuple(b[c] for c in b_cols))
                remap[b["block_id"]] = cur.lastrowid
            n["text_blocks_new"], n["text_blocks_reused"] = len(remap) - reused, reused
            fb_cols = _cols(src, "frame_blocks")
            rows = []
            for r in src.execute(f'SELECT {", ".join(fb_cols)} FROM frame_blocks').fetchall():
                d = dict(zip(fb_cols, r))
                d["block_id"] = remap[d["block_id"]]
                rows.append(tuple(d[c] for c in fb_cols))
            dst.executemany(f'INSERT INTO frame_blocks ({", ".join(fb_cols)}) VALUES ({", ".join("?" * len(fb_cols))})',
                            rows)
            n["frame_blocks"] = len(rows)
            # 向量：帧向量 target_id = frame_id 原样；块向量 target_id 换成新 block_id，目标已有则跳过
            e_cols = [c for c in _cols(src, "embeddings") if c != "emb_id"]
            n["embeddings"] = 0
            for e in src.execute(f'SELECT {", ".join(e_cols)} FROM embeddings').fetchall():
                d = dict(zip(e_cols, e))
                if d["target_kind"] == "block":
                    d["target_id"] = str(remap[int(d["target_id"])])
                cur = dst.execute(f'INSERT OR IGNORE INTO embeddings ({", ".join(e_cols)}) VALUES '
                                  f'({", ".join("?" * len(e_cols))})', tuple(d[c] for c in e_cols))
                n["embeddings"] += cur.rowcount
            db.bump_revision(dst)
            log = json.loads((dst.execute("SELECT value FROM meta WHERE key = 'merge_log'").fetchone() or ["[]"])[0])
            log.append({"src": str(src_dir), "at_ms": int(time.time() * 1000), "rows": n,
                        "images_copied": copied, "images_already_there": skipped})
            dst.execute("INSERT INTO meta (key, value) VALUES ('merge_log', ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(log),))
        plan |= {"rows": n, "images_copied": copied, "images_already_there": skipped,
                 "dst_frames_after": dst.execute("SELECT COUNT(*) FROM frames").fetchone()[0]}
        return plan
    finally:
        src.close()
        dst.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把源数据目录的帧并入目标数据目录（保留 frame_id）")
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--dst", required=True, type=Path)
    ap.add_argument("--apply", action="store_true", help="真正写入；不加只做检查")
    a = ap.parse_args(argv)
    res = merge(a.src.resolve(), a.dst.resolve(), a.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 1 if res["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
