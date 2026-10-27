"""v2 存储性能测量：在临时目录里造合成库，量写入、检索与向量检索。不碰任何真实数据目录。

    python -m screenrecall.store.bench --lines 1000000 --vectors 200000,1000000

文字全是程序生成的（随机常用汉字词 + 伪英文词 + 若干已知频率的植入词），时间分布在 50 天里；
5% 的行模拟空闲 OCR 晚插入（rowid 大、时间早），30% 的行之后又被看到（last_seen 晚于 first_seen），
所以 rowid 顺序和"最近看到"顺序不同，能测出排序是否真的按 last_seen。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import tempfile
import time
from pathlib import Path

import numpy as np

from . import schema
from .fts import docs, lexical
from .vectors import VectorStore
from .writer import LineIn, Writer

DAY = 86_400_000
T0 = 1_780_000_000_000

# 植入词：(词, 每多少行出现一次)
PLANTED = [("微信", 40), ("组会纪要", 20_000), ("报销", 2_000), ("the", 3), ("kubernetes", 5_000),
           ("deadline", 400), ("嗯", 30_000)]


class TextGen:
    def __init__(self, seed: int = 1):
        self.rng = np.random.default_rng(seed)
        chars = np.arange(0x4E00, 0x4E00 + 3000)
        self.rng.shuffle(chars)
        self.chars = [chr(c) for c in chars]
        zipf = 1.0 / np.arange(1, len(self.chars) + 1) ** 0.9
        self.char_p = zipf / zipf.sum()
        letters = np.array(list("abcdefghijklmnopqrstuvwxyz"))
        self.words = ["".join(self.rng.choice(letters, self.rng.integers(2, 10))) for _ in range(20_000)]
        wz = 1.0 / np.arange(1, len(self.words) + 1) ** 1.0
        self.word_p = wz / wz.sum()

    def batch(self, n: int, start_index: int) -> list[str]:
        rng = self.rng
        out = []
        cn = rng.random(n) < 0.6
        lens_cn = rng.integers(6, 30, n)
        lens_en = rng.integers(4, 12, n)
        cpool = rng.choice(len(self.chars), size=int(lens_cn.sum()), p=self.char_p)
        wpool = rng.choice(len(self.words), size=int(lens_en.sum()), p=self.word_p)
        ci = wi = 0
        for k in range(n):
            if cn[k]:
                m = int(lens_cn[k])
                s = "".join(self.chars[j] for j in cpool[ci:ci + m])
                ci += m
            else:
                m = int(lens_en[k])
                s = " ".join(self.words[j] for j in wpool[wi:wi + m])
                wi += m
            idx = start_index + k
            planted = [(f" {w} " if w.isascii() else w) for w, every in PLANTED if idx % every == every // 2]
            if planted:  # 一次插在同一处，后插的词不会把先插的词拆开
                s = s[: len(s) // 2] + "".join(planted) + s[len(s) // 2:]
            out.append(s)
        return out


def build(data_dir: Path, n_lines: int, *, lines_per_block: int = 40, blocks_per_segment: int = 5,
          seed: int = 1, log=print) -> dict:
    """直接用 SQL 批量建库（与写线程相同的表、索引文档和 tail 规则，但不做逐行匹配）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = schema.connect(schema.db_path(data_dir))
    gen = TextGen(seed)
    rng = np.random.default_rng(seed + 1)
    apps = [f"app{i:02d}.exe" for i in range(20)]
    t_start = time.perf_counter()
    line_id = 0
    block_id = 0
    seg_id = 0
    chunk = 20_000
    made = 0
    late = int(n_lines * 0.05)
    main = n_lines - late
    while made < n_lines:
        n = min(chunk, n_lines - made)
        texts = gen.batch(n, made)
        is_late = made >= main
        conn.execute("BEGIN")
        rows, fts_rows = [], []
        for start in range(0, n, lines_per_block):
            block_texts = texts[start:start + lines_per_block]
            if block_id % blocks_per_segment == 0 or is_late:
                seg_id += 1
                app = apps[int(rng.integers(len(apps)))]
                # 时间：主体按 rowid 递增铺满 50 天；晚插入的随机落在更早的时间
                frac = (made + start) / max(1, main)
                t = T0 + int((rng.random() if is_late else frac) * 50 * DAY)
                conn.execute("INSERT INTO segments (segment_id, app, title, first_seen_ms, last_seen_ms) "
                             "VALUES (?, ?, ?, ?, ?)", (seg_id, app, f"窗口标题 {seg_id}", t, t + 600_000))
            block_id += 1
            conn.execute("INSERT INTO blocks (block_id, segment_id, created_ms, last_seen_ms) VALUES (?, ?, ?, ?)",
                         (block_id, seg_id, t, t))
            norms = [docs.norm(x) for x in block_texts]
            for i, (x, nm) in enumerate(zip(block_texts, norms)):
                line_id += 1
                first = t + i * 1000
                last = first + (int(rng.integers(1, 30 * DAY)) if rng.random() < 0.3 else int(rng.integers(0, 60_000)))
                nxt = norms[i + 1] if i + 1 < len(norms) else None
                tail = docs.tail_of(nxt)
                y = float(i * 30)
                rows.append((line_id, block_id, seg_id, 10.0, y, 600.0, y + 20, y + 10e-6, x, nm, tail,
                             line_id + 1 if nxt is not None else None, "ocr" if is_late else "uia", 1.0, first, last))
                fts_rows.append((line_id, x, nm, tail))
        conn.executemany(
            "INSERT INTO lines (line_id, block_id, segment_id, x0, y0, x1, y1, order_key, text, norm, tail, "
            "tail_line_id, source, confidence, first_seen_ms, last_seen_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        docs.index_rows(conn, fts_rows)
        conn.execute("COMMIT")
        made += n
        if made % 500_000 < chunk:
            log(f"  已建 {made:,} 行，{time.perf_counter() - t_start:.0f} s")
    elapsed = time.perf_counter() - t_start
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("ANALYZE")
    sizes = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT CASE WHEN name LIKE 'lines_tri%' THEN 'fts_trigram' WHEN name LIKE 'lines_bi%' THEN 'fts_bigram' "
        "WHEN name = 'lines' THEN 'lines' WHEN name LIKE 'idx_lines%' THEN 'line_indexes' ELSE 'other' END, "
        "SUM(pgsize) FROM dbstat GROUP BY 1")}
    conn.close()
    return {"lines": n_lines, "segments": seg_id, "build_s": round(elapsed, 1),
            "bulk_lines_per_s": int(n_lines / elapsed),
            "db_mb": round(schema.db_path(data_dir).stat().st_size / 2 ** 20, 1),
            "size_mb": {k: round(v / 2 ** 20, 1) for k, v in sizes.items()}}


def writer_rate(data_dir: Path, n_stills: int = 1500, lines_per_still: int = 40, new_per_still: int = 8) -> dict:
    """真实写入路径：每次"看清"一屏 40 行，其中 8 行是新出现的（滚动），其余只更新 last_seen。"""
    gen = TextGen(9)
    w = Writer(data_dir)
    sid = w.segment_for("bench.exe", "写入测速", T0).result(30)
    b = w.block_new(sid, T0).result(30)
    pool = gen.batch(n_stills * new_per_still + lines_per_still, 10_000_000)
    lat: list[float] = []
    t0 = time.perf_counter()
    futs = []
    for k in range(n_stills):
        top = k * new_per_still
        lines = [LineIn(pool[top + i], 10, (top + i) * 30, 600, (top + i) * 30 + 20)
                 for i in range(lines_per_still)]
        s = time.perf_counter()
        futs.append(w.record_lines(b, T0 + k * 1000, (top * 30, (top + lines_per_still) * 30), lines))
        lat.append((time.perf_counter() - s) * 1000)
    for f in futs:
        f.result(120)
    elapsed = time.perf_counter() - t0
    # 单批事务时长：逐个同步提交测一次（各批 < 50 ms 的约束）
    batch_ms = []
    for k in range(50):
        top = (n_stills + k) * new_per_still
        lines = [LineIn(f"同步测量 {k}-{i} " + pool[i % len(pool)], 10, (top + i) * 30, 600, (top + i) * 30 + 20)
                 for i in range(lines_per_still)]
        s = time.perf_counter()
        w.record_lines(b, T0 + (n_stills + k) * 1000, (top * 30, (top + lines_per_still) * 30), lines).result(30)
        batch_ms.append((time.perf_counter() - s) * 1000)
    w.close()
    inserted = n_stills * new_per_still + lines_per_still - new_per_still
    return {"stills": n_stills, "lines_per_still": lines_per_still, "new_lines": inserted,
            "stills_per_s": round(n_stills / elapsed, 1), "new_lines_per_s": int(inserted / elapsed),
            "submit_ms_max": round(max(lat), 3), "submit_ms_p50": round(statistics.median(lat), 4),
            "one_still_roundtrip_ms_p50": round(statistics.median(batch_ms), 2),
            "one_still_roundtrip_ms_max": round(max(batch_ms), 2)}


def query_bench(data_dir: Path, repeat: int = 5) -> list[dict]:
    conn = schema.connect_ro(schema.db_path(data_dir))
    t_end = T0 + 50 * DAY
    cases = [
        ("2 字中文·常见", "微信", {}),
        ("2 字中文·较少", "报销", {}),
        ("2 字中文·随机", None, {}),
        ("1 字中文·很少", "嗯", {}),
        ("3-4 字中文·少", "组会纪要", {}),
        ("3-4 字中文·随机", None, {}),
        ("英文·少", "kubernetes", {}),
        ("英文·中等", "deadline", {}),
        ("常见词 the", "the", {}),
        ("常见词 the + 最近 1 天", "the", {"time_from": t_end - DAY}),
        ("常见词 the + 应用筛选", "the", {"app": "app07.exe"}),
        ("中等词 + 应用 + 7 天", "deadline", {"app": "app03.exe", "time_from": t_end - 7 * DAY}),
        ("两个词 AND", "微信 deadline", {}),
        ("跨行短语", None, {}),
    ]
    # 随机词取库里真实出现过的片段
    row = conn.execute("SELECT norm FROM lines WHERE line_id = 777").fetchone()
    cn_row = conn.execute("SELECT norm FROM lines WHERE norm GLOB '[一-龥]*' AND length(norm) > 10 "
                          "AND line_id > (SELECT MAX(line_id) / 3 FROM lines) LIMIT 1").fetchone()
    wrap = conn.execute("SELECT norm, tail FROM lines WHERE length(tail) >= 4 AND length(norm) >= 4 "
                        "AND line_id > (SELECT MAX(line_id) / 2 FROM lines) LIMIT 1").fetchone()
    out = []
    for label, q, filt in cases:
        if q is None:
            if label.startswith("2 字"):
                q = (cn_row or row)[0][3:5]
            elif label.startswith("3-4"):
                q = (cn_row or row)[0][2:6]
            else:
                q = wrap[0][-3:] + wrap[1][:3]
        times = []
        res = None
        for _ in range(repeat):
            s = time.perf_counter()
            res = lexical.search(conn, q, filters=lexical.Filters(**filt), limit=20, per_group=3)
            times.append((time.perf_counter() - s) * 1000)
        out.append({"case": label, "query_len": len(docs.norm(q)), "filters": list(filt),
                    "ms_median": round(statistics.median(times), 2), "ms_max": round(max(times), 2),
                    "mode": res.mode, "total": res.total, "lower_bound": res.total_is_lower_bound,
                    "groups": len(res.groups)})
    conn.close()
    return out


def vector_bench(root: Path, n: int, dim: int = 384, repeat: int = 7) -> dict:
    rng = np.random.default_rng(3)
    vs = VectorStore(root, dim)
    chunk = 50_000
    t0 = time.perf_counter()
    for start in range(0, n, chunk):
        m = min(chunk, n - start)
        v = rng.standard_normal((m, dim), dtype=np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        ids = np.arange(start + 1, start + m + 1)
        vs.append(ids, v, T0 + ids * 1000, ids // 50)
    append_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    reader = VectorStore(root, dim, readonly=True)
    open_s = time.perf_counter() - t0
    q = rng.standard_normal(dim).astype(np.float32)
    reader.search(q, 50)  # 预热（页缓存）

    def timeit(**kw):
        ts = []
        for _ in range(repeat):
            s = time.perf_counter()
            reader.search(q, 50, **kw)
            ts.append((time.perf_counter() - s) * 1000)
        return round(statistics.median(ts), 2)

    span = n * 1000
    res = {
        "rows": n, "dim": dim, "disk_mb": round(n * dim * 4 / 2 ** 20),
        "append_rows_per_s": int(n / append_s), "reader_open_s": round(open_s, 3),
        "search_ms": timeit(), "search_10pct_time_ms": timeit(time_from=T0 + int(span * 0.9)),
        "search_best_per_group_ms": timeit(best_per_group=True),
        "search_group_filter_ms": timeit(groups=np.arange(0, n // 50, 10)),
    }
    s = time.perf_counter()
    vs.append(np.arange(n + 1, n + 1001), rng.standard_normal((1000, dim), dtype=np.float32),
              np.full(1000, T0), np.zeros(1000))
    reader.refresh()
    res["incremental_refresh_1000_ms"] = round((time.perf_counter() - s) * 1000, 2)
    s = time.perf_counter()
    vs.delete(np.arange(1, 1001))
    res["delete_1000_ms"] = round((time.perf_counter() - s) * 1000, 2)
    s = time.perf_counter()
    reader.refresh()
    res["refresh_after_delete_ms"] = round((time.perf_counter() - s) * 1000, 2)
    reader.close()
    vs.close()
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.store.bench")
    ap.add_argument("--lines", type=int, default=1_000_000)
    ap.add_argument("--vectors", default="200000,1000000")
    ap.add_argument("--dir", help="临时目录（默认在系统临时目录下新建，结束后删除）")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--out", help="把结果写成 JSON")
    args = ap.parse_args(argv)
    base = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="sr-store-bench-"))
    base.mkdir(parents=True, exist_ok=True)
    result: dict = {}
    try:
        print("写入路径（写线程 record_lines）…")
        result["writer"] = writer_rate(base / "writer")
        print(json.dumps(result["writer"], ensure_ascii=False))
        if args.lines:
            print(f"批量建库 {args.lines:,} 行…")
            result["build"] = build(base / "big", args.lines)
            print(json.dumps(result["build"], ensure_ascii=False))
            result["queries"] = query_bench(base / "big")
            for r in result["queries"]:
                print(json.dumps(r, ensure_ascii=False))
            print("写入路径（在大库上）…")
            result["writer_on_big_db"] = writer_rate(base / "big", n_stills=500)
            print(json.dumps(result["writer_on_big_db"], ensure_ascii=False))
        result["vectors"] = []
        for n in [int(x) for x in args.vectors.split(",") if x]:
            print(f"向量 {n:,} x 384…")
            r = vector_bench(base / f"vec-{n}", n)
            result["vectors"].append(r)
            print(json.dumps(r, ensure_ascii=False))
            shutil.rmtree(base / f"vec-{n}", ignore_errors=True)
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
