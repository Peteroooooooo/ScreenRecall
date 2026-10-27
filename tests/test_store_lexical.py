"""v2 词法检索：路由（二元组 / 三元组）、跨行、AND、按最近看到排序、分组计数、筛选与常见词模式。"""

import pytest

from screenrecall.store import schema
from screenrecall.store.fts import docs, lexical
from screenrecall.store.writer import LineIn, Writer


@pytest.fixture
def env(tmp_path):
    w = Writer(tmp_path)
    c = schema.connect_ro(w.db_path)
    yield w, c
    c.close()
    w.close()


def put(w, app, title, t, texts, *, y0=10, step=30, block=None):
    sid = w.segment_for(app, title, t).result(5)
    b = block or w.block_new(sid, t).result(5)
    ids = w.record_lines(b, t, (0, y0 + step * len(texts) + 100),
                         [LineIn(x, 10, y0 + i * step, 600, y0 + i * step + 20) for i, x in enumerate(texts)]).result(5)
    return sid, b, ids


def S(c, q, **kw):
    filt = {k: kw.pop(k) for k in list(kw) if k in ("time_from", "time_to", "app", "task", "exclude_apps",
                                                    "exclude_title_keywords")}
    return lexical.search(c, q, filters=lexical.Filters(**filt), **kw)


def all_line_ids(res):
    return sorted(l["line_id"] for g in res.groups for l in g.lines)


# ---------------- 规范化与文档 ----------------

def test_norm_and_docs():
    assert docs.norm("Ｈｅｌｌｏ  World\n微 信") == "helloworld微信"
    assert docs.norm("STRASSE") == docs.norm("straße")
    assert docs.bi_doc("微信号 abc Def-9", docs.norm("微信号 abc Def-9"), "") == "微信 信号 号 abc def 9"
    assert docs.run_tokens("会") == ["会"]
    assert docs.tail_of("x" * 40) == "x" * docs.TAIL_CHARS


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


def test_long_term_trigram_sampling():
    t = lexical.plan("x" * 3 + "abcdefghijklmnopqrstuvwxyz0123456789").terms[0]
    assert len(t.fts) <= lexical.MAX_TRIGRAMS_PER_TERM


# ---------------- 匹配 ----------------

def test_cjk_one_two_and_long_queries(env):
    w, c = env
    _, _, ids = put(w, "WeChat.exe", "群聊", 1000, ["明天下午组会在 3 楼", "微信支付成功", "会"])
    assert all_line_ids(S(c, "组会")) == [ids[0]]
    assert all_line_ids(S(c, "会")) == sorted([ids[0], ids[2]])
    assert all_line_ids(S(c, "付")) == [ids[1]]
    assert all_line_ids(S(c, "下午组会")) == [ids[0]]
    assert all_line_ids(S(c, "组会在3楼")) == [ids[0]]  # 空白不敏感
    assert all_line_ids(S(c, "支付失败")) == []
    assert S(c, "组会").mode == "exact"


def test_english_case_fullwidth_and_word_prefix(env):
    w, c = env
    _, _, ids = put(w, "chrome.exe", "doc", 1000, ["Machine Learning Basics", "ＡＩ assistant", "rain gauge"])
    assert all_line_ids(S(c, "machine learning")) == [ids[0]]
    assert all_line_ids(S(c, '"MACHINE LEARNING"')) == [ids[0]]
    assert all_line_ids(S(c, "ai")) == [ids[1]]  # 两个字母 = 词开头：AI 命中，rain 不命中
    assert all_line_ids(S(c, "ain")) == [ids[2]]  # 三个字母起是子串
    assert all_line_ids(S(c, "learning gauge")) == []  # 多个词须在同一行


def test_mixed_short_terms_verified_exactly(env):
    w, c = env
    _, _, ids = put(w, "x.exe", "t", 1000, ["打开微app设置", "微 b app"])
    assert all_line_ids(S(c, "微a")) == [ids[0]]
    assert all_line_ids(S(c, "微b")) == [ids[1]]


def test_phrase_wrapping_onto_next_line(env):
    w, c = env
    _, _, ids = put(w, "WINWORD.EXE", "报告", 1000,
                    ["本研究采用了机器", "学习方法进行分析", "the quick brown fox jum", "ps over the lazy dog"])
    assert all_line_ids(S(c, "机器学习")) == [ids[0]]
    assert all_line_ids(S(c, "fox jumps")) == [ids[2]]
    # 完全落在下一行里的词只算下一行，不重复算上一行
    assert all_line_ids(S(c, "学习方法")) == [ids[1]]
    assert all_line_ids(S(c, "lazy")) == [ids[3]]
    assert S(c, "学习方法").total == 1
    # 二元组路由同样能跨行
    assert all_line_ids(S(c, "器学")) == [ids[0]]


def test_terms_and_quotes(env):
    w, c = env
    _, _, ids = put(w, "a.exe", "t", 1000, ["项目周报 第三周", "项目计划", "周报模板"])
    assert all_line_ids(S(c, "项目 周报")) == [ids[0]]
    assert all_line_ids(S(c, "项目")) == sorted(ids[:2])


def test_order_is_last_seen_not_rowid(env):
    w, c = env
    s_old, b_old, old = put(w, "a.exe", "older", 1000, ["共同关键词 A"])
    s_new, _, new = put(w, "b.exe", "newer", 5000, ["共同关键词 B"])
    # 空闲 OCR 晚插入一条更早时间的行（rowid 更大）
    s_late, _, late = put(w, "c.exe", "late-ocr", 500, ["共同关键词 C"])
    res = S(c, "共同关键词")
    assert [g.segment_id for g in res.groups] == [s_new, s_old, s_late]
    # 旧行又被看到：排到最前
    w.record_lines(b_old, 9000, (0, 200), [LineIn("共同关键词 A", 10, 10, 600, 30)]).result(5)
    assert [g.segment_id for g in S(c, "共同关键词").groups][0] == s_old


def test_grouping_counts_limits(env):
    w, c = env
    s1, _, _ = put(w, "a.exe", "one", 1000, [f"报销单 第{i}张" for i in range(7)])
    s2, _, _ = put(w, "b.exe", "two", 2000, ["报销单 汇总"])
    s3, _, _ = put(w, "c.exe", "three", 500, ["报销单 草稿"])
    res = S(c, "报销单", limit=2, per_group=3)
    assert res.total == 9 and res.total_groups == 3 and not res.total_is_lower_bound
    assert [g.segment_id for g in res.groups] == [s2, s1]
    assert res.groups[1].hits == 7 and len(res.groups[1].lines) == 3
    d = res.to_dict()
    assert d["groups"][0]["app"] == "b.exe" and d["groups"][0]["lines"][0]["text"] == "报销单 汇总"


def test_filters(env):
    w, c = env
    s1, _, _ = put(w, "WeChat.exe", "工作群", 1000, ["报价单已发"])
    s2, _, _ = put(w, "chrome.exe", "网银 - 招商银行", 5000, ["报价单下载"])
    s3, _, _ = put(w, "chrome.exe", "邮件", 9000, ["报价单回复"])
    p = w.piece_put(4000, 6000, app="chrome.exe", title="网银", segment_ids=[s2], task_label="报价").result(5)
    assert p
    seg = lambda **kw: [g.segment_id for g in S(c, "报价单", **kw).groups]  # noqa: E731
    assert seg(app="wechat.exe") == [s1]
    assert seg(time_from=4000, time_to=6000) == [s2]
    assert seg(time_from=8000) == [s3]
    assert seg(task="报价") == [s2]
    assert seg(exclude_apps=["CHROME.exe"]) == [s1]
    assert seg(exclude_title_keywords=["银行"]) == [s3, s1]


def test_empty_and_error_queries(env):
    _, c = env
    assert S(c, "   ").mode == "empty"
    r = S(c, '"abc')
    assert r.mode == "empty" and "引号" in r.note
    r = S(c, "## 测试")
    assert "##" in (r.note or "")


# ---------------- 常见词：最近优先模式 ----------------

def test_recent_mode_matches_exact_top_groups(env):
    w, c = env
    segs = []
    for k in range(30):
        s, _, _ = put(w, f"app{k % 3}.exe", f"title{k}", 1000 + k * 100, [f"the common word {k}-{i}" for i in range(5)])
        segs.append(s)
    exact = S(c, "common", limit=5, mode="exact")
    recent = S(c, "common", limit=5, mode="recent", scan_hits=40, scan_rows=100)
    assert exact.mode == "exact" and recent.mode == "recent"
    assert [g.segment_id for g in recent.groups] == [g.segment_id for g in exact.groups] == segs[::-1][:5]
    assert [l["line_id"] for l in recent.groups[0].lines] == [l["line_id"] for l in exact.groups[0].lines]
    assert recent.total_is_lower_bound and recent.total == 40 and exact.total == 150


def test_recent_mode_falls_back_when_filter_is_narrow(env):
    w, c = env
    rare, _, _ = put(w, "rare.exe", "old", 10, ["the common word rare"])
    for k in range(20):
        put(w, "busy.exe", f"t{k}", 1000 + k, [f"the common word {k}-{i}" for i in range(5)])
    res = S(c, "common", app="rare.exe", mode="recent", scan_hits=40, scan_rows=30)
    assert res.mode == "segment" and [g.segment_id for g in res.groups] == [rare]
    res = S(c, "common", time_from=0, mode="recent", limit=50, scan_hits=4000, scan_rows=30)
    assert res.mode == "exact" and res.total == 101


def test_all_modes_agree(env):
    w, c = env
    for k in range(12):
        put(w, f"app{k % 2}.exe", f"t{k}", 1000 + k * 37 % 11 * 100, [f"报告 第{k}-{i} 段" for i in range(4)])
    for kw in ({}, {"app": "app1.exe"}, {"time_from": 1300, "app": "app0.exe"}):
        res = {m: S(c, "报告", mode=m, limit=4, **kw) for m in ("exact", "segment", "recent")}
        ref = [(g.segment_id, [l["line_id"] for l in g.lines]) for g in res["exact"].groups]
        for m, r in res.items():
            assert [(g.segment_id, [l["line_id"] for l in g.lines]) for g in r.groups] == ref, (m, kw)


def test_planner_picks_by_cost(env):
    w, c = env
    for k in range(40):
        put(w, "busy.exe" if k else "rare.exe", f"t{k}", 1000 + k, [f"the word {k}-{i}" for i in range(10)])
    put(w, "x.exe", "y", 5000, ["rarexyz term"])
    assert S(c, "rarexyz").mode == "exact"
    assert S(c, "the", app="rare.exe").mode in ("segment", "exact")


def test_short_word_prefix_same_in_every_plan(env):
    w, c = env
    _, _, ids = put(w, "a.exe", "t", 1000, ["rain gauge machine", "AI machine", "again"])
    for m in ("exact", "segment", "recent"):
        kw = {"app": "a.exe"} if m == "segment" else {}
        assert all_line_ids(S(c, "ai", mode=m, **kw)) == [ids[1]], m
        assert all_line_ids(S(c, "ai machine", mode=m, **kw)) == [ids[1]], m
