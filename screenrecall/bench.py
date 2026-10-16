"""资源与速度测量工具（方案 7.3）：python -m screenrecall.bench <子命令>

  expand     把现有数据目录复制一份并用复制数据扩到指定帧数（只用于资源 / 速度测量，不能用于准确率或分类训练）
  query      在数据目录上测文字 / 画面查询的冷、热耗时，分别记录查询编码与读库 + 排序（P50 / P95）
  readiness  从数据库统计"抓拍 → 可词法搜到 / 语义就绪 / 全图就绪"的延迟，并列出未完成、失败、取消的数量
  mem        按进程树采样后台与页面的 CPU 与内存（含子进程：Windows venv 的 python.exe 只是启动器）

所有子命令输出一行 JSON（--out 可另存文件），数字只在指定测试机上才算测量结果；Mac 上只做冒烟。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from . import db, models, retrieval, search, storage, vectors

BENCH_META = "bench_expanded_from"


def pct(values: Sequence[float], q: float) -> float | None:
    """最近秩百分位（不插值）；空列表返回 None。"""
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(np.ceil(q / 100 * len(s))) - 1))
    return float(s[k])


def summary(values: Sequence[float]) -> dict:
    return {
        "n": len(values),
        "mean": float(statistics.fmean(values)) if values else None,
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "max": float(max(values)) if values else None,
    }


# ======================= expand =======================

def _noisy(blob: bytes, dim: int, rng: np.random.Generator, scale: float) -> bytes:
    v = vectors.from_blob(blob, dim).astype(np.float32)
    v = v + rng.normal(0, scale, size=v.shape).astype(np.float32)
    n = np.linalg.norm(v)
    return vectors.to_blob(v / n if n else v)


def expand(src: Path, dst: Path, target_frames: int, *, seed: int = 0, noise: float = 0.02,
           copy_files: bool = True) -> dict:
    """复制 src 数据目录到 dst，然后循环复制已完成索引的帧直到 target_frames。

    每个复制帧有自己的出现记录、OCR 文字与 FTS 行、文本块（新块键，向量加少量噪声）与全图向量，
    使矩阵规模与真实唯一帧数一致。dst 的 meta 标注来源，不能在其上运行后台录制。
    """
    if dst.exists() and any(dst.iterdir()):
        raise FileExistsError(f"{dst} 不是空目录")
    dst.mkdir(parents=True, exist_ok=True)
    s = db.connect(src / "screenrecall.sqlite3")
    d = db.connect(dst / "screenrecall.sqlite3")
    try:
        s.backup(d)
    finally:
        s.close()
    rng = np.random.default_rng(seed)
    try:
        d.execute("PRAGMA journal_mode = WAL")
        seeds = [r[0] for r in d.execute(
            "SELECT frame_id FROM frames WHERE image_status = 'ok' AND ocr_status = 'done' ORDER BY captured_at_ms"
        )]
        if not seeds:
            raise ValueError("源库没有 OCR 已完成且原图可用的帧，无法扩充")
        have = d.execute("SELECT COUNT(*) FROM frames WHERE image_status != 'deleted'").fetchone()[0]
        made = 0
        i = 0
        while have + made < target_frames:
            with db.write_txn(d):
                for _ in range(min(100, target_frames - have - made)):
                    _copy_frame(d, src, dst, seeds[i % len(seeds)], i, rng, noise, copy_files)
                    i += 1
                    made += 1
                db.bump_revision(d)
        with db.write_txn(d):
            d.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (BENCH_META, json.dumps({"src": str(src), "copies": made, "seed": seed})),
            )
        if copy_files:  # 源库原有帧的图片（复制帧的图片已在 _copy_frame 里复制）
            for r in d.execute("SELECT image_path FROM frames WHERE image_status = 'ok'").fetchall():
                p, sp = storage.resolve(dst, r[0]), storage.resolve(src, r[0])
                if not p.exists() and sp.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(sp, p)
        total = d.execute("SELECT COUNT(*) FROM frames WHERE image_status != 'deleted'").fetchone()[0]
        blocks = d.execute("SELECT COUNT(*) FROM text_blocks").fetchone()[0]
    finally:
        d.close()
    return {"frames": total, "copied": made, "text_blocks": blocks, "dst": str(dst)}


def _copy_frame(d: sqlite3.Connection, src: Path, dst: Path, fid: str, idx: int,
                rng: np.random.Generator, noise: float, copy_files: bool) -> None:
    f = d.execute("SELECT * FROM frames WHERE frame_id = ?", (fid,)).fetchone()
    new = db.new_id()
    shift = (idx + 1) * 60_000  # 每份复制往后错开 1 分钟，避免时间完全重合
    rel = storage.frame_rel_path(new, f["captured_at_ms"] + shift)
    if copy_files:
        sp, dp = storage.resolve(src, f["image_path"]), storage.resolve(dst, rel)
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sp, dp)
    cols = [k for k in f.keys() if k not in ("frame_id", "image_path", "captured_at_ms", "accepted_at_ms")]
    d.execute(
        f"INSERT INTO frames (frame_id, image_path, captured_at_ms, accepted_at_ms, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, ?, {', '.join('?' * len(cols))})",
        (new, rel, f["captured_at_ms"] + shift, f["accepted_at_ms"] + shift, *[f[k] for k in cols]),
    )
    for o in d.execute("SELECT * FROM occurrences WHERE frame_id = ?", (fid,)).fetchall():
        d.execute(
            "INSERT INTO occurrences (occurrence_id, frame_id, session_id, first_seen_ms, last_seen_ms, observations, "
            "app, window_title, hwnd, source_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (db.new_id(), new, o["session_id"], o["first_seen_ms"] + shift, o["last_seen_ms"] + shift,
             o["observations"], o["app"], o["window_title"], o["hwnd"], o["source_type"]),
        )
    t = d.execute("SELECT * FROM frame_text WHERE frame_id = ?", (fid,)).fetchone()
    if t is not None:
        cur = d.execute(
            "INSERT INTO frame_text (frame_id, text, text_fold, ocr_version, line_count) VALUES (?, ?, ?, ?, ?)",
            (new, t["text"], t["text_fold"], t["ocr_version"], t["line_count"]),
        )
        d.execute("INSERT INTO ocr_fts (rowid, text_fold) VALUES (?, ?)", (cur.lastrowid, t["text_fold"]))
    d.execute(
        "INSERT INTO ocr_lines (frame_id, line_no, text, confidence, quad, x0, y0, x1, y1, char_start, char_end) "
        "SELECT ?, line_no, text, confidence, quad, x0, y0, x1, y1, char_start, char_end FROM ocr_lines WHERE frame_id = ?",
        (new, fid),
    )
    for fb in d.execute(
        "SELECT fb.*, b.block_key, b.text AS btext, b.norm_version, b.chunker_version AS bver, b.token_count "
        "FROM frame_blocks fb JOIN text_blocks b ON b.block_id = fb.block_id WHERE fb.frame_id = ?", (fid,)
    ).fetchall():
        key = hashlib.blake2b(f"{fb['block_key']}|bench|{idx}".encode(), digest_size=16).hexdigest()
        cur = d.execute(
            "INSERT INTO text_blocks (block_key, text, norm_version, chunker_version, token_count, created_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, fb["btext"], fb["norm_version"], fb["bver"], fb["token_count"], 0),
        )
        bid = cur.lastrowid
        d.execute(
            "INSERT INTO frame_blocks (frame_id, seq, block_id, chunker_version, line_start, line_end, char_start, "
            "char_end, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (new, fb["seq"], bid, fb["chunker_version"], fb["line_start"], fb["line_end"], fb["char_start"],
             fb["char_end"], fb["x0"], fb["y0"], fb["x1"], fb["y1"]),
        )
        for e in d.execute(
            "SELECT * FROM embeddings WHERE target_kind = 'block' AND target_id = ?", (str(fb["block_id"]),)
        ).fetchall():
            _copy_emb(d, e, "block", str(bid), rng, noise)
    for e in d.execute("SELECT * FROM embeddings WHERE target_kind = 'frame' AND target_id = ?", (fid,)).fetchall():
        _copy_emb(d, e, "frame", new, rng, noise)


def _copy_emb(d, e, kind: str, target: str, rng, noise: float) -> None:
    d.execute(
        "INSERT INTO embeddings (target_kind, target_id, branch, model_id, preprocess_version, dim, dtype, vector, "
        "created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, target, e["branch"], e["model_id"], e["preprocess_version"], e["dim"], e["dtype"],
         _noisy(e["vector"], e["dim"], rng, noise), e["created_at_ms"]),
    )


# ======================= query =======================

DEFAULT_QUERIES = {
    retrieval.TEXT: ["同事推荐的餐厅", "API 文档", "电池容量", "error 0x80070005", "meeting notes", "订单 缴费通知"],
    retrieval.IMAGE: ["深色背景的代码编辑器", "白色网页 表格", "雪山大图", "dark terminal window", "chat window"],
}


@dataclass
class _Timed:
    fn: Callable[[Sequence[str]], np.ndarray]
    last_ms: float = 0.0

    def __call__(self, texts):
        t0 = time.perf_counter()
        out = self.fn(texts)
        self.last_ms = (time.perf_counter() - t0) * 1000
        return out


def real_engines(cfg, mode: str) -> retrieval.Engines:
    mc = cfg.models
    if mode == retrieval.TEXT:
        enc = models.StText(mc.text, mc)
        return retrieval.Engines(text_encoder=enc.encode, text_index=retrieval.TextVectorIndex(mc.text.model_id))
    enc = models.StText(mc.clip_text, mc)
    return retrieval.Engines(clip_text_encoder=enc.encode,
                             image_index=retrieval.ImageVectorIndex(models.image_model_id(mc)))


def bench_query(conn: sqlite3.Connection, engines: retrieval.Engines, mode: str, queries: Iterable[str],
                repeat: int = 5, **run_kw) -> dict:
    """第一轮（矩阵首次加载 + 编码器首次调用）记为冷查询，之后每条查询重复 repeat 次记为热查询。"""
    attr = "text_encoder" if mode == retrieval.TEXT else "clip_text_encoder"
    timed = _Timed(getattr(engines, attr))
    setattr(engines, attr, timed)
    queries = list(queries)
    cold, hot, enc_ms, rest_ms, empty = [], [], [], [], 0
    for rnd in range(repeat + 1):
        for raw in queries:
            timed.last_ms = 0.0
            t0 = time.perf_counter()
            resp = retrieval.run(conn, search.parse_query(raw), search.Filters(), mode=mode, engines=engines, **run_kw)
            total = (time.perf_counter() - t0) * 1000
            if rnd == 0:
                cold.append(total)
                continue
            hot.append(total)
            enc_ms.append(timed.last_ms)
            rest_ms.append(total - timed.last_ms)
            empty += not resp.results
    frames = conn.execute("SELECT COUNT(*) FROM frames WHERE image_status != 'deleted'").fetchone()[0]
    return {
        "mode": mode, "frames": frames, "queries": len(queries), "repeat": repeat,
        "cold_ms": summary(cold), "hot_ms": summary(hot),
        "hot_encode_ms": summary(enc_ms), "hot_db_rank_ms": summary(rest_ms),
        "hot_empty_results": empty,
        "note": "热查询 = 同一进程内第二轮起；冷查询含首次读入向量矩阵，不含进程启动与模型加载",
    }


# ======================= readiness =======================

def readiness(conn: sqlite3.Connection) -> dict:
    """每个阶段：done 的延迟 = 任务最后更新时间 − 抓拍时间（含排队与重试）；未完成 / 失败 / 取消单独计数。"""
    out = {}
    for stage in ("ocr", "text_embed", "image_embed"):
        rows = conn.execute(
            "SELECT j.status, j.updated_at_ms - f.captured_at_ms AS lat FROM index_jobs j "
            "JOIN frames f ON f.frame_id = j.frame_id WHERE j.stage = ?", (stage,)
        ).fetchall()
        done = [float(r["lat"]) / 1000 for r in rows if r["status"] == "done"]
        out[stage] = {
            "latency_s": summary(done),
            "unfinished": sum(r["status"] in ("pending", "running") for r in rows),
            "failed": sum(r["status"] == "failed" for r in rows),
            "cancelled": sum(r["status"] == "cancelled" for r in rows),
        }
    out["note"] = "导入的截图抓拍时间可能是用户给定的旧时间，统计就绪延迟时应只用实时录制的数据"
    return out


# ======================= mem =======================

def _proc_tree(psutil, root):
    procs = [root]
    try:
        procs += root.children(recursive=True)
    except psutil.Error:
        pass
    return procs


def find_processes(psutil, patterns: Sequence[str]) -> dict[str, list]:
    """按命令行子串找进程根（如 screenrecall.daemon、streamlit），再带上全部子进程。"""
    found: dict[str, list] = {p: [] for p in patterns}
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        if proc.pid == me:
            continue
        cmd = " ".join(proc.info.get("cmdline") or [])
        for p in patterns:
            if p in cmd:
                found[p].append(proc)
    # 只保留"根"：父进程也命中同一模式的，归到父进程的树里
    roots: dict[str, list] = {}
    for p, procs in found.items():
        pids = {x.pid for x in procs}
        roots[p] = [x for x in procs if (x.ppid() not in pids)]
    return roots


def sample_memory(psutil, groups: dict[str, list], duration_s: float, interval_s: float = 1.0) -> dict:
    """每 interval_s 采样一次：各组（含子进程）CPU% 与 RSS / USS；CPU% 同时给单核口径与整机口径。"""
    ncpu = psutil.cpu_count() or 1
    trees = {g: [q for r in roots for q in _proc_tree(psutil, r)] for g, roots in groups.items()}
    for procs in trees.values():
        for q in procs:
            try:
                q.cpu_percent(None)
            except psutil.Error:
                pass
    series = {g: {"cpu_core_pct": [], "rss_mib": [], "uss_mib": []} for g in trees}
    series["total"] = {"cpu_core_pct": [], "rss_mib": [], "uss_mib": []}
    end = time.monotonic() + duration_s
    while True:
        time.sleep(interval_s)
        tot = {"cpu_core_pct": 0.0, "rss_mib": 0.0, "uss_mib": 0.0}
        for g, procs in trees.items():
            cpu = rss = uss = 0.0
            uss_ok = True
            for q in procs:
                try:
                    cpu += q.cpu_percent(None)
                    rss += q.memory_info().rss / 2**20
                except psutil.Error:
                    continue
                try:  # USS 需要更高权限，取不到时只报 RSS
                    uss += q.memory_full_info().uss / 2**20
                except (psutil.Error, AttributeError):
                    uss_ok = False
            if not uss_ok:
                uss = float("nan")
            for k, v in (("cpu_core_pct", cpu), ("rss_mib", rss), ("uss_mib", uss)):
                series[g][k].append(v)
                tot[k] += v
        for k, v in tot.items():
            series["total"][k].append(v)
        if time.monotonic() >= end:
            break
    out = {"cpu_count": ncpu, "interval_s": interval_s, "duration_s": duration_s,
           "pids": {g: [q.pid for q in procs] for g, procs in trees.items()}, "groups": {}}
    for g, s in series.items():
        uss = [v for v in s["uss_mib"] if v == v]  # 去掉 NaN（该次采样取不到 USS）
        out["groups"][g] = {
            "cpu_core_pct": summary(s["cpu_core_pct"]),
            "cpu_machine_pct": summary([v / ncpu for v in s["cpu_core_pct"]]),
            "rss_mib": summary(s["rss_mib"]),
            "uss_mib": summary(uss) if len(uss) == len(s["uss_mib"]) else None,
        }
    out["note"] = ("cpu_core_pct 以单核为 100%；cpu_machine_pct 以整机为 100%。RSS 含共享库，两进程相加会重复计入；"
                   "USS 是进程独占内存，更接近两进程合计的真实增量")
    return out


# ======================= CLI =======================

def _emit(obj: dict, out: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    print(text)
    if out:
        Path(out).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m screenrecall.bench", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("expand", help="复制数据目录并扩到指定帧数（仅供资源 / 速度测量）")
    e.add_argument("--src", required=True)
    e.add_argument("--dst", required=True)
    e.add_argument("--frames", type=int, default=2000)
    e.add_argument("--no-files", action="store_true", help="不复制图片文件（只测查询时可省磁盘）")
    qp = sub.add_parser("query", help="冷 / 热查询耗时")
    qp.add_argument("--data-dir", required=True)
    qp.add_argument("--mode", choices=[retrieval.TEXT, retrieval.IMAGE], default=retrieval.TEXT)
    qp.add_argument("--queries", help="每行一条查询的 UTF-8 文件；默认用内置的中英混合查询")
    qp.add_argument("--repeat", type=int, default=5)
    r = sub.add_parser("readiness", help="抓拍到可搜索的延迟")
    r.add_argument("--data-dir", required=True)
    m = sub.add_parser("mem", help="后台与页面的 CPU / 内存（进程树）")
    m.add_argument("--match", nargs="+", default=["screenrecall.daemon", "streamlit"])
    m.add_argument("--duration", type=float, default=60.0)
    m.add_argument("--interval", type=float, default=1.0)
    for p in (e, qp, r, m):
        p.add_argument("--out", help="同时写入 JSON 文件")
    args = ap.parse_args(argv)

    if args.cmd == "expand":
        _emit(expand(Path(args.src), Path(args.dst), args.frames, copy_files=not args.no_files), args.out)
    elif args.cmd == "query":
        cfg = load_config(args.data_dir)
        qs = (Path(args.queries).read_text(encoding="utf-8").splitlines() if args.queries
              else DEFAULT_QUERIES[args.mode])
        qs = [x.strip() for x in qs if x.strip()]
        t0 = time.perf_counter()
        eng = real_engines(cfg, args.mode)
        load_s = time.perf_counter() - t0
        conn = db.connect(cfg.db_path)
        try:
            res = bench_query(conn, eng, args.mode, qs, args.repeat,
                              rrf_k=cfg.search.rrf_k, branch_top=cfg.search.branch_top)
        finally:
            conn.close()
        res["model_load_s"] = round(load_s, 2)
        _emit(res, args.out)
    elif args.cmd == "readiness":
        cfg = load_config(args.data_dir)
        conn = db.connect(cfg.db_path)
        try:
            _emit(readiness(conn), args.out)
        finally:
            conn.close()
    else:
        try:
            import psutil
        except ImportError:
            print("需要 psutil：pip install -r requirements.txt", file=sys.stderr)
            return 2
        groups = find_processes(psutil, args.match)
        missing = [g for g, roots in groups.items() if not roots]
        if missing:
            print(f"没有找到进程：{missing}（命令行需包含这些子串）", file=sys.stderr)
        _emit(sample_memory(psutil, {g: v for g, v in groups.items() if v}, args.duration, args.interval), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
