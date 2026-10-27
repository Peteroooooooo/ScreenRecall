"""第 6、7 步：证据帧的 CLIP 图片向量 + 内容分类；旧证据帧只统计不删除。

CLIP
- 新证据帧（frame_no 大于游标 worker.clip_frame_no、status = ok）按批编码，追加到 <数据目录>/vectors/image：
  id = frame_no，ts = captured_ms，group = segment_id（store 约定；没有内容源时为 0）；
- 游标不越过还在编码（status = encoding）的帧，免得漏掉；
- 一次空闲整理只进一次 ClipBatch（加载模型、限制 torch 线程），做完释放；
- 内容分类器（results/classifier/v1，CLIP 向量 + 线性模型）的特征模型与 CLIP 一致时，
  把类别写进 frames.label / label_score / label_model；
- 追加后核对帧还在（期间被隐私删除就把向量清掉）。

旧帧统计：早于 frame_report_days 天、没标"保留"的证据帧有多少张 / 多少字节，只记日志，不删。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from ..store.vectors import VectorStore
from .work_common import Ctx, log, read_image, timed

CLIP_CURSOR = "worker.clip_frame_no"
IMAGE_DIM = 512


def load_classifier(cfg, clip_model_id: str):
    """按配置加载内容分类器；特征模型（CLIP 版本 + 预处理）对不上时返回 None。"""
    d = cfg.idle.classifier_dir
    if not d:
        return None
    from .. import models
    from ..classify import LinearClassifier
    from ..config import REPO_ROOT

    p = Path(d)
    if not p.is_absolute():
        p = REPO_ROOT / p
    if not (p / "card.json").is_file():
        return None
    clf = LinearClassifier.load(p)
    if clf.card.get("feature_model_id") != clip_model_id or clf.card.get("feature_preprocess") != models.IMAGE_PREPROCESS:
        log.info("内容分类器的特征模型与当前 CLIP 不一致，不预测类别")
        return None
    return clf


def _label_put(conn, labels: list[tuple[str, float, str, str]], cursor: int) -> None:
    conn.executemany("UPDATE frames SET label = ?, label_score = ?, label_model = ? WHERE frame_id = ?", labels)
    Ctx.meta_put_sql(conn, CLIP_CURSOR, cursor)


class ClipStep:
    name = "clip"
    batch = 16

    def __init__(self, ctx: Ctx, clip_factory: Callable[[], object],
                 classifier_loader: Callable[[str], object] | None = None,
                 vectors_factory: Callable[[], VectorStore] | None = None):
        self.ctx = ctx
        self._clip_factory = clip_factory
        self._classifier_loader = classifier_loader or (lambda mid: load_classifier(ctx.cfg, mid))
        self._vectors_factory = vectors_factory or (
            lambda: VectorStore(ctx.data_dir / "vectors" / "image", IMAGE_DIM))
        self._off = False

    def _next(self, cursor: int, limit: int) -> list:
        rows = self.ctx.conn.execute(
            "SELECT frame_id, frame_no, image_path, captured_ms, segment_id, status FROM frames "
            "WHERE frame_no > ? AND status IN ('ok', 'encoding') ORDER BY frame_no LIMIT ?",
            (cursor, limit)).fetchall()
        out = []
        for r in rows:
            if r["status"] == "encoding":
                break  # 还没写完的帧：游标停在它前面
            out.append(r)
        return out

    def backlog(self) -> int:
        if not self.ctx.cfg.idle.clip or self._off:
            return 0
        return len(self._next(self.ctx.meta_get(CLIP_CURSOR), 1))

    def run(self) -> int:
        ctx = self.ctx
        if self.backlog() == 0:
            return 0
        done = 0
        with timed(ctx, self.name) as st:
            try:
                batch_ctx = self._clip_factory()
                clip = batch_ctx.__enter__()
            except Exception as exc:  # noqa: BLE001 —— 没装 torch、缺模型
                self._off = True
                ctx.once("clip-missing", "CLIP 图片模型不可用，跳过图片向量：%s", exc)
                return 0
            vs = None
            try:
                vs = self._vectors_factory()
                clf = None
                try:
                    clf = self._classifier_loader(getattr(clip, "model_id", ""))
                except Exception as exc:  # noqa: BLE001
                    ctx.once("clf-missing", "内容分类器加载失败，不预测类别：%s", exc)
                while not ctx.stop.is_set():
                    rows = self._next(ctx.meta_get(CLIP_CURSOR), self.batch)
                    if not rows:
                        break
                    done += self._batch(clip, clf, vs, rows, st)
            finally:
                if vs is not None:
                    vs.close()
                batch_ctx.__exit__(None, None, None)
        return done

    def _batch(self, clip, clf, vs: VectorStore, rows: list, st) -> int:
        ctx = self.ctx
        imgs, keep = [], []
        for r in rows:
            try:
                imgs.append(read_image(str(ctx.data_dir / r["image_path"])))
                keep.append(r)
            except Exception:  # noqa: BLE001 —— 文件被删 / 损坏：跳过这一帧
                st.extra["unreadable"] = st.extra.get("unreadable", 0) + 1
        vecs = np.asarray(clip.encode(imgs), dtype=np.float32).reshape(-1, IMAGE_DIM) if imgs else None
        labels = []
        if keep:
            ids = [int(r["frame_no"]) for r in keep]
            # 上次追加完、没来得及推进游标就退出了：已在库里的不再追加（id 不能重复）
            have = {h.id for h in vs.search(np.zeros(IMAGE_DIM, np.float32), k=len(ids), ids=ids)}
            new = [i for i, x in enumerate(ids) if x not in have]
            if new:
                vs.append([ids[i] for i in new], vecs[new], [int(keep[i]["captured_ms"]) for i in new],
                          [int(keep[i]["segment_id"] or 0) for i in new])
            if clf is not None:
                names, scores = clf.predict(vecs)
                labels = [(n, float(s), clf.version, r["frame_id"]) for n, s, r in zip(names, scores, keep)]
        ctx.writer.call(_label_put, labels, int(rows[-1]["frame_no"])).result(30)
        if keep:
            ph = ", ".join("?" * len(keep))
            alive = {int(x[0]) for x in ctx.conn.execute(
                f"SELECT frame_no FROM frames WHERE frame_no IN ({ph}) AND status = 'ok'",
                [int(r["frame_no"]) for r in keep])}
            dead = [int(r["frame_no"]) for r in keep if int(r["frame_no"]) not in alive]
            if dead:
                log.info("%d 张证据帧在算向量期间被删除，已清掉对应向量", len(dead))
                vs.delete(dead)
        st.add(len(keep))
        st.extra["labelled"] = st.extra.get("labelled", 0) + len(labels)
        return len(rows)


def frame_report(ctx: Ctx) -> dict:
    """第 7 步：只统计，不删除。"""
    days = int(ctx.cfg.idle.frame_report_days)
    cutoff = ctx.clock() - days * 86_400_000
    r = ctx.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM frames WHERE status = 'ok' AND keep = 0 AND captured_ms < ?",
        (cutoff,)).fetchone()
    kept = ctx.conn.execute("SELECT COUNT(*) FROM frames WHERE status = 'ok' AND keep = 1 AND captured_ms < ?",
                            (cutoff,)).fetchone()[0]
    rep = {"older_than_days": days, "frames": int(r[0]), "bytes": int(r[1]), "kept": int(kept)}
    ctx.stat("frames_report").extra.update(rep)
    return rep
