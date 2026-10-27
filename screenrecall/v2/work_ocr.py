"""第 1 步：待 OCR 区域（UIA 读不到字的证据帧或其中一块）。

ocr_claim → 按区域裁剪证据图 → OcrEngine(tiny).recognize(crop, crop=True) → 加回裁剪偏移
→ writer.ocr_result(region_id, 帧像素坐标的行)。tiny 的平均置信度低于 second_pass_below 时用 small 再读一遍，
取平均置信度高的那一份。失败 → 记下错误，这次整理不再试（下次整理进程启动时再试）；
到 ocr_max_attempts 次 → ocr_complete(ok=False)，标 failed。
缺 OCR 模型：记一条中文提示，跳过整步（不领取区域，不消耗重试次数）。
"""

from __future__ import annotations

import logging
from typing import Callable

from ..store.writer import LineIn
from .work_common import Ctx, Stopped, log, read_image, timed

OcrFactory = Callable[[str], object]  # model -> 有 recognize(bgr, crop=True) 的引擎


def default_ocr_factory(cfg) -> OcrFactory:
    def make(model: str):
        from ..text.ocr2 import OcrEngine

        return OcrEngine(model, threads=cfg.ocr.threads, models_dir=cfg.ocr_models_dir)
    return make


class OcrStep:
    name = "ocr"

    def __init__(self, ctx: Ctx, factory: OcrFactory):
        self.ctx = ctx
        self.factory = factory
        self._engines: dict[str, object | None] = {}

    def close(self) -> None:
        """放掉已加载的引擎（缺模型的记录保留，不再重试加载）。"""
        self._engines = {k: v for k, v in self._engines.items() if v is None}

    def _engine(self, model: str):
        if model not in self._engines:
            try:
                self._engines[model] = self.factory(model)
            except Exception as exc:  # noqa: BLE001 —— OcrModelsMissing、rapidocr 没装、模型损坏
                self._engines[model] = None
                self.ctx.once(f"ocr-missing-{model}", "OCR 模型（%s）不可用，跳过%s：%s", model,
                              "文字识别" if model == self.ctx.cfg.ocr.model else "低置信度复查", exc)
        return self._engines[model]

    def pending(self) -> int:
        return int(self.ctx.conn.execute(
            "SELECT COUNT(*) FROM ocr_regions WHERE status = 'pending'").fetchone()[0])

    def run(self) -> int:
        """处理到没有可领取的区域为止；返回处理的区域数。"""
        ctx = self.ctx
        if self.pending() == 0:
            return 0
        engine = self._engine(ctx.cfg.ocr.model)
        if engine is None:
            return 0
        done = 0
        with timed(ctx, self.name) as st:
            while not ctx.stop.is_set():
                regions = ctx.writer.ocr_claim(max(1, ctx.cfg.ocr.batch_regions)).result(30)
                if not regions:
                    break
                cache: dict[str, object] = {}
                for i, r in enumerate(regions):
                    if ctx.stop.is_set():
                        _release(ctx, [x["region_id"] for x in regions[i:]])
                        raise Stopped()
                    self._one(engine, r, cache, st)
                    done += 1
                    st.add()
        return done

    def _one(self, engine, r: dict, cache: dict, st) -> None:
        ctx = self.ctx
        rid = int(r["region_id"])
        try:
            path = r["image_abs_path"]
            img = cache.get(path)
            if img is None:
                cache.clear()
                img = cache[path] = read_image(path)
            h, w = img.shape[:2]
            x0, y0, x1, y1 = (int(v) for v in r["rect"])
            x0, y0 = max(0, min(x0, w)), max(0, min(y0, h))
            x1, y1 = max(x0, min(x1, w)), max(y0, min(y1, h))
            lines: list[LineIn] = []
            if x1 - x0 >= 2 and y1 - y0 >= 2:
                crop = img[y0:y1, x0:x1]
                found = engine.recognize(crop, crop=True)
                found = self._second_pass(crop, found, st)
                for ln in found:
                    text = str(ln.text).strip()
                    if not text:
                        continue
                    lines.append(LineIn(text, ln.x0 + x0, ln.y0 + y0, ln.x1 + x0, ln.y1 + y0, "ocr",
                                        float(ln.confidence)))
            ctx.writer.ocr_result(rid, lines).result(30)
            st.extra["lines"] = st.extra.get("lines", 0) + len(lines)
            st.extra["pixels"] = st.extra.get("pixels", 0) + (x1 - x0) * (y1 - y0)
        except Stopped:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单个区域失败不影响别的
            log.warning("区域 %d 识别失败：%s", rid, type(exc).__name__)
            log.debug("区域 %d 识别失败详情", rid, exc_info=True)
            st.extra["failed"] = st.extra.get("failed", 0) + 1
            err = f"{type(exc).__name__}: {exc}"[:300]
            if int(r.get("attempts", 1)) >= ctx.writer.cfg.ocr_max_attempts:
                ctx.writer.ocr_complete(rid, ok=False, error=err).result(30)  # 到次数了：failed
            else:
                ctx.writer.call(_defer, rid, err, ctx.clock()).result(30)

    def _second_pass(self, crop, found: list, st) -> list:
        cfg = self.ctx.cfg.ocr
        if not cfg.second_pass or not found or cfg.model == "small":
            return found
        mean = sum(float(x.confidence) for x in found) / len(found)
        if mean >= cfg.second_pass_below:
            return found
        small = self._engine("small")
        if small is None:
            return found
        st.extra["second_pass"] = st.extra.get("second_pass", 0) + 1
        better = small.recognize(crop, crop=True)
        if not better:
            return found
        mean2 = sum(float(x.confidence) for x in better) / len(better)
        return better if mean2 >= mean else found


def _defer(conn, region_id: int, error: str, now: int) -> None:
    """失败但没到最大次数：记下错误，保持"已领取"，这次整理不再领它（否则马上又领到、连着失败三次）。
    下次整理进程启动时（_recover）或 claim_stale_ms 之后再试。"""
    conn.execute("UPDATE ocr_regions SET status = 'running', claimed_ms = ?, last_error = ?, updated_ms = ? "
                 "WHERE region_id = ?", (now, error, now, region_id))


def _release(ctx: Ctx, region_ids: list[int]) -> None:
    """停下时把领了还没做的区域放回去（不算一次尝试）。"""
    if not region_ids:
        return

    def op(conn, ids):
        conn.executemany(
            "UPDATE ocr_regions SET status = 'pending', attempts = max(0, attempts - 1), claimed_ms = NULL "
            "WHERE region_id = ? AND status = 'running'", [(int(i),) for i in ids])
    try:
        ctx.writer.call(op, region_ids).result(10)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("放回区域失败", exc_info=True)
