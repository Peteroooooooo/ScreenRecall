"""v2 存储测速脚本的小规模冒烟：合成库能建、检索能跑、植入词按预期频率出现。正式数字见 results/。"""

import numpy as np

from screenrecall.store import bench, schema
from screenrecall.store.fts import lexical


def test_build_and_query_small(tmp_path):
    info = bench.build(tmp_path / "big", 6000, log=lambda *_: None)
    assert info["lines"] == 6000 and info["db_mb"] > 0
    c = schema.connect_ro(schema.db_path(tmp_path / "big"))
    # 植入词：组会纪要每 20000 行一次 → 6000 行里没有；微信每 40 行一次
    assert lexical.search(c, "组会纪要").total == 0
    assert lexical.search(c, "微信", limit=1000).total >= 150
    # rowid 顺序与"最近看到"不同（晚插入 + 再次看到）
    ids = [r[0] for r in c.execute("SELECT line_id FROM lines ORDER BY last_seen_ms DESC LIMIT 200")]
    assert ids != sorted(ids, reverse=True)
    c.close()
    rows = bench.query_bench(tmp_path / "big", repeat=1)
    assert len(rows) == 14 and all(r["ms_median"] >= 0 for r in rows)


def test_writer_rate_and_vectors_small(tmp_path):
    r = bench.writer_rate(tmp_path / "w", n_stills=30)
    assert r["stills"] == 30 and r["new_lines_per_s"] > 0
    v = bench.vector_bench(tmp_path / "v", 2000, dim=32, repeat=1)
    assert v["rows"] == 2000 and v["search_ms"] >= 0


def test_textgen_is_deterministic():
    a = bench.TextGen(5).batch(50, 0)
    b = bench.TextGen(5).batch(50, 0)
    assert a == b and len(set(a)) > 40
    assert np.mean([len(x) for x in a]) > 5
