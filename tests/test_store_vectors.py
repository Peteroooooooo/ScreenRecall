"""v2 向量库：追加、增量刷新、时间 / id / 组筛选、每组最高分、墓碑清零、压缩与半截写入修复。"""

import numpy as np
import pytest

from screenrecall.store.vectors import VectorStore

DIM = 16


def unit(rng, n):
    v = rng.standard_normal((n, DIM)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def brute(vecs, ids, q, k, mask=None):
    s = vecs @ (q / np.linalg.norm(q))
    if mask is not None:
        s = np.where(mask, s, -np.inf)
    order = np.lexsort((ids, -s))[:k]
    return [int(ids[i]) for i in order if np.isfinite(s[i])]


@pytest.fixture
def data():
    rng = np.random.default_rng(7)
    n = 1000
    return rng, unit(rng, n), np.arange(1, n + 1), np.arange(n) * 10, np.arange(n) % 37


def test_append_and_exact_topk(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    vs = VectorStore(tmp_path / "v", DIM, segment_rows=128)
    assert vs.append(ids, vecs, ts, grp) == 1000
    vs.refresh()
    assert len(vs) == 1000
    assert len(list((tmp_path / "v").glob("*.vec"))) == 8  # 跨段
    for _ in range(5):
        q = rng.standard_normal(DIM).astype(np.float32)
        assert [h.id for h in vs.search(q, 10)] == brute(vecs, ids, q, 10)
    h = vs.search(vecs[5], 1)[0]
    assert h.id == 6 and h.score == pytest.approx(1.0, abs=1e-5) and h.ts == 50 and h.group == 5


def test_filters_time_ids_groups(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    vs = VectorStore(tmp_path / "v", DIM, segment_rows=100)
    vs.append(ids, vecs, ts, grp)
    q = rng.standard_normal(DIM).astype(np.float32)
    m = (ts >= 2000) & (ts < 5000)
    assert [h.id for h in vs.search(q, 20, time_from=2000, time_to=5000)] == brute(vecs, ids, q, 20, m)
    allow = rng.choice(ids, 50, replace=False)
    assert [h.id for h in vs.search(q, 7, ids=allow)] == brute(vecs, ids, q, 7, np.isin(ids, allow))
    assert [h.id for h in vs.search(q, 7, groups=[3, 4])] == brute(vecs, ids, q, 7, np.isin(grp, [3, 4]))
    assert vs.search(q, 5, time_from=10 ** 9) == []


def test_best_per_group(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    vs = VectorStore(tmp_path / "v", DIM, segment_rows=300)
    vs.append(ids, vecs, ts, grp)
    q = rng.standard_normal(DIM).astype(np.float32)
    s = vecs @ (q / np.linalg.norm(q))
    best = {}
    for i in range(len(ids)):
        if grp[i] not in best or s[i] > s[best[grp[i]]]:
            best[grp[i]] = i
    expect = sorted(best.values(), key=lambda i: -s[i])[:10]
    hits = vs.search(q, 10, best_per_group=True)
    assert [h.id for h in hits] == [int(ids[i]) for i in expect]
    assert len({h.group for h in hits}) == 10


def test_reader_refreshes_incrementally(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    w = VectorStore(tmp_path / "v", DIM, segment_rows=64)
    r = VectorStore(tmp_path / "v", DIM, readonly=True)
    assert len(r) == 0
    w.append(ids[:100], vecs[:100], ts[:100], grp[:100])
    r.refresh()
    assert len(r) == 100
    segs_before = dict(r._segments)
    w.append(ids[100:150], vecs[100:150], ts[100:150], grp[100:150])
    r.refresh()
    assert len(r) == 150
    assert r._segments[0] is segs_before[0]  # 已满的段没有重新加载
    assert r.search(vecs[120], 1)[0].id == 121


def test_readonly_store_on_missing_dir(tmp_path):
    r = VectorStore(tmp_path / "nothing", DIM, readonly=True)
    assert r.search(np.ones(DIM), 3) == []
    with pytest.raises(RuntimeError):
        r.append([1], np.ones((1, DIM)), [1])


def test_delete_zeroes_rows_and_tombstones_mask_readers(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    marker = np.full(DIM, 123.456, np.float32)  # 可在文件里认出来的字节
    v2 = vecs.copy()
    v2[10] = marker
    w = VectorStore(tmp_path / "v", DIM, segment_rows=256)
    r = VectorStore(tmp_path / "v", DIM, readonly=True)
    w.append(ids, v2, ts, grp)
    r.refresh()
    assert r.search(marker, 1)[0].id == 11
    raw = lambda: b"".join(p.read_bytes() for p in (tmp_path / "v").glob("*.vec"))  # noqa: E731
    assert marker.tobytes() in raw()
    assert w.delete([11, 12, 99999]) == 2
    assert marker.tobytes() not in raw()
    r.refresh()
    assert 11 not in [h.id for h in r.search(marker, 50)]
    assert len(r) == 998


def test_compact_rewrites_segments_and_keeps_results(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    w = VectorStore(tmp_path / "v", DIM, segment_rows=256)
    w.append(ids, vecs, ts, grp)
    dead = ids[:300]
    w.delete(dead)
    q = rng.standard_normal(DIM).astype(np.float32)
    before = [h.id for h in w.search(q, 20)]
    size_before = sum(p.stat().st_size for p in (tmp_path / "v").glob("*.vec"))
    assert w.compact() == 2  # 第 0、1 段有死行；最后一段（仍在追加）不动
    assert [h.id for h in w.search(q, 20)] == before
    size_after = sum(p.stat().st_size for p in (tmp_path / "v").glob("*.vec"))
    assert size_after < size_before
    r = VectorStore(tmp_path / "v", DIM, readonly=True)
    assert [h.id for h in r.search(q, 20)] == before and len(r) == 700
    w.append([5000], vecs[:1], [1], [1])  # 压缩后仍能追加
    r.refresh()
    assert len(r) == 701


def test_repair_truncated_append(tmp_path, data):
    rng, vecs, ids, ts, grp = data
    w = VectorStore(tmp_path / "v", DIM, segment_rows=1000)
    w.append(ids[:10], vecs[:10], ts[:10], grp[:10])
    # 模拟写到一半退出：vec 多写了半行，ids 少一行
    p = next((tmp_path / "v").glob("*.vec"))
    with open(p, "ab") as fh:
        fh.write(b"\x01" * (DIM * 2))
    pid = next((tmp_path / "v").glob("*.ids"))
    with open(pid, "r+b") as fh:
        fh.truncate(9 * 8)
    w2 = VectorStore(tmp_path / "v", DIM)
    assert len(w2) == 9
    w2.append(ids[10:12], vecs[10:12], ts[10:12], grp[10:12])
    w2.refresh()
    assert len(w2) == 11 and w2.search(vecs[11], 1)[0].id == 12


def test_dimension_mismatch(tmp_path):
    VectorStore(tmp_path / "v", DIM)
    with pytest.raises(ValueError):
        VectorStore(tmp_path / "v", DIM + 1)
    vs = VectorStore(tmp_path / "v", DIM)
    with pytest.raises(ValueError):
        vs.search(np.ones(DIM + 1), 1)
    with pytest.raises(ValueError):
        vs.append([1, 2], np.ones((1, DIM)), [1, 2])
