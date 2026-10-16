"""向量待办处理：文本块语义向量与全图向量（方案 4.3、5.1；提案 3.4、4.2）。

- 按待办 job_id（≈ 抓拍顺序）领取，最老待办优先；OCR 待办达到高水位时暂停向量，先让文字可搜；
- 推理在事务外；写回前短事务核对执行标识、目标版本与图片状态，删除或重建后不复活；
- 文本：从 OCR 行按实际 tokenizer 分块，只为缺向量的共享块编码；重写本帧块关联并回收失去引用的旧块；
- 全图：原图保持比例补边后编码，每帧一份；原图不可用则取消；
- 每次写回同一事务内 bump_revision，供页面刷新向量矩阵。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from . import classify, db, models, storage, vectors
from .config import Config
from .timeutil import utc_ms


@dataclass(frozen=True)
class _Job:
    job_id: int
    frame_id: str
    stage: str
    target_version: int
    attempt: int
    token: str
    image_path: str


_CLAIM_SQL = """
SELECT j.job_id, j.frame_id, j.stage, j.target_version, j.attempts,
       f.image_path, f.image_status, f.record_version
FROM index_jobs j JOIN frames f ON f.frame_id = j.frame_id
WHERE j.stage IN ('text_embed', 'image_embed') AND j.status = 'pending' AND j.not_before_ms <= ?
ORDER BY j.job_id
LIMIT 1
"""


def ocr_backlog(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM index_jobs WHERE stage = 'ocr' AND status IN ('pending', 'running')"
    ).fetchone()[0])


class EmbedWorker:
    """text_encoder / image_encoder 由工厂函数延迟创建（首次需要时加载模型），测试可注入替身。"""

    def __init__(
        self,
        cfg: Config,
        wake: threading.Event,
        stop: threading.Event,
        log: logging.Logger,
        *,
        text_encoder_factory: Callable[[], models.TextEncoder] | None = None,
        image_encoder_factory: Callable[[], models.ImageEncoder] | None = None,
        classifier=...,
    ):
        self.cfg = cfg
        self.wake = wake
        self.stop = stop
        self.log = log
        self._text_factory = text_encoder_factory or (lambda: models.StText(cfg.models.text, cfg.models))
        self._image_factory = image_encoder_factory or (lambda: models.StImage(cfg.models.image, cfg.models))
        self._text: models.TextEncoder | None = None
        self._image: models.ImageEncoder | None = None
        # 内容分类器（可选）：写回全图向量时顺带写预测标签；加载失败只记日志，不影响向量
        try:
            self.classifier = classify.load_for(cfg) if classifier is ... else classifier
        except Exception:
            log.exception("分类器加载失败，页面将显示待分类")
            self.classifier = None

    @property
    def text_encoder(self) -> models.TextEncoder:
        if self._text is None:
            self._text = self._text_factory()
        return self._text

    @property
    def image_encoder(self) -> models.ImageEncoder:
        if self._image is None:
            self._image = self._image_factory()
        return self._image

    # ---------------- 循环 ----------------

    def run(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            while not self.stop.is_set():
                try:
                    if not self.run_once(conn):
                        self.wake.wait(2.0)
                        self.wake.clear()
                except Exception:
                    self.log.exception("向量循环出错")
                    self.stop.wait(2.0)
        finally:
            conn.close()

    def run_once(self, conn: sqlite3.Connection) -> bool:
        """处理至多一个待办；返回是否做了事。OCR 积压时让路（方案 4.3）。"""
        if ocr_backlog(conn) >= self.cfg.index.ocr_high_water:
            return False
        job = self._claim(conn)
        if job is None:
            return False
        if job.stage == vectors.TEXT_STAGE:
            self._process_text(conn, job)
        else:
            self._process_image(conn, job)
        return True

    def _claim(self, conn: sqlite3.Connection) -> _Job | None:
        while True:
            now = utc_ms()
            with db.write_txn(conn):
                row = conn.execute(_CLAIM_SQL, (now,)).fetchone()
                if row is None:
                    return None
                dead = row["image_status"] == "deleted" or (
                    row["stage"] == vectors.IMAGE_STAGE and row["image_status"] != "ok"
                )
                if dead or row["record_version"] != row["target_version"]:
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
                return _Job(row["job_id"], row["frame_id"], row["stage"], row["target_version"],
                            row["attempts"] + 1, token, row["image_path"])

    def _still_valid(self, conn: sqlite3.Connection, job: _Job, *, need_image: bool) -> bool:
        row = conn.execute(
            "SELECT f.image_status FROM index_jobs j JOIN frames f ON f.frame_id = j.frame_id "
            "WHERE j.job_id = ? AND j.status = 'running' AND j.worker_token = ? AND f.record_version = ?",
            (job.job_id, job.token, job.target_version),
        ).fetchone()
        if row is None or row[0] == "deleted":
            return False
        return row[0] == "ok" if need_image else True

    def _discard(self, conn: sqlite3.Connection, job: _Job) -> None:
        conn.execute(
            "UPDATE index_jobs SET status = 'cancelled', worker_token = NULL, updated_at_ms = ?, "
            "last_error = '写回前目标已失效，结果已丢弃' WHERE job_id = ? AND worker_token = ?",
            (utc_ms(), job.job_id, job.token),
        )

    def _done(self, conn: sqlite3.Connection, job: _Job, status_col: str) -> None:
        now = utc_ms()
        conn.execute(f"UPDATE frames SET {status_col} = 'done' WHERE frame_id = ?", (job.frame_id,))
        conn.execute(
            "UPDATE index_jobs SET status = 'done', worker_token = NULL, updated_at_ms = ?, last_error = NULL "
            "WHERE job_id = ?",
            (now, job.job_id),
        )
        db.bump_revision(conn)

    # ---------------- 文本 ----------------

    def _process_text(self, conn: sqlite3.Connection, job: _Job) -> None:
        t0 = time.perf_counter()
        try:
            enc = self.text_encoder
            lines = vectors.load_ocr_lines(conn, job.frame_id)
            chunks = vectors.chunk_lines(lines, enc.count_tokens, max_tokens=enc.max_tokens)
            keys = [vectors.block_key(c.text) for c in chunks]
            have = self._keys_with_vectors(conn, keys, enc.model_id)
            todo = {k: c.text for k, c in zip(keys, chunks) if k not in have}
            todo_keys = list(todo)
            vecs = enc.encode([todo[k] for k in todo_keys]) if todo_keys else None
        except Exception as exc:
            self._fail(conn, job, "text_embed_error", f"{type(exc).__name__}: {exc}", retryable=True)
            return
        now = utc_ms()
        with db.write_txn(conn):
            if not self._still_valid(conn, job, need_image=False):
                self._discard(conn, job)
                self.log.info("text_embed stale frame=%s：结果丢弃", job.frame_id)
                return
            old = [r[0] for r in conn.execute("SELECT block_id FROM frame_blocks WHERE frame_id = ?", (job.frame_id,))]
            vectors.replace_frame_blocks(conn, job.frame_id, chunks, now)
            for i, k in enumerate(todo_keys):
                bid = conn.execute("SELECT block_id FROM text_blocks WHERE block_key = ?", (k,)).fetchone()[0]
                vectors.put_embedding(conn, target_kind="block", target_id=str(bid), branch="text",
                                      model_id=enc.model_id, preprocess=vectors.TEXT_PREPROCESS,
                                      vec=vecs[i], now_ms=now)
            vectors.gc_blocks(conn, old)
            self._done(conn, job, "text_embed_status")
        self.log.info(
            "text_embed done frame=%s blocks=%d encoded=%d ms=%d",
            job.frame_id, len(chunks), len(todo_keys), int((time.perf_counter() - t0) * 1000),
        )

    @staticmethod
    def _keys_with_vectors(conn: sqlite3.Connection, keys: list[str], model_id: str) -> set[str]:
        if not keys:
            return set()
        rows = conn.execute(
            "SELECT b.block_key FROM text_blocks b JOIN embeddings e ON e.target_kind = 'block' "
            "AND e.target_id = CAST(b.block_id AS TEXT) AND e.branch = 'text' AND e.model_id = ? "
            "AND e.preprocess_version = ? WHERE b.block_key IN (SELECT value FROM json_each(?))",
            (model_id, vectors.TEXT_PREPROCESS, json.dumps(keys)),
        )
        return {r[0] for r in rows}

    # ---------------- 全图 ----------------

    def _process_image(self, conn: sqlite3.Connection, job: _Job) -> None:
        t0 = time.perf_counter()
        path = storage.resolve(self.cfg.data_dir, job.image_path)
        try:
            with Image.open(path) as im:
                im.load()
                image = im.convert("RGB")
        except FileNotFoundError:
            self._fail(conn, job, "image_missing", "图片文件缺失", retryable=False)
            return
        except OSError as exc:
            self._fail(conn, job, "image_decode", str(exc), retryable=True)
            return
        try:
            enc = self.image_encoder
            vec = enc.encode_images([image])[0]
        except Exception as exc:
            self._fail(conn, job, "image_embed_error", f"{type(exc).__name__}: {exc}", retryable=True)
            return
        now = utc_ms()
        with db.write_txn(conn):
            if not self._still_valid(conn, job, need_image=True):
                self._discard(conn, job)
                self.log.info("image_embed stale frame=%s：结果丢弃", job.frame_id)
                return
            vectors.put_embedding(conn, target_kind="frame", target_id=job.frame_id, branch="image",
                                  model_id=enc.model_id, preprocess=models.IMAGE_PREPROCESS, vec=vec, now_ms=now)
            clf = self.classifier
            if clf is not None and clf.card["feature_model_id"] == enc.model_id:
                classify.write_prediction(conn, clf, job.frame_id, vec)
            self._done(conn, job, "image_embed_status")
        self.log.info("image_embed done frame=%s ms=%d", job.frame_id, int((time.perf_counter() - t0) * 1000))

    # ---------------- 失败 ----------------

    def _fail(self, conn: sqlite3.Connection, job: _Job, code: str, msg: str, *, retryable: bool) -> None:
        now = utc_ms()
        error = f"{code}: {msg}"[:500]
        col = "text_embed_status" if job.stage == vectors.TEXT_STAGE else "image_embed_status"
        with db.write_txn(conn):
            if retryable and job.attempt < self.cfg.index.embed_max_attempts:
                delay = int(self.cfg.index.embed_retry_backoff_seconds * 1000 * job.attempt)
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
                conn.execute(f"UPDATE frames SET {col} = 'failed' WHERE frame_id = ?", (job.frame_id,))
                final = True
        self.log.warning("%s %s frame=%s attempt=%d %s", job.stage, "failed" if final else "retry",
                         job.frame_id, job.attempt, error)
