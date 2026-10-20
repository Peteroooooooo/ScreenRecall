"""检索评测脚本（方案 6.2）：替身编码器上的流程测试。**这里的数字不是实验结果。**"""

import json

from screenrecall import evaluate, retrieval

from .conftest import make_image
from .test_retrieval import Pipeline


def _q(i, query, relevant, split="dev", scenario="chat", language="en", **kw):
    return evaluate.Query(id=f"q{i}", split=split, scenario=scenario, language=language, mode=kw.pop("mode", "text"),
                          query=query, relevant=relevant, **kw)


def test_metrics_definitions():
    assert evaluate.first_relevant_rank(["a", "b", "c"], {"c"}) == 3
    assert evaluate.score(3) == (1, 1 / 3)
    assert evaluate.score(6) == (0, 1 / 6)
    assert evaluate.score(11) == (0, 0.0) and evaluate.score(None) == (0, 0.0)


def test_five_experiments_share_candidates_and_keep_every_query(h):
    p = Pipeline(h)
    rest = p.add(make_image(1), ["colleague recommended restaurant downtown"])
    api = p.add(make_image(2), ["API documentation link"])
    code = p.add(make_image(3), ["error 0x80070005 access denied"])
    eng = p.engines()
    qs = [
        _q(1, "restaurant recommended", [rest]),
        _q(2, '"0x80070005"', [code], scenario="code"),               # 只有引号：所有实验同一路径
        _q(3, "bright", [api], split="blind", scenario="web", language="zh"),
        _q(4, "nothing matches zzz", [api], split="blind", scenario="web"),  # 无命中也保留、计 0
        _q(5, '"NOT-THERE" restaurant', [rest], split="blind"),       # 引号硬过滤为空
    ]
    rep = evaluate.evaluate(h.conn, qs, eng, split="all")
    assert [r["id"] for r in rep["per_query"]] == ["q1", "q2", "q3", "q4", "q5"]
    r1 = rep["per_query"][0]["results"]
    assert r1["exp1_lexical"]["first_relevant_rank"] == 1 and r1["exp4_rrf_text"]["hit5"] == 1
    r2 = rep["per_query"][1]["results"]
    assert {e: v["top10"] for e, v in r2.items()} == {e: [code] for e in evaluate.EXPERIMENTS}
    assert all(v["hit5"] == 0 for v in rep["per_query"][4]["results"].values())
    blind = rep["summary"]["blind"]
    assert blind["overall"]["exp1_lexical"]["n"] == 3  # 分母不因某方法失败而减少
    assert blind["language:zh"]["exp1_lexical"]["n"] == 1 and "scenario:web" in blind
    assert "exp5_rrf_three" in blind["overall"]
    assert "| overall |" in evaluate.to_markdown(rep)


def test_split_selection_and_missing_models_are_recorded(h):
    p = Pipeline(h)
    f = p.add(make_image(1), ["alpha beta"])
    qs = [_q(1, "alpha", [f]), _q(2, "beta", [f], split="blind")]
    rep = evaluate.evaluate(h.conn, qs, retrieval.Engines(), split="dev")
    assert [r["id"] for r in rep["per_query"]] == ["q1"]
    assert rep["per_query"][0]["errors"]  # 模型不可用写进逐条结果
    assert rep["per_query"][0]["results"]["exp1_lexical"]["hit5"] == 1
    assert rep["per_query"][0]["results"]["exp2_semantic"]["top10"] == []


def test_protocol_check_and_loader(tmp_path):
    items = []
    for sp in ("dev", "blind"):
        for sc in ("code", "chat", "web", "table"):
            for k in range(5):
                items.append({"id": f"{sp}-{sc}-{k}", "split": sp, "scenario": sc,
                              "language": "zh" if len(items) % 2 == 0 else "en", "query": "x", "relevant": ["f"]})
    path = tmp_path / "q.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    qs = evaluate.load_queries(path)
    notes = evaluate.protocol_check(qs)
    assert notes == []  # 40 条、每侧每场景 5 条、中英各 20
    bad = evaluate.protocol_check(qs[:-1] + [evaluate.Query("z", "blind", "table", "en", "text", "x", [])])
    assert any("没有相关帧" in n for n in bad)


def test_candidates_restrict_every_branch_before_topk(h):
    """仿界面帧不在正式名单里：任何实验（含纯引号路径）都不能把它排进结果，正式帧补上名次。"""
    p = Pipeline(h)
    mock = p.add(make_image(1), ["error 0x80070005 access denied mock terminal"])
    real = p.add(make_image(2), ["error 0x80070005 access denied"])
    eng = p.engines()
    only_real = frozenset({real})
    for q in (_q(1, "access denied error", [real], scenario="code"), _q(2, '"0x80070005"', [real], scenario="code")):
        full = evaluate.rankings(h.conn, q, eng)
        assert any(mock in v for k, v in full.items() if not k.startswith("_"))
        ranks = evaluate.rankings(h.conn, q, eng, candidates=only_real)
        for e in evaluate.EXPERIMENTS:
            assert mock not in ranks[e]
        assert ranks["exp1_lexical"][:1] == [real] and ranks["exp4_rrf_text"][:1] == [real]
    rep = evaluate.evaluate(h.conn, [_q(1, "access denied", [real])], eng, candidates=only_real)
    assert rep["frames_in_library"] == 1 and rep["candidates_restricted"]


def _qfile(tmp_path, status, relevant):
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"id": "d1", "split": "dev", "scenario": "code", "language": "en", "query": "x",
                                 "relevant": relevant, "review_status": status}]), encoding="utf-8")
    return path


def test_formal_run_refuses_drafts_missing_candidates_and_blind_rehearsal(tmp_path, capsys):
    q = _qfile(tmp_path, "draft", ["f1"])
    assert evaluate.load_queries(q)[0].review_status == "draft"
    assert evaluate.main(["--queries", str(q), "--split", "dev", "--out", str(tmp_path / "o"), "--check-only"]) == 0
    assert "未经人工复核" in capsys.readouterr().out
    # 正式：草稿、无候选名单 → 拒绝
    assert evaluate.main(["--queries", str(q), "--split", "dev", "--out", str(tmp_path / "o")]) == 1
    err = capsys.readouterr().err
    assert "review_status" in err and "--candidates" in err
    # 正式：相关帧不在名单里 → 拒绝
    cand = tmp_path / "cand.txt"
    cand.write_text("# x\nf2\n", encoding="utf-8")
    q2 = _qfile(tmp_path, "approved", ["f1"])
    assert evaluate.main(["--queries", str(q2), "--split", "dev", "--out", str(tmp_path / "o"),
                          "--candidates", str(cand)]) == 1
    assert "不在正式候选名单" in capsys.readouterr().err
    # 演练只能跑 dev
    assert evaluate.main(["--queries", str(q), "--split", "blind", "--out", str(tmp_path / "o"), "--rehearsal"]) == 1
    assert "只能跑 --split dev" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()
    rep = {"split": "dev", "run_kind": "rehearsal", "frames_in_library": 3, "summary": {}}
    assert "不是正式实验结果" in evaluate.to_markdown(rep)
