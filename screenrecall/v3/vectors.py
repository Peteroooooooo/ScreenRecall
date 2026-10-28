"""可选（"有空再做"，cfg.vectors 默认关）：每行字一条 MiniLM 向量，按意思搜。

向量库：store.vectors.VectorStore(<数据目录>/vectors/lines, dim=384)，id = line_id，ts = 行最近看到的时间，
group = 最近看到它的出处。空闲整理进程从 meta 'vec_line_cursor' 之后接着编码（Queries.lines_after），
写完一批再推进游标；进程中途被杀，下次重做的那批里已经在库里的 id 跳过（不重复追加）。
主进程用 search() 给 Queries.search(semantic=…)。编码器第一次用时才加载（LazyTextEncoder，闲置自动卸载）。
隐私删除删掉的行：向量还在，但 Queries.search 只认库里还在的 line_id；下次编码不会再碰它们
（lines.line_id 是 AUTOINCREMENT，删掉的编号不会给新行，旧向量不会冒充新行）。
"""

from __future__ import annotations

import logging

import numpy as np

from ..text.encoder import DIM
from .config import V3Config

log = logging.getLogger("screenrecall.v3.worker")

CURSOR_KEY = "vec_line_cursor"
FETCH = 1000
ENCODE_BATCH = 64


class LineVectors:
    def __init__(self, cfg: V3Config, *, encoder=None, readonly: bool = False):
        self.cfg = cfg
        self.root = cfg.vectors_dir / "lines"
        self.readonly = readonly
        self._encoder = encoder
        self._vs = None

    @property
    def encoder(self):
        if self._encoder is None:
            from ..text.encoder import LazyTextEncoder

            self._encoder = LazyTextEncoder(threads=2)
        return self._encoder

    def _store(self):
        """向量库（第一次用时打开）；只读且还没建库时返回 None。"""
        if self._vs is None:
            if self.readonly and not (self.root / "manifest.json").exists():
                return None
            from ..store.vectors import VectorStore

            self._vs = VectorStore(self.root, DIM, readonly=self.readonly)
        return self._vs

    def close(self) -> None:
        if self._vs is not None:
            self._vs.close()
            self._vs = None

    def update(self, queries, store, stop) -> int:
        """把游标之后的新行编码追加进向量库；返回追加了几行。每批之间看停止标记。"""
        vs = self._store()
        try:
            cursor = int(store.meta_get(CURSOR_KEY, "0") or 0)
        except (TypeError, ValueError):
            cursor = 0
        added = 0
        while stop is None or not stop.is_set():
            rows = queries.lines_after(cursor, limit=FETCH) or []
            if not rows:
                break
            for s in range(0, len(rows), ENCODE_BATCH):
                if stop is not None and stop.is_set():
                    return added
                part = rows[s:s + ENCODE_BATCH]
                have = vs.vectors([r[0] for r in part], refresh=True)
                part = [r for r in part if int(r[0]) not in have]
                if part:
                    vecs = np.asarray(self.encoder.encode([str(r[1]) for r in part]), dtype=np.float32)
                    vs.append([int(r[0]) for r in part], vecs.reshape(len(part), DIM), [int(r[2]) for r in part],
                              [int(r[3] or 0) for r in part])
                    added += len(part)
                cursor = int(rows[min(s + ENCODE_BATCH, len(rows)) - 1][0])
                store.meta_put(CURSOR_KEY, str(cursor))
        return added

    def search(self, text: str, k: int) -> list[tuple[int, float]]:
        """按意思最像的 k 行：[(line_id, 分数)]，分数从高到低。没有向量库或没有字时返回空。"""
        text = (text or "").strip()
        if not text or k <= 0:
            return []
        vs = self._store()
        if vs is None:
            return []
        q = np.asarray(self.encoder.encode([text]), dtype=np.float32).reshape(-1)
        return [(h.id, h.score) for h in vs.search(q, int(k))]
