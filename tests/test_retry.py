"""失败待办的人工重试（方案 3.4 第 5 条）：真实 Recorder + SQLite + OcrWorker，替身 OCR 引擎。"""

import logging
import threading
from dataclasses import replace

from screenrecall import lifecycle, retry, search
from screenrecall.ocr import OcrBox, OcrWorker

from .conftest import make_image


class _Engine:
    version = "fake"

    def __init__(self):
        self.fail = True

    def recognize(self, image):
        if self.fail:
            raise RuntimeError("onnx session crashed")
        return [OcrBox("invoice 4127 retried", 0.9, [[0, 0], [30, 0], [30, 10], [0, 10]])]


def _setup(h, n=2):
    cfg = replace(h.cfg, ocr=replace(h.cfg.ocr, max_attempts=1))
    h.show(make_image(1))
    h.start()
    h.rec.sample(h.conn)
    for seed in range(2, n + 1):
        h.show(make_image(seed))
        h.rec.snapshot(h.conn, None)
    eng = _Engine()
    w = OcrWorker(cfg, eng, threading.Event(), threading.Event(), logging.getLogger("t"))
    while (job := w._claim(h.conn)) is not None:
        w._process(h.conn, job)
    return eng, w, [f["frame_id"] for f in h.frames()]


def test_retry_requeues_failed_ocr_and_then_succeeds(h):
    eng, w, fids = _setup(h)
    jobs = retry.failed_jobs(h.conn, stage="ocr")
    assert len(jobs) == 2 and all(j.retryable for j in jobs) and jobs[0].error_code == "ocr_error"
    assert retry.summary(h.conn)["ocr"]["retryable"] == 2
    assert w._claim(h.conn) is None                       # 自动重试已用尽：不会自己再做
    res = retry.retry_failed(h.conn, stage="ocr")
    assert res.requeued == {"ocr": 2} and not res.not_retryable
    st = h.conn.execute("SELECT status, attempts, not_before_ms, last_error FROM index_jobs WHERE stage = 'ocr'").fetchall()
    assert all(r[0] == "pending" and r[1] == 0 and r[2] == 0 and r[3].startswith("人工重试") for r in st)
    assert {r[0] for r in h.conn.execute("SELECT ocr_status FROM frames")} == {"pending"}
    eng.fail = False
    while (job := w._claim(h.conn)) is not None:
        w._process(h.conn, job)
    assert {r[0] for r in h.conn.execute("SELECT ocr_status FROM frames")} == {"done"}
    hits = search.search(h.conn, search.parse_query('"retried"'))
    assert {r.frame_id for r in hits.results} == set(fids)
    # OCR 成功后重新登记了文本向量（失败时只登记过全图）
    stages = {r[0] for r in h.conn.execute("SELECT stage FROM index_jobs WHERE frame_id = ? AND status = 'pending'",
                                           (fids[0],))}
    assert stages == {"text_embed", "image_embed"}
    assert retry.failed_jobs(h.conn) == []


def test_deleted_or_missing_frames_are_not_requeued(h):
    _, _, fids = _setup(h, n=3)
    h.conn.execute("UPDATE frames SET image_status = 'missing' WHERE frame_id = ?", (fids[0],))
    lifecycle.delete_frame(h.conn, h.cfg.data_dir, fids[1], confirmed_favorites=set())
    jobs = {j.frame_id: j for j in retry.failed_jobs(h.conn)}
    assert not jobs[fids[0]].retryable and jobs[fids[0]].why_not == "原图文件缺失"
    res = retry.retry_failed(h.conn)
    assert res.requeued == {"ocr": 1} and res.not_retryable == {"原图文件缺失": 1}
    assert h.conn.execute("SELECT status FROM index_jobs WHERE frame_id = ? AND stage = 'ocr'",
                          (fids[0],)).fetchone()[0] == "failed"


def test_retry_only_selected_jobs_and_stale_version(h):
    _, _, fids = _setup(h, n=3)
    jobs = retry.failed_jobs(h.conn, stage="ocr")
    h.conn.execute("UPDATE frames SET record_version = record_version + 1 WHERE frame_id = ?", (jobs[1].frame_id,))
    assert retry.failed_jobs(h.conn, stage="ocr")[1].why_not == "画面已更新，旧待办作废"
    res = retry.retry_failed(h.conn, job_ids=[jobs[0].job_id])
    assert res.requeued == {"ocr": 1}
    left = {j.job_id for j in retry.failed_jobs(h.conn)}
    assert jobs[0].job_id not in left and jobs[2].job_id in left


def test_cli_list_and_retry(h, capsys):
    _setup(h)
    assert retry.main(["--data-dir", str(h.cfg.data_dir), "--list"]) == 0
    assert "可重试 2 条" in capsys.readouterr().out
    assert retry.main(["--data-dir", str(h.cfg.data_dir)]) == 0
    assert "已改回待处理 2 条" in capsys.readouterr().out
    assert retry.failed_jobs(h.conn) == []
