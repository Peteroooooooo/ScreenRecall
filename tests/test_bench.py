"""测量工具冒烟（方案 7.3）：只检查 bench 的统计口径和扩库结构，数字本身没有意义，必须在 Windows 测试机上测。"""

import os
import subprocess
import sys

import pytest

from screenrecall import bench, db, retrieval, search

from .conftest import ROOT, make_image
from .test_retrieval import Pipeline, q


def test_percentiles_use_nearest_rank():
    vals = list(range(1, 21))
    assert bench.pct(vals, 50) == 10 and bench.pct(vals, 95) == 19 and bench.pct(vals, 100) == 20
    assert bench.pct([], 95) is None
    assert bench.summary([2.0, 4.0])["mean"] == 3.0


@pytest.fixture
def lib(h):
    p = Pipeline(h)
    fids = [p.add(make_image(i), [f"invoice number {i}", "shared footer"]) for i in range(1, 4)]
    return p, fids


def test_expand_creates_unique_frames_blocks_and_vectors(lib, h, tmp_path):
    p, fids = lib
    blocks0 = h.conn.execute("SELECT COUNT(*) FROM text_blocks").fetchone()[0]
    dst = tmp_path / "bench"
    res = bench.expand(h.cfg.data_dir, dst, 20)
    assert res["frames"] == 20 and res["copied"] == 17
    c = db.connect(dst / "screenrecall.sqlite3")
    try:
        n = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        assert n("SELECT COUNT(*) FROM occurrences") >= 20
        assert n("SELECT COUNT(*) FROM frame_text") == 20 and n("SELECT COUNT(*) FROM ocr_fts") == 20
        assert n("SELECT COUNT(*) FROM text_blocks") > blocks0  # 复制帧有自己的块，矩阵随帧数增长
        assert n("SELECT COUNT(*) FROM embeddings WHERE target_kind = 'frame'") == 20
        assert n("SELECT COUNT(DISTINCT vector) FROM embeddings WHERE target_kind = 'frame'") == 20  # 加了噪声
        files = [r[0] for r in c.execute("SELECT image_path FROM frames")]
        assert all((dst / f).exists() for f in files)
        assert n(f"SELECT COUNT(*) FROM meta WHERE key = '{bench.BENCH_META}'") == 1
        # 扩充后的库仍能被正常检索
        assert len(search.search(c, q('"invoice"'), limit=50).results) == 20
    finally:
        c.close()
    # 源库不被改动
    assert h.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 3
    with pytest.raises(FileExistsError):
        bench.expand(h.cfg.data_dir, dst, 30)


def test_bench_query_separates_cold_hot_encode_and_rank(lib, h):
    p, _ = lib
    res = bench.bench_query(h.conn, p.engines(), retrieval.TEXT, ["invoice number", "shared footer"], repeat=3)
    assert res["cold_ms"]["n"] == 2 and res["hot_ms"]["n"] == 6
    assert res["hot_encode_ms"]["n"] == 6 and res["hot_db_rank_ms"]["n"] == 6
    assert res["hot_empty_results"] == 0 and res["frames"] == 3
    img = bench.bench_query(h.conn, p.engines(), retrieval.IMAGE, ["bright"], repeat=2)
    assert img["hot_ms"]["n"] == 2


def test_readiness_counts_done_and_unfinished(lib, h):
    h.rec.snapshot(h.conn, None)  # 同一画面复用，不新增待办
    h.show(make_image(9))
    h.rec.snapshot(h.conn, None)  # 新画面：OCR 待办未处理
    r = bench.readiness(h.conn)
    assert r["ocr"]["latency_s"]["n"] == 3 and r["ocr"]["unfinished"] == 1
    assert r["text_embed"]["latency_s"]["n"] == 3 and r["image_embed"]["failed"] == 0


def test_mem_samples_process_tree():
    psutil = pytest.importorskip("psutil")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)  # bench-mem-probe"])
    try:
        # Windows venv 的 python.exe 是启动器 shim，会再 spawn 真正的解释器；两者命令行都含标记，
        # 都该被采样（正是 bench 要统计整棵进程树的本意）。只断言根进程 child.pid 在其中。
        groups = bench.find_processes(psutil, ["bench-mem-probe"])
        pids = {x.pid for x in groups["bench-mem-probe"]}
        assert child.pid in pids
        child_proc = psutil.Process(child.pid)
        expected = {child.pid} | {c.pid for c in child_proc.children(recursive=True)}
        assert pids <= expected  # 只匹配到探针进程树，没有误伤无关进程
        out = bench.sample_memory(psutil, groups, duration_s=0.2, interval_s=0.1)
        g = out["groups"]["bench-mem-probe"]
        assert g["rss_mib"]["max"] > 0 and out["groups"]["total"]["rss_mib"]["n"] >= 1
        assert child.pid in out["pids"]["bench-mem-probe"]
        light = bench.sample_memory(psutil, groups, duration_s=0.2, interval_s=0.1, with_uss=False)
        assert light["groups"]["bench-mem-probe"]["uss_mib"] is None and not light["uss_sampled"]
        assert light["groups"]["bench-mem-probe"]["rss_mib"]["max"] > 0
    finally:
        child.kill()
        child.wait()


def test_cli_readiness_runs(lib, h, tmp_path):
    out = tmp_path / "r.json"
    env = {**os.environ, "SCREENRECALL_DATA_DIR": str(h.cfg.data_dir)}
    r = subprocess.run([sys.executable, "-m", "screenrecall.bench", "readiness", "--data-dir", str(h.cfg.data_dir),
                        "--out", str(out)], capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert out.exists() and '"ocr"' in out.read_text(encoding="utf-8")
