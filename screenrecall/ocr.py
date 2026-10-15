"""OCR：用 RapidOCR 识别已保存的原分辨率图片，写回原文、行坐标与全文索引（方案 5.1、4.3）。

- 按捕获顺序领取待办；推理在事务外执行；
- 写回前在短事务里核对：任务仍归本执行者、目标版本未变、图片仍可用，否则丢弃结果，
  防止"删掉的截图又被 OCR 写回来"；
- 同一帧重复写回会先清掉旧结果，保证幂等；
- 暂时性错误有界重试并退避，超过上限标为失败，不阻塞后续帧。
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

from PIL import Image

from . import db, storage, textutil, vectors
from .config import Config, OcrConfig
from .timeutil import utc_ms


@dataclass(frozen=True)
class OcrBox:
    text: str
    confidence: float
    quad: list[list[float]]  # 4 个 [x, y]，原图物理像素


def ocr_version() -> str:
    try:
        v = importlib.metadata.version("rapidocr-onnxruntime")
    except importlib.metadata.PackageNotFoundError:
        v = "unknown"
    return f"rapidocr-onnxruntime {v} / PP-OCRv4"


class OcrEngine:
    def __init__(self, cfg: OcrConfig):
        from rapidocr_onnxruntime import RapidOCR

        self._engine = RapidOCR(
            intra_op_num_threads=cfg.intra_op_threads,
            inter_op_num_threads=cfg.inter_op_threads,
            max_side_len=cfg.max_side_len,
            print_verbose=False,
        )
        self.version = ocr_version()

    def recognize(self, image: Image.Image) -> list[OcrBox]:
        if image.mode != "RGB":
            image = image.convert("RGB")
        result, _elapse = self._engine(image)
        if not result:
            return []
        return [
            OcrBox(str(item[1]), float(item[2]), [[float(x), float(y)] for x, y in item[0]])
            for item in result
        ]


@dataclass(frozen=True)
class _Job:
    job_id: int
    frame_id: str
    image_path: str
    target_version: int
    attempt: int
    token: str


_CLAIM_SQL = """
SELECT j.job_id, j.frame_id, j.target_version, j.attempts,
       f.image_path, f.image_status, f.record_version
FROM index_jobs j JOIN frames f ON f.frame_id = j.frame_id
WHERE j.stage = 'ocr' AND j.status = 'pending' AND j.not_before_ms <= ?
ORDER BY j.job_id
LIMIT 1
"""

_STILL_VALID_SQL = """
SELECT 1 FROM index_jobs j JOIN frames f ON f.frame_id = j.frame_id
WHERE j.job_id = ? AND j.status = 'running' AND j.worker_token = ?
  AND f.record_version = ? AND f.image_status = 'ok'
"""


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """启动时把上次中断时仍在执行的任务改回待办。"""
    with db.write_txn(conn):
        cur = conn.execute(
            "UPDATE index_jobs SET status = 'pending', worker_token = NULL, updated_at_ms = ? WHERE status = 'running'",
            (utc_ms(),),
        )
        return cur.rowcount


class OcrWorker:
    def __init__(self, cfg: Config, engine: OcrEngine, wake: threading.Event, stop: threading.Event, log: logging.Logger):
        self.cfg = cfg
        self.engine = engine
        self.wake = wake
        self.stop = stop
        self.log = log

    def run(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            while not self.stop.is_set():
                try:
                    job = self._claim(conn)
                    if job is None:
                        self.wake.wait(1.0)
                        self.wake.clear()
                        continue
                    self._process(conn, job)
                except Exception:
                    self.log.exception("OCR 循环出错")
                    self.stop.wait(1.0)
        finally:
            conn.close()

    def _claim(self, conn: sqlite3.Connection) -> _Job | None:
        while True:
            now = utc_ms()
            with db.write_txn(conn):
                row = conn.execute(_CLAIM_SQL, (now,)).fetchone()
                if row is None:
                    return None
                if row["image_status"] != "ok" or row["record_version"] != row["target_version"]:
                    conn.execute(
                        "UPDATE index_jobs SET status = 'cancelled', worker_token = NULL, updated_at_ms = ?, "
                        "last_error = '目标已失效（图片不可用或版本已变）' WHERE job_id = ?",
                        (now, row["job_id"]),
                    )
                    continue
                token = uuid.uuid4().hex
                conn.execute(
                    "UPDATE index_jobs SET status = 'running', worker_token = ?, attempts = attempts + 1, "
                    "updated_at_ms = ? WHERE job_id = ?",
                    (token, now, row["job_id"]),
                )
                return _Job(
                    row["job_id"], row["frame_id"], row["image_path"], row["target_version"],
                    row["attempts"] + 1, token,
                )

    def _process(self, conn: sqlite3.Connection, job: _Job) -> None:
        t0 = time.perf_counter()
        path = storage.resolve(self.cfg.data_dir, job.image_path)
        try:
            with Image.open(path) as im:
                im.load()
                image = im.convert("RGB")
        except FileNotFoundError:
            self._fail(conn, job, "image_missing", "图片文件缺失", retryable=False, image_missing=True)
            return
        except OSError as exc:
            self._fail(conn, job, "image_decode", str(exc), retryable=True)
            return
        try:
            boxes = self.engine.recognize(image)
        except Exception as exc:
            self._fail(conn, job, "ocr_error", f"{type(exc).__name__}: {exc}", retryable=True)
            return
        ocr_ms = int((time.perf_counter() - t0) * 1000)

        full, spans = textutil.join_lines([b.text for b in boxes])
        folded = textutil.fold(full)
        rows = []
        for i, (b, (s, e)) in enumerate(zip(boxes, spans)):
            xs = [p[0] for p in b.quad]
            ys = [p[1] for p in b.quad]
            rows.append((job.frame_id, i, b.text, b.confidence, json.dumps(b.quad), min(xs), min(ys), max(xs), max(ys), s, e))

        now = utc_ms()
        with db.write_txn(conn):
            if conn.execute(_STILL_VALID_SQL, (job.job_id, job.token, job.target_version)).fetchone() is None:
                conn.execute(
                    "UPDATE index_jobs SET status = 'cancelled', worker_token = NULL, updated_at_ms = ?, "
                    "last_error = '写回前目标已失效，结果已丢弃' WHERE job_id = ? AND worker_token = ?",
                    (now, job.job_id, job.token),
                )
                stale = True
            else:
                stale = False
                old = conn.execute("SELECT text_id FROM frame_text WHERE frame_id = ?", (job.frame_id,)).fetchone()
                if old is not None:
                    conn.execute("DELETE FROM ocr_fts WHERE rowid = ?", (old[0],))
                    conn.execute("DELETE FROM frame_text WHERE text_id = ?", (old[0],))
                conn.execute("DELETE FROM ocr_lines WHERE frame_id = ?", (job.frame_id,))
                cur = conn.execute(
                    "INSERT INTO frame_text (frame_id, text, text_fold, ocr_version, line_count) VALUES (?, ?, ?, ?, ?)",
                    (job.frame_id, full, folded, self.engine.version, len(boxes)),
                )
                conn.execute("INSERT INTO ocr_fts (rowid, text_fold) VALUES (?, ?)", (cur.lastrowid, folded))
                conn.executemany(
                    "INSERT INTO ocr_lines (frame_id, line_no, text, confidence, quad, x0, y0, x1, y1, "
                    "char_start, char_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.execute("UPDATE frames SET ocr_status = 'done' WHERE frame_id = ?", (job.frame_id,))
                # 文字可搜后再补向量：有文字登记文本+全图，无文字只登记全图（方案 2.1、3.2）
                vectors.register_embed_jobs(conn, job.frame_id, job.target_version, has_text=bool(full.strip()))
                conn.execute(
                    "UPDATE index_jobs SET status = 'done', worker_token = NULL, updated_at_ms = ?, last_error = NULL "
                    "WHERE job_id = ?",
                    (now, job.job_id),
                )
                db.bump_revision(conn)
        if stale:
            self.log.info("ocr stale frame=%s：写回前目标已失效，结果丢弃", job.frame_id)
        else:
            self.log.info(
                "ocr done frame=%s lines=%d ocr_ms=%d total_ms=%d",
                job.frame_id, len(boxes), ocr_ms, int((time.perf_counter() - t0) * 1000),
            )

    def _fail(self, conn, job: _Job, code: str, msg: str, *, retryable: bool, image_missing: bool = False) -> None:
        now = utc_ms()
        error = f"{code}: {msg}"[:500]
        with db.write_txn(conn):
            if retryable and job.attempt < self.cfg.ocr.max_attempts:
                delay = int(self.cfg.ocr.retry_backoff_seconds * 1000 * job.attempt)
                conn.execute(
                    "UPDATE index_jobs SET status = 'pending', worker_token = NULL, not_before_ms = ?, "
                    "updated_at_ms = ?, last_error = ? WHERE job_id = ? AND worker_token = ?",
                    (now + delay, now, error, job.job_id, job.token),
                )
                final = False
            else:
                conn.execute(
                    "UPDATE index_jobs SET status = 'failed', worker_token = NULL, updated_at_ms = ?, last_error = ? "
                    "WHERE job_id = ? AND worker_token = ?",
                    (now, error, job.job_id, job.token),
                )
                conn.execute(
                    "UPDATE frames SET ocr_status = 'failed'"
                    + (", image_status = 'missing'" if image_missing else "")
                    + " WHERE frame_id = ?",
                    (job.frame_id,),
                )
                if image_missing:
                    conn.execute(
                        "UPDATE frames SET text_embed_status = 'skipped', image_embed_status = 'skipped' "
                        "WHERE frame_id = ?",
                        (job.frame_id,),
                    )
                else:
                    # OCR 失败不删除其他分支：原图仍可用时照常计算全图向量（方案 3.2）
                    vectors.register_embed_jobs(conn, job.frame_id, job.target_version, has_text=False)
                final = True
        self.log.warning(
            "ocr %s frame=%s attempt=%d %s", "failed" if final else "retry", job.frame_id, job.attempt, error
        )
