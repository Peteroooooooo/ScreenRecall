"""文本块与向量的存储约定（方案 3.1、3.3、3.7、5.1；提案 4.2、5.1）。

- 文本块：按 OCR 行序、简单空间间距和**实际 tokenizer** 长度切块（计入特殊 token），长块少量重叠；
  相同规范化文本只存一份（text_blocks），每帧各自的块顺序、行范围、原文范围和坐标存在 frame_blocks；
- 规范化只用于检索与缓存：NFC + 去掉每行行尾空白；不合并行内空白，显示用原文仍取 frame_text；
- 向量：float32 小端 BLOB，带模型标识（repo@revision）、预处理版本、维度与类型；
  同一 (目标, 分支, 模型, 预处理) 只有一份，换模型或预处理会生成新的特征集，不与旧向量混用；
- OCR 完成后登记 text_embed / image_embed 待办；无文字的帧只登记 image_embed。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

import numpy as np

NORM_VERSION = "n1"  # NFC + 行尾空白去除
CHUNKER_VERSION = "c1"  # 行序 + 空间间距 + tokenizer 长度 + 1 行重叠
TEXT_PREPROCESS = f"{NORM_VERSION}/{CHUNKER_VERSION}"

TEXT_STAGE = "text_embed"
IMAGE_STAGE = "image_embed"
EMBED_STAGES = (TEXT_STAGE, IMAGE_STAGE)


# ---------------- 规范化与分块（纯函数） ----------------

def normalize(text: str) -> str:
    """检索/缓存用规范化：NFC，去掉每行行尾空白。不改动行内空白与大小写。"""
    text = unicodedata.normalize("NFC", text)
    return "\n".join(line.rstrip() for line in text.split("\n"))


@dataclass(frozen=True)
class OcrLine:
    line_no: int
    text: str
    char_start: int
    char_end: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class Chunk:
    text: str  # 规范化后的块文本（用于编码与复用）
    line_start: int
    line_end: int  # 含
    char_start: int  # 在 frame_text.text 中的 [start, end)
    char_end: int
    bbox: tuple[float, float, float, float]
    token_count: int


def _split_long(line: OcrLine, count: Callable[[str], int], max_tokens: int) -> list[OcrLine]:
    """单行超长时按字符切成若干段，每段都不超过 max_tokens；坐标按字符比例近似分配。"""
    pieces: list[OcrLine] = []
    text = line.text
    start = 0
    n = len(text)
    width = line.x1 - line.x0
    while start < n:
        lo, hi = start + 1, n
        while lo < hi:  # 最长可容纳前缀
            mid = (lo + hi + 1) // 2
            if count(text[start:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        end = max(lo, start + 1)
        fx0 = line.x0 + width * start / n
        fx1 = line.x0 + width * end / n
        pieces.append(
            OcrLine(line.line_no, text[start:end], line.char_start + start, line.char_start + end,
                    fx0, line.y0, fx1, line.y1)
        )
        start = end
    return pieces


def chunk_lines(
    lines: Sequence[OcrLine],
    count_tokens: Callable[[str], int],
    *,
    max_tokens: int = 128,
    overlap_lines: int = 1,
    gap_factor: float = 1.5,
) -> list[Chunk]:
    """按行序组块。

    count_tokens(text) 必须返回包含特殊 token 的真实长度（如 [CLS]/[SEP]）。
    换块条件：加入下一行后超长，或与上一行的垂直间距大于 gap_factor × 行高中位数。
    因超长换块时，新块以前一块最后 overlap_lines 行开头（若仍放得下）；因间距换块时不重叠。
    """
    units: list[OcrLine] = []
    for ln in lines:
        if not normalize(ln.text).strip():
            continue
        if count_tokens(normalize(ln.text)) > max_tokens:
            units.extend(_split_long(ln, lambda s: count_tokens(normalize(s)), max_tokens))
        else:
            units.append(ln)
    if not units:
        return []
    heights = [max(1.0, u.y1 - u.y0) for u in units]
    med_h = statistics.median(heights)

    chunks: list[Chunk] = []

    def emit(group: list[OcrLine]) -> None:
        text = normalize("\n".join(u.text for u in group))
        chunks.append(
            Chunk(
                text=text,
                line_start=group[0].line_no,
                line_end=group[-1].line_no,
                char_start=min(u.char_start for u in group),
                char_end=max(u.char_end for u in group),
                bbox=(min(u.x0 for u in group), min(u.y0 for u in group),
                      max(u.x1 for u in group), max(u.y1 for u in group)),
                token_count=count_tokens(text),
            )
        )

    group: list[OcrLine] = [units[0]]
    for prev, cur in zip(units, units[1:]):
        gap = cur.y0 - prev.y1
        spatial_break = gap > gap_factor * med_h
        candidate = normalize("\n".join(u.text for u in group + [cur]))
        if spatial_break:
            emit(group)
            group = [cur]
        elif count_tokens(candidate) > max_tokens:
            emit(group)
            carry = group[-overlap_lines:] if overlap_lines > 0 else []
            # 重叠只在放得下时保留，且不重复同一块
            while carry and count_tokens(normalize("\n".join(u.text for u in carry + [cur]))) > max_tokens:
                carry = carry[1:]
            group = carry + [cur]
        else:
            group.append(cur)
    emit(group)
    return chunks


def block_key(text: str) -> str:
    raw = f"{NORM_VERSION}\x00{CHUNKER_VERSION}\x00{text}".encode("utf-8")
    return hashlib.blake2b(raw, digest_size=20).hexdigest()


# ---------------- 向量编码 ----------------

def to_blob(vec: np.ndarray) -> bytes:
    return np.ascontiguousarray(vec, dtype="<f4").tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    arr = np.frombuffer(blob, dtype="<f4")
    if arr.size != dim:
        raise ValueError(f"向量长度 {arr.size} 与登记维度 {dim} 不符")
    return arr


# ---------------- 数据库操作（须在调用方事务内） ----------------

def load_ocr_lines(conn: sqlite3.Connection, frame_id: str) -> list[OcrLine]:
    return [
        OcrLine(r["line_no"], r["text"], r["char_start"], r["char_end"], r["x0"], r["y0"], r["x1"], r["y1"])
        for r in conn.execute(
            "SELECT line_no, text, char_start, char_end, x0, y0, x1, y1 FROM ocr_lines "
            "WHERE frame_id = ? ORDER BY line_no",
            (frame_id,),
        )
    ]


def register_embed_jobs(conn: sqlite3.Connection, frame_id: str, record_version: int, *, has_text: bool) -> None:
    """OCR 结束后登记向量待办。created_at 用抓拍时间，使待办年龄计入从抓拍开始的等待。"""
    captured = conn.execute("SELECT captured_at_ms FROM frames WHERE frame_id = ?", (frame_id,)).fetchone()[0]
    stages = [IMAGE_STAGE] + ([TEXT_STAGE] if has_text else [])
    for stage in stages:
        conn.execute(
            "INSERT INTO index_jobs (frame_id, stage, status, target_version, created_at_ms, updated_at_ms) "
            "VALUES (?, ?, 'pending', ?, ?, ?) "
            "ON CONFLICT (frame_id, stage) DO UPDATE SET status = 'pending', target_version = excluded.target_version, "
            "attempts = 0, worker_token = NULL, not_before_ms = 0, last_error = NULL, updated_at_ms = excluded.updated_at_ms",
            (frame_id, stage, record_version, captured, captured),
        )
    conn.execute(
        "UPDATE frames SET text_embed_status = ?, image_embed_status = 'pending' WHERE frame_id = ?",
        ("pending" if has_text else "skipped", frame_id),
    )


def upsert_block(conn: sqlite3.Connection, chunk: Chunk, now_ms: int) -> int:
    key = block_key(chunk.text)
    conn.execute(
        "INSERT INTO text_blocks (block_key, text, norm_version, chunker_version, token_count, created_at_ms) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (block_key) DO NOTHING",
        (key, chunk.text, NORM_VERSION, CHUNKER_VERSION, chunk.token_count, now_ms),
    )
    return int(conn.execute("SELECT block_id FROM text_blocks WHERE block_key = ?", (key,)).fetchone()[0])


def replace_frame_blocks(conn: sqlite3.Connection, frame_id: str, chunks: Sequence[Chunk], now_ms: int) -> list[int]:
    """幂等地重写一帧的块关联，返回按 seq 排列的 block_id。旧的、不再被引用的块由回收逻辑处理。"""
    conn.execute("DELETE FROM frame_blocks WHERE frame_id = ?", (frame_id,))
    ids: list[int] = []
    for seq, ch in enumerate(chunks):
        bid = upsert_block(conn, ch, now_ms)
        ids.append(bid)
        conn.execute(
            "INSERT INTO frame_blocks (frame_id, seq, block_id, chunker_version, line_start, line_end, "
            "char_start, char_end, x0, y0, x1, y1) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (frame_id, seq, bid, CHUNKER_VERSION, ch.line_start, ch.line_end, ch.char_start, ch.char_end, *ch.bbox),
        )
    return ids


def missing_block_vectors(conn: sqlite3.Connection, block_ids: Iterable[int], model_id: str) -> list[int]:
    ids = sorted(set(block_ids))
    if not ids:
        return []
    have = {
        int(r[0])
        for r in conn.execute(
            "SELECT target_id FROM embeddings WHERE target_kind = 'block' AND branch = 'text' "
            "AND model_id = ? AND preprocess_version = ? AND target_id IN (SELECT value FROM json_each(?))",
            (model_id, TEXT_PREPROCESS, json.dumps([str(i) for i in ids])),
        )
    }
    return [i for i in ids if i not in have]


def put_embedding(
    conn: sqlite3.Connection, *, target_kind: str, target_id: str, branch: str, model_id: str,
    preprocess: str, vec: np.ndarray, now_ms: int,
) -> None:
    conn.execute(
        "INSERT INTO embeddings (target_kind, target_id, branch, model_id, preprocess_version, dim, dtype, vector, "
        "created_at_ms) VALUES (?, ?, ?, ?, ?, ?, 'float32', ?, ?) "
        "ON CONFLICT (target_kind, target_id, branch, model_id, preprocess_version) DO UPDATE SET "
        "vector = excluded.vector, dim = excluded.dim, created_at_ms = excluded.created_at_ms",
        (target_kind, str(target_id), branch, model_id, preprocess, int(vec.shape[-1]), to_blob(vec), now_ms),
    )


def gc_orphan_blocks(conn: sqlite3.Connection) -> int:
    """回收不再被任何帧引用的文本块及其向量（最后一个有效引用消失后才回收，方案 3.2）。"""
    orphans = [
        int(r[0])
        for r in conn.execute(
            "SELECT block_id FROM text_blocks b WHERE NOT EXISTS "
            "(SELECT 1 FROM frame_blocks fb WHERE fb.block_id = b.block_id)"
        )
    ]
    for bid in orphans:
        conn.execute("DELETE FROM embeddings WHERE target_kind = 'block' AND target_id = ?", (str(bid),))
        conn.execute("DELETE FROM text_blocks WHERE block_id = ?", (bid,))
    return len(orphans)
