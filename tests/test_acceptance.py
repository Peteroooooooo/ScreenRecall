"""方案 8.3 关键场景验收矩阵中可在 Mac 上自动化的部分：A05–A10。

A01–A04 已由 test_recorder.py 覆盖（用例名带编号）。A11、A12 依赖收藏 / 删除 / 到期（"下一步"第 10 项，
尚未实现）；A13 是断网与 100%/125%/150% 缩放的实机核验，必须在 Windows 上做。

这些都是用假窗口、替身 OCR 与替身编码器的逻辑验证，**不等于 Windows 实机验收通过**。
"""

import logging
import threading
from dataclasses import replace

from screenrecall import db, retrieval, search
from screenrecall.ocr import OcrBox, OcrWorker, recover_interrupted

from .conftest import make_image
from .test_retrieval import Pipeline, box, q


def _filter_only(h, fid):
    """只覆盖 fid 那次出现的捕获时间范围。"""
    o = h.conn.execute("SELECT first_seen_ms, last_seen_ms FROM occurrences WHERE frame_id = ?", (fid,)).fetchone()
    return search.Filters(o[0], o[1] + 1)


def _ids(resp):
    return [r.frame_id for r in resp.results]


# ---------- A05 短词检索 ----------

def test_a05_short_terms_fall_back_to_scan_and_order_is_repeatable(h):
    p = Pipeline(h)
    f_cn = p.add(make_image(1), ["同事推荐的餐厅"])
    f_16 = p.add(make_image(2), ["参数 16 已保存"])
    f_mem = p.add(make_image(3), ["memory leak report"])
    f_both = p.add(make_image(4), ["memory 16 GB"])
    assert _ids(search.search(h.conn, q("餐厅"))) == [f_cn]  # 两字中文，不足 trigram 长度
    assert set(_ids(search.search(h.conn, q("16")))) == {f_16, f_both}
    mixed = search.search(h.conn, q("16 memory"))
    assert mixed.rank_basis == "terms"
    assert _ids(mixed)[0] == f_both  # 命中两个片段的排第一
    assert set(_ids(mixed)) == {f_16, f_mem, f_both}  # 短片段命中没有因缺少 FTS 分数而丢失
    assert all(_ids(search.search(h.conn, q("16 memory"))) == _ids(mixed) for _ in range(3))


def test_a05_percent_and_underscore_are_literal(h):
    p = Pipeline(h)
    f1 = p.add(make_image(1), ["rate 50%_x"])
    p.add(make_image(2), ["rate 50 abc"])
    assert _ids(search.search(h.conn, q("%_"))) == [f1]


# ---------- A06 引号约束与放宽 ----------

def test_a06_quotes_are_and_with_date_filter_and_case(h):
    p = Pipeline(h)
    f1 = p.add(make_image(1), ["Model-X battery 5000"])
    p.add(make_image(2), ["Model-X only"])
    f3 = p.add(make_image(3), ["Model-X battery 6000"])
    both = q('"Model-X" "battery"')
    assert set(_ids(search.search(h.conn, both))) == {f1, f3}
    assert _ids(search.search(h.conn, both, _filter_only(h, f1))) == [f1]  # 日期条件作用于出现记录
    assert _ids(search.search(h.conn, q('"model-x"'))) != []
    strict = search.search(h.conn, q('"model-x"'), case_sensitive=True)
    assert strict.results == [] and strict.can_relax


def test_a06_no_match_is_not_relaxed_automatically(h):
    p = Pipeline(h)
    p.add(make_image(1), ["Error Code:", "0x8007 0005"])
    resp = search.search(h.conn, q('"errorcode: 0x80070005"'))
    assert resp.results == [] and resp.can_relax and not resp.relaxed


def test_a06_relax_ignores_case_and_whitespace_across_ocr_lines(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["Error Code:", "0x8007 0005"])
    resp = search.search(h.conn, q('"errorcode: 0x80070005"'), relaxed=True)
    assert _ids(resp) == [fid] and resp.relaxed
    assert resp.results[0].matched_lines == [0, 1]  # 命中跨两行，两行都高亮


def test_a06_relax_keeps_other_filters_and_and_semantics(h):
    p = Pipeline(h)
    f1 = p.add(make_image(1), ["ERROR CODE 0x8007 0005"])
    f2 = p.add(make_image(2), ["error code 0x80070005 missing"])
    # 其他筛选保留：只看 f1 的时间
    assert _ids(search.search(h.conn, q('"errorcode0x80070005"'), _filter_only(h, f1), relaxed=True)) == [f1]
    # 放宽后所有原串仍须同时满足
    assert _ids(search.search(h.conn, q('"ERRORCODE" "missing"'), relaxed=True)) == [f2]
    # 从基础筛选重新扫描，不沿用严格匹配的空集合
    assert search.search(h.conn, q('"errorcode"')).results == []
    assert set(_ids(search.search(h.conn, q('"errorcode"'), relaxed=True))) == {f1, f2}


def test_a06_relax_in_text_mode_shows_rule_and_still_fuses(h):
    p = Pipeline(h)
    fid = p.add(make_image(1), ["Error Code:", "0x8007 0005", "access denied"])
    strict = retrieval.run(h.conn, q('"errorcode" access'), search.Filters(), mode=retrieval.TEXT, engines=p.engines())
    assert strict.results == [] and strict.can_relax
    relaxed = retrieval.run(h.conn, q('"errorcode" access'), search.Filters(), mode=retrieval.TEXT,
                            engines=p.engines(), relaxed=True)
    assert _ids(relaxed) == [fid] and relaxed.relaxed
    assert relaxed.notes[0] == search.RELAX_RULE
    assert relaxed.rank_basis == "rrf"


def test_a06_whitespace_only_quote_cannot_be_relaxed(h):
    p = Pipeline(h)
    p.add(make_image(1), ["a b"])
    resp = search.search(h.conn, q('" "'), relaxed=True)
    assert resp.results == [] and "为空" in resp.message


# ---------- A07 帧级排名 ----------

def test_a07_one_rank_per_frame_despite_many_blocks_and_occurrences(h):
    p = Pipeline(h)
    long_line = " ".join(["battery capacity"] * 40)  # 80 词，替身分词器下每行单独成块
    img = make_image(1)
    f_multi = p.add(img, [long_line, long_line + " x", long_line + " y"])
    assert h.conn.execute("SELECT COUNT(*) FROM frame_blocks WHERE frame_id = ?", (f_multi,)).fetchone()[0] >= 3
    others = [p.add(make_image(10 + i), [f"battery capacity note {i}"]) for i in range(3)]
    h.show(img)
    h.rec.snapshot(h.conn, None)  # 同一画面再出现一次
    resp = retrieval.run(h.conn, q("battery capacity"), search.Filters(), mode=retrieval.TEXT, engines=p.engines())
    ids = _ids(resp)
    assert len(ids) == len(set(ids)) == 4  # 每帧只占一位
    assert set(ids) == {f_multi, *others}
    multi = next(r for r in resp.results if r.frame_id == f_multi)
    assert multi.occurrences_in_filter == 2


# ---------- A08 OCR 失败 ----------

class _FlakyEngine:
    version = "fake"

    def __init__(self, texts):
        self.texts = texts

    def recognize(self, image):
        t = self.texts.pop(0)
        if t is None:
            raise RuntimeError("boom")
        return [box(x, 12 * i) for i, x in enumerate(t)]


def test_a08_ocr_failure_visible_does_not_block_next_and_differs_from_empty(h):
    cfg = replace(h.cfg, ocr=replace(h.cfg.ocr, max_attempts=1))
    h.show(make_image(1))
    h.start()
    h.rec.sample(h.conn)
    for seed in (2, 3):
        h.show(make_image(seed))
        h.rec.snapshot(h.conn, None)
    fids = [f["frame_id"] for f in h.frames()]
    w = OcrWorker(cfg, _FlakyEngine([None, ["after failure"], []]), threading.Event(), threading.Event(),
                  logging.getLogger("t"))
    while (job := w._claim(h.conn)) is not None:
        w._process(h.conn, job)
    status = dict(h.conn.execute("SELECT frame_id, ocr_status FROM frames").fetchall())
    assert [status[f] for f in fids] == ["failed", "done", "done"]
    err = h.conn.execute("SELECT last_error FROM index_jobs WHERE frame_id = ? AND stage = 'ocr'", (fids[0],)).fetchone()
    assert "boom" in err[0]  # 失败原因可见
    lines = dict(h.conn.execute("SELECT frame_id, line_count FROM frame_text").fetchall())
    assert fids[0] not in lines and lines[fids[2]] == 0  # 异常 ≠ 确实没有文字
    assert _ids(search.search(h.conn, q("after"))) == [fids[1]]
    # 其他有效分支不被抹去：OCR 失败的帧仍有全图任务
    jobs = {r[0] for r in h.conn.execute("SELECT stage FROM index_jobs WHERE frame_id = ?", (fids[0],))}
    assert "image_embed" in jobs


# ---------- A09 重启恢复 ----------

def test_a09_interrupted_jobs_resume_without_duplicates(h):
    p = Pipeline(h)
    done = p.add(make_image(1), ["already indexed text"])
    h.show(make_image(2))
    h.rec.snapshot(h.conn, None)

    class E:
        version = "fake"

        def recognize(self, image):
            return [OcrBox("resumed text", 0.9, [[0, 0], [10, 0], [10, 10], [0, 10]])]

    w = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    job = w._claim(h.conn)  # 后台在这里"停止"：任务停在 running
    assert h.conn.execute("SELECT status FROM index_jobs WHERE job_id = ?", (job.job_id,)).fetchone()[0] == "running"
    assert _ids(search.search(h.conn, q("already"))) == [done]  # 停机期间既有索引照常可查
    assert recover_interrupted(h.conn) == 1
    w2 = OcrWorker(h.cfg, E(), threading.Event(), threading.Event(), logging.getLogger("t"))
    while (j := w2._claim(h.conn)) is not None:
        w2._process(h.conn, j)
    while p.embed.run_once(h.conn):
        pass
    assert h.conn.execute("SELECT COUNT(*) FROM frame_text WHERE frame_id = ?", (job.frame_id,)).fetchone()[0] == 1
    assert _ids(search.search(h.conn, q("resumed"))) == [job.frame_id]
    dup = h.conn.execute(
        "SELECT COUNT(*) FROM (SELECT target_id, branch, COUNT(*) c FROM embeddings GROUP BY target_id, branch, "
        "model_id HAVING c > 1)"
    ).fetchone()[0]
    assert dup == 0


# ---------- A10 删除中推理 ----------

def test_a10_deleted_frame_disappears_from_lexical_semantic_and_image(h):
    p = Pipeline(h)
    keep = p.add(make_image(1), ["shared topic alpha"])
    gone = p.add(make_image(2), ["shared topic beta"])
    eng = p.engines()
    assert set(_ids(retrieval.run(h.conn, q("shared topic"), search.Filters(), mode=retrieval.TEXT, engines=eng))) == {
        keep, gone}
    with h.conn:  # 逻辑失效 + 版本与索引修订号提升（完整删除流程见"下一步"第 10 项）
        h.conn.execute("UPDATE frames SET image_status = 'deleted', record_version = record_version + 1 "
                       "WHERE frame_id = ?", (gone,))
        db.bump_revision(h.conn)
    assert _ids(search.search(h.conn, q("shared"))) == [keep]
    assert _ids(retrieval.run(h.conn, q("shared topic"), search.Filters(), mode=retrieval.TEXT, engines=eng)) == [keep]
    img = retrieval.run(h.conn, q("bright"), search.Filters(), mode=retrieval.IMAGE, engines=eng)
    assert gone not in _ids(img)
