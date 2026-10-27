"""v2 只读查询：搜索（含向量分支 RRF 融合）、时间线、片段阅读顺序、帧高亮、当天概览、控制状态。"""

import json
from datetime import datetime

import numpy as np
import pytest

from screenrecall.store.queries import RRF_K, Queries
from screenrecall.store.vectors import VectorStore
from screenrecall.store.writer import LineIn, Writer

DIM = 8


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    w = Writer(tmp_path)
    q = Queries(tmp_path, clock=Clock(10 ** 7))
    yield w, q, tmp_path
    q.close()
    w.close()


def put(w, app, title, t, texts, block=None, y0=10, step=30):
    sid = w.segment_for(app, title, t).result(5)
    b = block or w.block_new(sid, t).result(5)
    ids = w.record_lines(b, t, (y0 - 5, y0 + step * len(texts) + 50),
                         [LineIn(x, 10, y0 + i * step, 600, y0 + i * step + 20) for i, x in enumerate(texts)]).result(5)
    return sid, b, ids


def test_search_lexical_only_is_json(env):
    w, q, _ = env
    sid, _, ids = put(w, "WeChat.exe", "群", 1000, ["组会改到周五"])
    res = q.search("组会")
    json.dumps(res, ensure_ascii=False)
    assert res["groups"][0]["segment_id"] == sid and res["groups"][0]["sources"] == ["lexical"]
    assert res["groups"][0]["lines"][0]["line_id"] == ids[0]


def test_search_fuses_vector_branch_with_rrf(env):
    w, q, tmp = env
    s1, _, a = put(w, "a.exe", "文档一", 1000, ["深度学习入门"])
    s2, _, b = put(w, "b.exe", "文档二", 2000, ["神经网络训练技巧"])  # 只有语义相关，没有字面命中
    s3, _, c = put(w, "c.exe", "文档三", 3000, ["深度学习的历史"])
    vs = VectorStore(tmp / "vectors" / "text", DIM)
    e = np.eye(DIM, dtype=np.float32)
    vs.append([a[0], b[0], c[0]], np.stack([e[0], e[0] * 0.9 + e[1] * 0.1, e[2]]), [1000, 2000, 3000], [s1, s2, s3])
    q.text_vectors = VectorStore(tmp / "vectors" / "text", DIM, readonly=True)
    enc = lambda text: e[0]  # noqa: E731
    res = q.search("深度学习", encoder=enc)
    order = [g["segment_id"] for g in res["groups"]]
    # 词法：s3(1), s1(2)；向量：s1(1), s2(2), s3(3)
    assert order == [s1, s3, s2]
    g1 = res["groups"][0]
    assert g1["sources"] == ["lexical", "vector"]
    assert g1["score"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    g2 = res["groups"][2]
    assert g2["sources"] == ["vector"] and g2["lines"][0]["line_id"] == b[0]
    assert res["mode"].endswith("+vector")
    # 应用筛选同样作用于向量分支
    res = q.search("深度学习", app="b.exe", encoder=enc)
    assert [g["segment_id"] for g in res["groups"]] == [s2]
    res = q.search("深度学习", exclude_apps=["a.exe"], encoder=enc)
    assert s1 not in [g["segment_id"] for g in res["groups"]]


def test_timeline_orders_and_hides(env):
    w, q, _ = env
    w.activity_start(1000, hwnd=1, pid=1, process="code.exe", title="main.py")
    w.gap(2000, 3000, "locked")
    w.activity_start(3000, hwnd=2, pid=2, process="KeePass.exe", title="密码库")
    w.activity_start(4000, hwnd=3, pid=3, process="chrome.exe", title="网银 - 某银行", excluded=True)
    sid = w.segment_for("code.exe", "main.py", 1000).result(5)
    w.piece_put(1000, 2000, app="code.exe", title="main.py", segment_ids=[sid], task_label="写代码").result(5)
    tl = q.timeline(0, 10_000)
    kinds = [(x["kind"], x["start_ms"]) for x in tl["items"]]
    assert kinds == [("activity", 1000), ("piece", 1000), ("gap", 2000), ("activity", 3000), ("activity", 4000)]
    assert tl["items"][-1]["title"] is None and tl["items"][-1]["excluded"] and tl["items"][-1]["open"]
    hidden = q.timeline(0, 10_000, exclude_apps=["keepass.exe"], exclude_title_keywords=["银行"])
    assert [x.get("process") for x in hidden["items"] if x["kind"] == "activity"] == ["code.exe"]
    only = q.timeline(0, 10_000, app="code.exe")
    assert {x["kind"] for x in only["items"]} == {"activity", "piece", "gap"}


def test_piece_reading_order_with_merged_blocks_skims_gaps(env):
    w, q, _ = env
    sid = w.segment_for("chrome.exe", "长文章", 1000).result(5)
    b1 = w.block_new(sid, 1000, anchor="top").result(5)
    b2 = w.block_new(sid, 2000).result(5)
    put(w, "chrome.exe", "长文章", 1000, ["第一段", "第二段"], block=b1, y0=0)
    # b2 是后来从中间开始看的另一把标尺，空闲时发现它接在 b1 的 600 px 处
    put(w, "chrome.exe", "长文章", 2000, ["第四段", "第五段"], block=b2, y0=40)
    put(w, "chrome.exe", "长文章", 2500, ["第三段"], block=b2, y0=-20)
    w.block_merge(b2, b1, 600).result(5)
    w.skim(sid, b2, 2100, 2200, 100, 400)
    w.gap(2300, 2400, "minimised")
    other = w.segment_for("chrome.exe", "别的页", 1000).result(5)
    put(w, "chrome.exe", "别的页", 1500, ["别的页内容"])
    p = w.piece_put(900, 3000, app="chrome.exe", title="长文章", segment_ids=[sid]).result(5)
    fid = w.save_frame(np.zeros((10, 10, 3), np.uint8), captured_ms=2000, segment_id=sid, block_id=b2, pos_y=0)
    w.summary_put("piece", str(p), "读了一篇长文章")
    w.flush()
    d = q.piece(p)
    json.dumps(d, ensure_ascii=False)
    assert len(d["segments"]) == 1 and d["segments"][0]["segment_id"] == sid
    blocks = d["segments"][0]["blocks"]
    assert len(blocks) == 1 and blocks[0]["block_id"] == b1 and blocks[0]["merged"] == [b2]
    assert [x["text"] for x in blocks[0]["lines"]] == ["第一段", "第二段", "第三段", "第四段", "第五段"]
    assert blocks[0]["lines"][3]["y0"] == 640
    assert blocks[0]["skims"] == [{"start_ms": 2100, "end_ms": 2200, "y_from": 700, "y_to": 1000}]
    assert [g["reason"] for g in d["gaps"]] == ["minimised"]
    assert [f["frame_id"] for f in d["frames"]] == [fid]
    assert d["summary"]["text"] == "读了一篇长文章"
    assert q.piece(99999) is None
    assert other


def test_frame_with_highlight(env):
    w, q, tmp = env
    sid, b, ids = put(w, "a.exe", "t", 1000, ["第一行", "第二行"])
    fid = w.save_frame(np.zeros((100, 200, 3), np.uint8), captured_ms=1000, segment_id=sid, block_id=b, pos_y=0,
                       app="a.exe", title="t", line_ids=ids, line_rects=[(0, 10, 100, 30), (0, 40, 100, 60)])
    w.flush()
    f = q.frame(fid, highlight=ids[1])
    assert f["status"] == "ok" and f["width"] == 200 and f["path"].startswith(str(tmp))
    assert [(x["text"], x["highlight"]) for x in f["lines"]] == [("第一行", False), ("第二行", True)]
    assert q.frame("nope") is None


def test_summary_and_day_overview(env):
    w, q, _ = env
    day = datetime(2026, 10, 27)
    t0 = int(day.timestamp() * 1000)
    q.clock = Clock(t0 + 5 * 3600_000)
    w.activity_start(t0 - 3600_000, hwnd=1, pid=1, process="code.exe", title="x")  # 前一天开始，跨午夜
    w.activity_start(t0 + 3600_000, hwnd=2, pid=2, process="chrome.exe", title="y")
    w.activity_start(t0 + 2 * 3600_000, hwnd=3, pid=3, process="secret.exe", title="z", excluded=True)
    w.gap(t0 + 3 * 3600_000, t0 + 3 * 3600_000 + 60_000, "locked")
    put(w, "code.exe", "x", t0 + 1000, ["今天的一行", "另一行"])
    put(w, "code.exe", "x", t0 - 86_400_000, ["昨天的一行"])
    w.summary_put("day", "2026-10-27", "今天写了代码", start_ms=t0, end_ms=t0 + 86_400_000).result(5)
    d = q.day_overview(day.date())
    apps = {a["process"]: a["ms"] for a in d["apps"]}
    assert apps == {"code.exe": 3600_000, "chrome.exe": 3600_000}  # 排除的窗口不计时长
    assert d["gaps"] == {"locked": {"count": 1, "ms": 60_000}}
    assert d["lines_new"] == 2 and d["summary"]["text"] == "今天写了代码"
    assert q.summary("day", "2026-10-28") is None
    json.dumps(d, ensure_ascii=False)


def test_control_and_apps(env):
    w, q, _ = env
    w.control_set(hidden_from_agents={"apps": ["KeePass.exe"], "title_keywords": []}, agent_enabled=True).result(5)
    q.clock = Clock(10 ** 13)
    w.heartbeat(recorder_state="recording").result(5)
    c = q.control()
    assert c["hidden_from_agents"]["apps"] == ["KeePass.exe"] and c["agent_enabled"] is True
    assert c["recorder_alive"] is False  # 心跳用的是写线程的真实时钟，与这里的假时钟差很远
    put(w, "a.exe", "t1", 1000, ["x"])
    put(w, "b.exe", "t2", 2000, ["y"])
    assert [a["app"] for a in q.apps()] == ["b.exe", "a.exe"]


def test_queries_work_before_recorder_ever_ran(tmp_path):
    q = Queries(tmp_path)
    assert q.search("任何")["total"] == 0
    assert q.timeline(0, 1)["items"] == []
    assert q.control()["desired_state"] == "paused"
    q.close()
