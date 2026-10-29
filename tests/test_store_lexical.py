"""词法检索的查询规划（store.fts.lexical）：切词、二元组 / 三元组路由、SQL 片段；匹配语义在 v3 库上验证。"""

import pytest

from screenrecall.store.fts import docs, lexical
from screenrecall.v3 import config
from screenrecall.v3.store import Queries, Store
from screenrecall.v3.types import SourceLabel, TextLine


def test_norm_and_docs():
    assert docs.norm("Ｈｅｌｌｏ  World\n微 信") == "helloworld微信"
    assert docs.norm("STRASSE") == docs.norm("straße")
    assert docs.bi_doc("微信号 abc Def-9", docs.norm("微信号 abc Def-9"), "") == "微信 信号 号 abc def 9"
    assert docs.run_tokens("会") == ["会"]


def test_plan_routing():
    p = lexical.plan('微信 组会纪要 "Hello World" ab 微a ##')
    kinds = {t.text: t.kind for t in p.terms}
    assert kinds == {"微信": "short", "组会纪要": "tri", "helloworld": "tri", "ab": "short", "微a": "short"}
    assert p.dropped == ("##",)
    short = {t.text: t.fts for t in p.terms if t.kind == "short"}
    # 混合短词只用中日韩词元出候选，由 instr 精确核验（"微a" 也要能搜到 "微ab"、"x微a"）
    assert short["微信"] == ('"微信"',) and short["ab"] == ('"ab"*',) and short["微a"] == ('"微"*',)
    assert {t.text: t.word_prefix for t in p.terms if t.kind == "short"} == {"微信": False, "ab": True, "微a": False}
    assert lexical.plan("会").terms[0].fts == ('"会"*',)
    assert lexical.plan('"未闭合').error
    assert lexical.split_query('a “b c” d') == (["b c", "a", "d"], None)
    assert [t.text for t in lexical.plan("微信 微信 WeChat wechat").terms] == ["微信", "wechat"]


def test_long_term_trigram_sampling():
    t = lexical.plan("x" * 3 + "abcdefghijklmnopqrstuvwxyz0123456789").terms[0]
    assert len(t.fts) <= lexical.MAX_TRIGRAMS_PER_TERM


def test_sql_fragments():
    p = lexical.plan("组会纪要 ai")
    params = {}
    assert "lines_tri" in lexical.candidates_sql(p.terms, params) and params["lx_m"].startswith('"组会纪"')
    assert lexical.verify_sql(p.terms, params, "x.norm") == "instr(x.norm, :lx_t0) > 0 AND instr(x.norm, :lx_t1) > 0"
    assert "lines_bi" in lexical.word_prefix_sql(p.terms, params) and params["lx_mw"] == '"ai"*'
    short = lexical.plan("ai 会")
    params = {}
    assert "lines_bi" in lexical.candidates_sql(short.terms, params)
    assert lexical.word_prefix_sql(short.terms, params) == ""  # 候选本身已来自 lines_bi


@pytest.fixture
def q(tmp_path):
    cfg = config.build({}, data_dir=tmp_path / "d", home=tmp_path)
    st = Store(cfg, clock=lambda: 1000)
    sid, _ = st.upsert_source(SourceLabel("url", "https://a.example/", "msedge.exe", "t", url="https://a.example/"),
                              1000)
    texts = ["打开微app设置", "微 b app", "项目周报 第三周", "项目计划", "周报模板", "第21号", "rain"]
    st.record_lines(sid, [TextLine(t, (0, i * 30, 100, 20), "uia") for i, t in enumerate(texts)], 1000)
    qs = Queries(cfg, clock=lambda: 2000)
    yield qs
    qs.close()
    st.close()


def found(q, text):
    return sorted(h["text"] for r in q.search(text)["results"] for h in r["hits"])


def test_matching_semantics_on_v3(q):
    assert found(q, "微a") == ["打开微app设置"]
    assert found(q, "微b") == ["微 b app"]
    assert found(q, "项目 周报") == ["项目周报 第三周"]
    assert found(q, "项目") == sorted(["项目计划", "项目周报 第三周"])
    assert found(q, "1号") == ["第21号"]  # 混合短词是普通子串
    assert found(q, "ra") == ["rain"] and found(q, "ai") == []  # 纯字母数字短词 = 以它开头的词
    assert found(q, '"项目周报 第三周"') == ["项目周报 第三周"]
