"""向量命中判定（store.gate）：命中行的"凑数程度"修正、门槛、纯数字查询不走语义分支；Queries / SearchService 接上。"""

import numpy as np
import pytest

from screenrecall.store.gate import PROBES, GateConfig, VectorGate, has_letters
from screenrecall.store.queries import Queries
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import LineIn, Writer

DIM = 8
E = np.eye(DIM, dtype=np.float32)


def unit(*parts):
    v = np.sum([w * E[i] for i, w in parts], axis=0).astype(np.float32)
    return v / np.linalg.norm(v)


# 泛泛的查询都在 e7 方向：和 e7 沾边的行就是"和什么都有点像"的行
PROBE_VECS = np.stack([E[7]] * 4 + [unit((7, 0.8), (6, 0.6))] * 2)


def test_has_letters():
    assert has_letters("bike") and has_letters("第七章") and has_letters("1999年") and has_letters("v2")
    assert not has_letters("13800138000") and not has_letters("3.14159") and not has_letters(" 12:30 ")


def test_hubness_is_mean_of_top_k_probe_similarities_clamped_at_zero():
    g = VectorGate(PROBE_VECS, GateConfig(k=2))
    h = g.hubness(np.stack([E[7], E[0], -E[7], unit((0, 0.6), (7, 0.8))]))
    assert h == pytest.approx([1.0, 0.0, 0.0, 0.8], abs=1e-6)
    assert g.hubness(np.zeros((0, DIM), np.float32)).shape == (0,)
    with pytest.raises(ValueError):
        VectorGate(np.zeros((0, DIM), np.float32))


def test_select_drops_hub_lines_and_hits_far_below_the_best():
    g = VectorGate(PROBE_VECS, GateConfig(floor=0.4, margin=0.15, k=2))
    hub = unit((0, 0.6), (7, 0.8))      # 余弦 0.6，但和泛泛的查询也像（0.8）→ 修正分 -0.2
    plain = unit((0, 0.6), (1, 0.8))    # 余弦 0.6，和泛泛的查询不像 → 修正分 0.6
    weak = unit((0, 0.42), (2, 0.9))    # 修正分 0.42 >= floor，但比最好的 0.6 低了 0.18 > margin
    keep, adj = g.select([0.6, 0.6, 0.42], np.stack([hub, plain, weak]))
    assert keep.tolist() == [1] and adj == pytest.approx([0.6], abs=1e-5)
    keep, adj = VectorGate(PROBE_VECS, GateConfig(floor=0.4, margin=1.0, k=2)).select(
        [0.42, 0.6, 0.6], np.stack([weak, plain, hub]))
    assert keep.tolist() == [1, 0]  # 按修正分从高到低
    assert g.select([], np.zeros((0, DIM), np.float32))[0].size == 0


@pytest.fixture
def env(tmp_path):
    w = Writer(tmp_path)
    q = Queries(tmp_path, clock=lambda: 10 ** 7)
    yield w, q, tmp_path
    q.close()
    w.close()


def put(w, app, title, t, texts):
    sid = w.segment_for(app, title, t).result(5)
    b = w.block_new(sid, t).result(5)
    ids = w.record_lines(b, t, (0, 40 + 30 * len(texts)),
                         [LineIn(x, 10, 10 + i * 30, 600, 30 + i * 30) for i, x in enumerate(texts)]).result(5)
    return sid, ids


def _store(tmp, ids, vecs, segs):
    vs = VectorStore(tmp / "vectors" / "text2", DIM)
    vs.append(ids, np.stack(vecs), [1000 + i for i in range(len(ids))], segs)
    return VectorStore(tmp / "vectors" / "text2", DIM, readonly=True)


def test_queries_gate_rejects_a_hub_line_that_the_fixed_rule_keeps(env):
    w, q, tmp = env
    s1, a = put(w, "chat.exe", "群", 1000, ["韩梅梅 10:07"])
    s2, b = put(w, "edge.exe", "文章", 2000, ["和查询意思相近的一行"])
    q.text_vectors = _store(tmp, [a[0], b[0]], [unit((0, 0.6), (7, 0.8)), unit((0, 0.6), (1, 0.8))], [s1, s2])
    enc = lambda text: E[0]  # noqa: E731
    gate = VectorGate(PROBE_VECS, GateConfig(floor=0.4, margin=0.15, floor_with_literal=0.5, k=2))
    fixed = q.search("毫不相干", encoder=enc, sort="relevant")
    assert sorted(g["segment_id"] for g in fixed["groups"]) == sorted([s1, s2])  # 两条余弦都是 0.6
    res = q.search("毫不相干", encoder=enc, sort="relevant", gate=gate)
    assert [g["segment_id"] for g in res["groups"]] == [s2]
    assert res["groups"][0]["semantic_score"] == pytest.approx(0.6, abs=1e-5)  # 修正分（凑数程度 0）


def test_queries_gate_bar_with_literal_hits_and_number_only_queries(env):
    w, q, tmp = env
    s1, a = put(w, "a.exe", "文档一", 1000, ["深度学习入门 2026"])
    s2, b = put(w, "b.exe", "文档二", 2000, ["随便一行"])
    mid = unit((0, 0.45), (1, np.sqrt(1 - 0.45 ** 2)))  # 修正分 0.45：过了 floor 0.4，没过有字面命中时的 0.5
    q.text_vectors = _store(tmp, [a[0], b[0]], [mid, mid], [s1, s2])
    enc = lambda text: E[0]  # noqa: E731
    gate = VectorGate(PROBE_VECS, GateConfig(floor=0.4, margin=0.15, floor_with_literal=0.5, k=2))
    assert [g["segment_id"] for g in q.search("深度学习", encoder=enc, sort="relevant", gate=gate)["groups"]] == [s1]
    assert sorted(g["segment_id"] for g in q.search("毫不相干", encoder=enc, gate=gate)["groups"]) == [s1, s2]
    # 纯数字：只有字面命中，不走语义分支（不编码）
    calls = []
    res = q.search("2026", encoder=lambda t: calls.append(t) or E[0], sort="relevant", gate=gate)
    assert [g["segment_id"] for g in res["groups"]] == [s1] and calls == []
    assert not res["mode"].endswith("+vector")
    assert q.search("13800138000", encoder=enc, gate=gate)["groups"] == []


class ProbeAwareEncoder:
    """查询编码成 e0；PROBES 编码成 e7 方向（和测试里的行都不像）。"""
    loaded = True

    def __init__(self):
        self.probe_calls = 0
        self.query_calls = 0

    def encode(self, texts):
        texts = list(texts)
        if texts == list(PROBES):
            self.probe_calls += 1
            return np.stack([E[7]] * len(texts))
        self.query_calls += 1
        return np.stack([E[0]] * len(texts))

    def unload(self):
        pass


def test_search_service_builds_the_gate_once_and_can_turn_it_off(tmp_path):
    from screenrecall.v2.searchsvc import SearchService, text_vectors_dir

    w = Writer(tmp_path)
    try:
        s1, a = put(w, "chat.exe", "群", 1000, ["韩梅梅 10:07"])
        s2, b = put(w, "edge.exe", "文章", 2000, ["和查询意思相近的一行"])
        w.flush()
        vs = VectorStore(text_vectors_dir(tmp_path), DIM)
        vs.append([a[0], b[0]], np.stack([unit((0, 0.6), (7, 0.8)), unit((0, 0.6), (1, 0.8))]), [1000, 2000],
                  [s1, s2])
        enc = ProbeAwareEncoder()
        svc = SearchService(tmp_path, encoder=enc, model_check=lambda: True)
        for _ in range(2):
            res = svc.search("毫不相干", sort="relevant")
            assert res["semantic"] is True and [g["segment_id"] for g in res["groups"]] == [s2]
        assert enc.probe_calls == 1 and enc.query_calls == 2
        svc.close()
        off = SearchService(tmp_path, encoder=ProbeAwareEncoder(), model_check=lambda: True, gate_config=None)
        assert sorted(g["segment_id"] for g in off.search("毫不相干")["groups"]) == sorted([s1, s2])
        off.close()
    finally:
        w.close()
