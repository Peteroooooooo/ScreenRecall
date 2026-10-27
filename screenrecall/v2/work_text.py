"""第 2、3 步：文字向量（按短段落）与按文字重叠合并块。

文字向量
- 新行（line_id 大于游标 worker.text_line_id）按块分组、块内按阅读顺序，相邻的最多 chunk_lines 行 /
  chunk_tokens 个 token 拼成一个段落；纵向隔得太远的行不拼。一个段落一个向量：
  id = 段落第一行的 line_id，ts = 段落里最早的 first_seen_ms，group = segment_id（store 约定）；
  段落成员登记在 text_chunks（store.queries 的向量分支靠它把 id 换回这几行）。
- 顺序：算向量 → 一个写操作里登记成员 + 推进游标 → 追加向量 → 核对成员还在（期间被隐私删除就把向量清掉）。
  崩溃最多丢这一批的向量，不会留下被删文字算出来的向量。
- 隐私删除删掉段落里的某几行时（store.delete），整段向量被删，剩下的行登记 text_rechunk 待办；
  这里用新的负数编号（meta worker.text_chunk_seq）重算，不复用已删除的 id（向量库按墓碑屏蔽）。

合并块
- 同一内容源里两个块（各自换算到根块坐标）有 >= 3 行连续相同（规范化文字）、总字数够、
  y 差一致（不超过 match_dy_px）→ 新块并入旧块：writer.block_merge(新, 旧, y 差的中位数)；
  两边都有的行，新块那份记成旧块那份的别名（store.aliases，检索和阅读视图只显示一份）；
- 增量：只看 line_id 大于游标 worker.merge_line_id 的新行所在的内容源；这些新行落在合并树里、和别的块重复的
  也记成别名（writer.alias_new_lines）。
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from ..store.queries import _block_roots
from ..store.vectors import VectorStore
from .work_common import Ctx, Stopped, log, timed

TEXT_CURSOR = "worker.text_line_id"
CHUNK_SEQ = "worker.text_chunk_seq"
MERGE_CURSOR = "worker.merge_line_id"
TEXT_DIM = 384
LINE_BATCH = 1000
ENCODE_BATCH = 64


@dataclass
class Chunk:
    line_ids: list[int]
    text: str
    ts: int
    segment_id: int
    block_id: int
    id: int = 0


def make_chunks(rows: Sequence, *, max_lines: int = 6, max_tokens: int = 100,
                count_tokens: Callable[[str], int] | None = None) -> list[Chunk]:
    """rows：带 line_id / block_id / segment_id / text / order_key / y0 / y1 / first_seen_ms 的行。
    按块分组、块内按阅读顺序切成段落；id 先填第一行的 line_id。"""
    count = count_tokens or (lambda s: max(1, len(s) // 2))
    by_block: dict[int, list] = defaultdict(list)
    for r in rows:
        if str(r["text"]).strip():
            by_block[int(r["block_id"])].append(r)
    out: list[Chunk] = []
    for bid in sorted(by_block):
        items = sorted(by_block[bid], key=lambda r: (float(r["order_key"]), int(r["line_id"])))
        cur: list = []
        tokens = 0
        prev = None
        for r in items:
            t = max(1, int(count(str(r["text"]))))
            far = False
            if prev is not None:
                h = max(8.0, float(prev["y1"]) - float(prev["y0"]))
                far = float(r["y0"]) - float(prev["y1"]) > max(60.0, 2.5 * h)
            if cur and (len(cur) >= max_lines or tokens + t > max_tokens or far):
                out.append(_chunk(cur))
                cur, tokens = [], 0
            cur.append(r)
            tokens += t
            prev = r
        if cur:
            out.append(_chunk(cur))
    return out


def _chunk(rows: list) -> Chunk:
    return Chunk(line_ids=[int(r["line_id"]) for r in rows], text="\n".join(str(r["text"]).strip() for r in rows),
                 ts=min(int(r["first_seen_ms"]) for r in rows), segment_id=int(rows[0]["segment_id"]),
                 block_id=int(rows[0]["block_id"]), id=int(rows[0]["line_id"]))


def _put_chunks(conn, chunks: list[tuple[int, list[int]]], allocate: bool, cursor: tuple[str, int] | None,
                job_id: int | None, now: int) -> dict:
    """写线程里：登记段落成员。allocate=True 时为每段分配新的负数编号。
    成员行已被删除（这期间发生了隐私删除）的段落不追加向量，剩下的行登记重组待办。
    返回 {'ids': 每段的编号, 'complete': 可以追加向量的段落下标}。"""
    ids: list[int] = []
    complete: list[int] = []
    seq = 0
    if allocate:
        r = conn.execute("SELECT value FROM meta WHERE key = ?", (CHUNK_SEQ,)).fetchone()
        seq = int(r[0]) if r else 0
    for i, (cid, members) in enumerate(chunks):
        if allocate:
            seq += 1
            cid = -seq
        ids.append(cid)
        ph = ", ".join("?" * len(members))
        present = {int(x[0]) for x in conn.execute(f"SELECT line_id FROM lines WHERE line_id IN ({ph})", members)}
        keep = [m for m in members if m in present]
        if keep:
            conn.executemany("INSERT OR REPLACE INTO text_chunks (line_id, chunk_id) VALUES (?, ?)",
                             [(m, cid) for m in keep])
        if len(keep) == len(members):
            complete.append(i)
        elif keep:
            conn.execute(
                "INSERT INTO jobs (kind, target, created_ms, updated_ms) VALUES ('text_rechunk', ?, ?, ?) "
                "ON CONFLICT (kind, target) DO UPDATE SET status = 'pending', attempts = 0, not_before_ms = 0, "
                "updated_ms = excluded.updated_ms", (str(cid), now, now))
    if allocate:
        Ctx.meta_put_sql(conn, CHUNK_SEQ, seq)
    if cursor is not None:
        Ctx.meta_put_sql(conn, *cursor)
    if job_id is not None:
        conn.execute("UPDATE jobs SET status = 'done', last_error = NULL, updated_ms = ? WHERE job_id = ?",
                     (now, job_id))
    return {"ids": ids, "complete": complete}


class TextVectorStep:
    name = "text"

    def __init__(self, ctx: Ctx, encoder_factory: Callable[[], object],
                 vectors_factory: Callable[[], VectorStore] | None = None):
        self.ctx = ctx
        self._encoder_factory = encoder_factory
        self._vectors_factory = vectors_factory or (
            lambda: VectorStore(ctx.data_dir / "vectors" / "text", TEXT_DIM))
        self._enc = None
        self._enc_failed = False
        self._vs: VectorStore | None = None

    def close(self) -> None:
        if self._vs is not None:
            self._vs.close()
            self._vs = None
        self._enc = None

    @property
    def vs(self) -> VectorStore:
        if self._vs is None:
            self._vs = self._vectors_factory()
        return self._vs

    def _encoder(self):
        if self._enc is None and not self._enc_failed:
            try:
                self._enc = self._encoder_factory()
            except Exception as exc:  # noqa: BLE001 —— EncoderModelMissing、onnxruntime 问题
                self._enc_failed = True
                self.ctx.once("encoder-missing", "文本向量模型不可用，跳过文字向量：%s", exc)
        return self._enc

    def _count(self, enc) -> Callable[[str], int]:
        fn = getattr(enc, "count_tokens", None)
        if fn is None:
            return lambda s: max(1, len(s) // 2)
        return lambda s: max(1, int(fn(s)) - 2)  # count_tokens 含首尾两个特殊符号

    def _encode(self, enc, texts: list[str]) -> np.ndarray:
        out = []
        for s in range(0, len(texts), ENCODE_BATCH):
            self.ctx.stop.check()
            out.append(np.asarray(enc.encode(texts[s:s + ENCODE_BATCH]), dtype=np.float32).reshape(-1, TEXT_DIM))
        return np.concatenate(out) if out else np.zeros((0, TEXT_DIM), np.float32)

    def backlog(self) -> int:
        cur = self.ctx.meta_get(TEXT_CURSOR)
        n = self.ctx.conn.execute("SELECT COUNT(*) FROM lines WHERE line_id > ?", (cur,)).fetchone()[0]
        j = self.ctx.conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'text_rechunk' "
                                  "AND status = 'pending'").fetchone()[0]
        return int(n) + int(j)

    def run(self) -> int:
        if self.backlog() == 0:
            return 0
        enc = self._encoder()
        if enc is None:
            return 0
        done = 0
        with timed(self.ctx, self.name) as st:
            done += self._rechunk(enc, st)
            while not self.ctx.stop.is_set():
                n = self._new_lines(enc, st)
                if n == 0:
                    break
                done += n
        return done

    # ---------------- 新行 ----------------

    def _new_lines(self, enc, st) -> int:
        ctx = self.ctx
        cur = ctx.meta_get(TEXT_CURSOR)
        rows = ctx.conn.execute(
            "SELECT line_id, block_id, segment_id, text, order_key, y0, y1, first_seen_ms FROM lines "
            "WHERE line_id > ? ORDER BY line_id LIMIT ?", (cur, LINE_BATCH)).fetchall()
        if not rows:
            return 0
        cfg = ctx.cfg.idle
        chunks = make_chunks(rows, max_lines=cfg.chunk_lines, max_tokens=cfg.chunk_tokens,
                             count_tokens=self._count(enc))
        vecs = self._encode(enc, [c.text for c in chunks])
        res = ctx.writer.call(_put_chunks, [(c.id, c.line_ids) for c in chunks], False,
                              (TEXT_CURSOR, int(rows[-1]["line_id"])), None, ctx.clock()).result(60)
        self._append(chunks, vecs, res)
        st.add(len(rows))
        st.extra["chunks"] = st.extra.get("chunks", 0) + len(res["complete"])
        return len(rows)

    def _append(self, chunks: list[Chunk], vecs: np.ndarray, res: dict) -> None:
        ok = res["complete"]
        if not ok:
            return
        ids = [res["ids"][i] for i in ok]
        self.vs.append(ids, vecs[ok], [chunks[i].ts for i in ok], [chunks[i].segment_id for i in ok])
        # 核对：追加前后若有隐私删除，成员数会变少 —— 这段向量就地清零
        want = {res["ids"][i]: len(chunks[i].line_ids) for i in ok}
        ph = ", ".join("?" * len(ids))
        have = {int(r[0]): int(r[1]) for r in self.ctx.conn.execute(
            f"SELECT chunk_id, COUNT(*) FROM text_chunks WHERE chunk_id IN ({ph}) GROUP BY chunk_id", ids)}
        dead = [cid for cid, n in want.items() if have.get(cid, 0) != n]
        if dead:
            log.info("%d 段文字在算向量期间被删除，已清掉对应向量", len(dead))
            self.vs.delete(dead)

    # ---------------- 隐私删除后的重组 ----------------

    def _rechunk(self, enc, st) -> int:
        ctx = self.ctx
        done = 0
        while not ctx.stop.is_set():
            jobs = ctx.writer.job_claim(["text_rechunk"], limit=20).result(30)
            if not jobs:
                break
            for i, j in enumerate(jobs):
                if ctx.stop.is_set():
                    _unclaim(ctx, [x["job_id"] for x in jobs[i:]])
                    raise Stopped()
                try:
                    done += self._rechunk_one(enc, j)
                except Stopped:
                    _unclaim(ctx, [x["job_id"] for x in jobs[i:]])
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("段落重组失败：%s", type(exc).__name__)
                    ctx.writer.job_fail(j["job_id"], f"{type(exc).__name__}: {exc}").result(30)
        st.extra["rechunked"] = st.extra.get("rechunked", 0) + done
        return done

    def _rechunk_one(self, enc, job: dict) -> int:
        ctx = self.ctx
        old = int(job["target"])
        self.vs.delete([old])  # 删除方已删过；再删一次保证旧段落的向量不留在盘上
        rows = ctx.conn.execute(
            "SELECT l.line_id, l.block_id, l.segment_id, l.text, l.order_key, l.y0, l.y1, l.first_seen_ms "
            "FROM text_chunks c JOIN lines l ON l.line_id = c.line_id WHERE c.chunk_id = ?", (old,)).fetchall()
        if not rows:
            ctx.writer.job_done(job["job_id"]).result(30)
            return 0
        cfg = ctx.cfg.idle
        chunks = make_chunks(rows, max_lines=cfg.chunk_lines, max_tokens=cfg.chunk_tokens,
                             count_tokens=self._count(enc))
        vecs = self._encode(enc, [c.text for c in chunks])
        res = ctx.writer.call(_put_chunks, [(c.id, c.line_ids) for c in chunks], True, None,
                              int(job["job_id"]), ctx.clock()).result(60)
        self._append(chunks, vecs, res)
        return len(rows)


def _unclaim(ctx: Ctx, job_ids: list[int]) -> None:
    def op(conn, ids):
        conn.executemany("UPDATE jobs SET status = 'pending', attempts = max(0, attempts - 1) "
                         "WHERE job_id = ? AND status = 'running'", [(int(i),) for i in ids])
    ctx.writer.call(op, job_ids).result(10)


# ======================= 合并块 =======================

def find_overlap(dst: Sequence[tuple[str, float]], src: Sequence[tuple[str, float]], *, min_run: int = 3,
                 min_chars: int = 12, tol: float = 8.0, max_repeat: int = 20) -> int | None:
    """dst / src：(规范化文字, y0) 按阅读顺序。找最长的连续相同段（>= min_run 行、总字数 >= min_chars、
    各行 y 差的极差 <= tol），返回 offset = y_dst - y_src 的中位数（取整）；没有返回 None。"""
    pos: dict[str, list[int]] = defaultdict(list)
    for j, (n, _) in enumerate(dst):
        pos[n].append(j)
    best: tuple[int, int, int] | None = None  # (行数, 字数, offset)
    for i, (n, _) in enumerate(src):
        hits = pos.get(n)
        if not hits or len(hits) > max_repeat:
            continue
        for j in hits:
            if i > 0 and j > 0 and src[i - 1][0] == dst[j - 1][0]:
                continue  # 不是一段的开头
            k = 0
            while i + k < len(src) and j + k < len(dst) and src[i + k][0] == dst[j + k][0]:
                k += 1
            if k < min_run:
                continue
            chars = sum(len(src[i + t][0]) for t in range(k))
            if chars < min_chars:
                continue
            diffs = [dst[j + t][1] - src[i + t][1] for t in range(k)]
            if max(diffs) - min(diffs) > tol:
                continue
            cand = (k, chars, int(round(statistics.median(diffs))))
            if best is None or cand[:2] > best[:2]:
                best = cand
    return None if best is None else best[2]


class MergeStep:
    name = "merge"
    max_roots = 40
    max_lines = 3000

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def backlog(self) -> int:
        cur = self.ctx.meta_get(MERGE_CURSOR)
        r = self.ctx.conn.execute("SELECT MAX(line_id) FROM lines").fetchone()[0]
        return 0 if r is None or int(r) <= cur else int(r) - cur

    def run(self) -> int:
        ctx = self.ctx
        cur = ctx.meta_get(MERGE_CURSOR)
        top = ctx.conn.execute("SELECT MAX(line_id) FROM lines").fetchone()[0]
        if top is None or int(top) <= cur:
            return 0
        merged = 0
        with timed(ctx, self.name) as st:
            segs = [int(r[0]) for r in ctx.conn.execute(
                "SELECT DISTINCT segment_id FROM lines WHERE line_id > ? AND line_id <= ?", (cur, int(top)))]
            for sid in segs:
                ctx.stop.check()
                merged += self._segment(sid)
                # 合并之后录制进程还往并入的块里写的新行：与同一棵合并树里别的块重复的记成别名
                st.extra["aliases"] = st.extra.get("aliases", 0) + int(
                    ctx.writer.alias_new_lines(sid, cur).result(30) or 0)
            ctx.meta_put(MERGE_CURSOR, int(top))
            st.add(merged)
            st.extra["segments"] = st.extra.get("segments", 0) + len(segs)
        return merged

    def _segment(self, sid: int) -> int:
        conn = self.ctx.conn
        roots_of = _block_roots(conn, sid)
        info = {int(r[0]): (int(r[1]), r[2]) for r in conn.execute(
            "SELECT block_id, created_ms, anchor FROM blocks WHERE segment_id = ? AND merged_into IS NULL "
            "ORDER BY created_ms DESC, block_id DESC LIMIT ?", (sid, self.max_roots))}
        if len(info) < 2:
            return 0
        seqs: dict[int, list[tuple[str, float, float]]] = {b: [] for b in info}
        for bid, (root, off, _, _) in roots_of.items():
            if root not in seqs:
                continue
            for r in conn.execute(
                "SELECT norm, y0, x0 FROM lines WHERE block_id = ? AND disappeared_ms IS NULL "
                "ORDER BY order_key LIMIT ?", (bid, self.max_lines)):
                seqs[root].append((str(r[0]), float(r[1]) + off, float(r[2])))
        for s in seqs.values():
            s.sort(key=lambda x: (x[1], x[2]))
        tol = float(self.ctx.cfg.writer.match_dy_px)
        merged = 0
        order = sorted(info, key=lambda b: (info[b][0], b))  # 旧 → 新
        changed = True
        while changed and len(order) > 1:
            changed = False
            for jj in range(len(order) - 1, 0, -1):
                src = order[jj]
                for ii in range(jj):
                    dst = order[ii]
                    a = [(n, y) for n, y, _ in seqs[dst]]
                    b = [(n, y) for n, y, _ in seqs[src]]
                    off = find_overlap(a, b, tol=tol)
                    if off is None:
                        continue
                    if info[src][1] == info[dst][1] == "top" and abs(off) > tol:
                        continue  # 两块都贴着顶端：位置应当重合，对不上就不是同一段内容
                    try:
                        self.ctx.writer.block_merge(src, dst, off).result(30)
                    except Exception as exc:  # noqa: BLE001 —— 并发删除、环
                        log.info("块 %d 并入 %d 失败：%s", src, dst, exc)
                        continue
                    seqs[dst] = sorted(seqs[dst] + [(n, y + off, x) for n, y, x in seqs[src]],
                                       key=lambda x: (x[1], x[2]))
                    del seqs[src]
                    order.remove(src)
                    merged += 1
                    changed = True
                    break
                if changed:
                    break
        return merged
