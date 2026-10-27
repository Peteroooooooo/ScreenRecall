"""第 2、3 步：文字向量（每行一个 + 相邻几行一个短段落）与按文字重叠合并块。

文字向量（布局 2，向量库 <data_dir>/vectors/text2，见 searchsvc.text_vectors_dir）
- 新行（line_id 大于游标 worker.text_line_id）每行一个向量：id = line_id，ts = first_seen_ms，
  group = segment_id（store 约定）；很短的行（"}"、"12:30"、"收到"，见 line_worth_a_vector）不单独算。
- 另外同一块里阅读顺序相邻的 2 到 chunk_lines 行（不超过 chunk_tokens 个 token，纵向隔得太远的不拼）
  算一个短段落向量，补上一句话折成几行时的上下文：id = 新的负数编号（meta worker.text_chunk_seq），
  ts = 段落里最早的 first_seen_ms；成员登记在 text_chunks（store.queries 的向量分支靠它把 id 换回这几行）。
  评测（experiments/search）：6 行一段时近义 / 跨语言的 found@5 只有 0.26 / 0.35，长段落把一句话的意思冲淡了；
  每行一个 + 3 行短段落是 0.9 左右。
- 顺序：算向量 → 一个写操作里登记成员 + 推进游标 → 追加向量 → 核对行和成员还在（期间被隐私删除就把向量清掉）。
  崩溃最多丢这一批的向量，不会留下被删文字算出来的向量。
- 隐私删除（store.delete）：被删行自己的向量（id = line_id）和含被删行的段落向量都删掉，段落里剩下的行登记
  text_rechunk 待办；这里给剩下的行重新拼段落（新的负数编号，不复用已删除的 id：向量库按墓碑屏蔽）。
  剩下的行自己的向量只含它自己的字，不用重算。
- 迁移：老布局（6 行一段、id = 段落第一行的 line_id，向量库 vectors/text）在第一次运行时整体作废：
  老向量文件就地清零后删除目录，清空 text_chunks 和老的重组待办，游标归零，所有行按新布局重算。

合并块
- 同一内容源里两个块（各自换算到根块坐标）有 >= 3 行连续相同（规范化文字）、总字数够、
  y 差一致（不超过 match_dy_px）→ 新块并入旧块：writer.block_merge(新, 旧, y 差的中位数)；
  两边都有的行，新块那份记成旧块那份的别名（store.aliases，检索和阅读视图只显示一份）；
- 增量：只看 line_id 大于游标 worker.merge_line_id 的新行所在的内容源；这些新行落在合并树里、和别的块重复的
  也记成别名（writer.alias_new_lines）。
"""

from __future__ import annotations

import os
import shutil
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from ..store.fts.docs import is_cjk
from ..store.queries import _block_roots
from ..store.vectors import VectorStore
from .searchsvc import legacy_text_vectors_dir, text_vectors_dir
from .work_common import Ctx, Stopped, log, timed

TEXT_CURSOR = "worker.text_line_id"
CHUNK_SEQ = "worker.text_chunk_seq"
TEXT_LAYOUT = "worker.text_layout"
LAYOUT = 2
MERGE_CURSOR = "worker.merge_line_id"
TEXT_DIM = 384
LINE_BATCH = 1000
ENCODE_BATCH = 64
MIN_LINE_LETTERS = 6


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


def line_worth_a_vector(text: str) -> bool:
    """单独算向量的行：字母至少 MIN_LINE_LETTERS 个（汉字、假名、谚文一个算两个）。
    很短的行（"}"、"12:30"、"OK 👍"、"收到"、聊天里的"小王 10:02"）单独算出来的向量和什么都有点像，
    只会给不相关的查询凑命中（合成评测里 "收到" 是好几个查询的头号凑数命中）；它们仍在段落向量里。"""
    return sum(2 if is_cjk(ch) else 1 for ch in text if ch.isalpha()) >= MIN_LINE_LETTERS


def make_units(rows: Sequence, *, max_lines: int = 3, max_tokens: int = 100,
               count_tokens: Callable[[str], int] | None = None) -> tuple[list[Chunk], list[Chunk]]:
    """新行 → (单行向量, 段落向量)。单行：id = line_id；段落（2 行及以上）：id 留给写线程分配负数编号。"""
    lines = [_chunk([r]) for r in sorted(rows, key=lambda r: int(r["line_id"]))
             if str(r["text"]).strip() and line_worth_a_vector(str(r["text"]))]
    chunks = [c for c in make_chunks(rows, max_lines=max_lines, max_tokens=max_tokens, count_tokens=count_tokens)
              if len(c.line_ids) >= 2]
    return lines, chunks


def _put_units(conn, lines: list[int], chunks: list[list[int]], cursor: tuple[str, int] | None,
               job_id: int | None, now: int, old_chunk: int | None = None) -> dict:
    """写线程里：为段落分配新的负数编号并登记成员，推进游标 / 完成待办（同一个事务）。
    old_chunk：重组时先去掉旧段落剩下的成员登记（只剩一行的不再算段落）。
    行或成员已被删除（这期间发生了隐私删除）的不追加向量；段落只剩部分成员时登记重组待办。
    返回 {'chunk_ids': 每段的编号, 'chunks_ok': 可以追加的段落下标, 'lines_ok': 可以追加的单行下标}。"""
    if old_chunk is not None:
        conn.execute("DELETE FROM text_chunks WHERE chunk_id = ?", (int(old_chunk),))
    wanted = sorted({int(x) for x in lines} | {int(m) for c in chunks for m in c})
    present: set[int] = set()
    for s in range(0, len(wanted), 500):
        part = wanted[s:s + 500]
        ph = ", ".join("?" * len(part))
        present |= {int(x[0]) for x in conn.execute(f"SELECT line_id FROM lines WHERE line_id IN ({ph})", part)}
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (CHUNK_SEQ,)).fetchone()
    seq = int(r[0]) if r else 0
    chunk_ids: list[int] = []
    chunks_ok: list[int] = []
    for i, members in enumerate(chunks):
        seq += 1
        cid = -seq
        chunk_ids.append(cid)
        keep = [m for m in members if m in present]
        if len(keep) == len(members):
            chunks_ok.append(i)
        if len(keep) >= 2:
            conn.executemany("INSERT OR REPLACE INTO text_chunks (line_id, chunk_id) VALUES (?, ?)",
                             [(m, cid) for m in keep])
            if len(keep) < len(members):
                conn.execute(
                    "INSERT INTO jobs (kind, target, created_ms, updated_ms) VALUES ('text_rechunk', ?, ?, ?) "
                    "ON CONFLICT (kind, target) DO UPDATE SET status = 'pending', attempts = 0, not_before_ms = 0, "
                    "updated_ms = excluded.updated_ms", (str(cid), now, now))
    Ctx.meta_put_sql(conn, CHUNK_SEQ, seq)
    if cursor is not None:
        Ctx.meta_put_sql(conn, *cursor)
    if job_id is not None:
        conn.execute("UPDATE jobs SET status = 'done', last_error = NULL, updated_ms = ? WHERE job_id = ?",
                     (now, job_id))
    return {"chunk_ids": chunk_ids, "chunks_ok": chunks_ok,
            "lines_ok": [i for i, lid in enumerate(lines) if int(lid) in present]}


def _reset_layout(conn) -> None:
    """迁移到布局 2（写线程里一个事务）：老段落登记和老的重组待办作废，游标归零，记下布局版本。"""
    conn.execute("DELETE FROM text_chunks")
    conn.execute("DELETE FROM jobs WHERE kind = 'text_rechunk'")
    Ctx.meta_put_sql(conn, TEXT_CURSOR, 0)
    Ctx.meta_put_sql(conn, TEXT_LAYOUT, LAYOUT)


def wipe_vector_dir(root: Path) -> bool:
    """把一个向量库目录里的向量文件就地写零，再整个删掉。返回目录是否已经不在了。
    删不掉（别的进程还映射着、拿着锁）也没关系：向量已经是零，下次再删。"""
    if not root.exists():
        return True
    for p in root.glob("*.vec"):
        try:
            size = p.stat().st_size
            with open(p, "r+b") as fh:
                left = size
                zeros = bytes(min(size, 1 << 20))
                while left > 0:
                    n = min(left, len(zeros))
                    fh.write(zeros[:n])
                    left -= n
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.warning("清零老向量文件失败：%s", type(exc).__name__)
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


class TextVectorStep:
    name = "text"

    def __init__(self, ctx: Ctx, encoder_factory: Callable[[], object],
                 vectors_factory: Callable[[], VectorStore] | None = None):
        self.ctx = ctx
        self._encoder_factory = encoder_factory
        self._vectors_factory = vectors_factory or (
            lambda: VectorStore(text_vectors_dir(ctx.data_dir), TEXT_DIM))
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
        if not self._migrate():
            return 0
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

    # ---------------- 布局迁移 ----------------

    def _migrate(self) -> bool:
        """老布局的向量作废、按新布局从头算（见模块说明）。返回能否接着算向量。"""
        ctx = self.ctx
        legacy = legacy_text_vectors_dir(ctx.data_dir)
        if ctx.meta_get(TEXT_LAYOUT) >= LAYOUT:
            if legacy.exists():
                wipe_vector_dir(legacy)  # 上次没删掉（被占用）：再试
            return True
        # 先把老向量清零（之后它们对应的段落登记就没了，隐私删除再也找不到它们）
        wipe_vector_dir(legacy)
        new = text_vectors_dir(ctx.data_dir)
        if new.exists():  # 迁移前就有新布局的目录（不该发生）：它的 id 和墓碑对不上新的登记，删掉重来
            self.close()
            if not wipe_vector_dir(new):
                ctx.once("text-layout-busy", "文字向量目录被占用，下次空闲再迁移")
                return False
        ctx.writer.call(_reset_layout).result(60)
        log.info("文字向量迁移到布局 %d：所有行重新计算", LAYOUT)
        return True

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
        lines, chunks = make_units(rows, max_lines=cfg.chunk_lines, max_tokens=cfg.chunk_tokens,
                                   count_tokens=self._count(enc))
        vecs = self._encode(enc, [u.text for u in lines] + [c.text for c in chunks])
        res = ctx.writer.call(_put_units, [u.id for u in lines], [c.line_ids for c in chunks],
                              (TEXT_CURSOR, int(rows[-1]["line_id"])), None, ctx.clock()).result(60)
        self._append(lines, chunks, vecs, res)
        st.add(len(rows))
        st.extra["line_vectors"] = st.extra.get("line_vectors", 0) + len(res["lines_ok"])
        st.extra["chunks"] = st.extra.get("chunks", 0) + len(res["chunks_ok"])
        return len(rows)

    def _append(self, lines: list[Chunk], chunks: list[Chunk], vecs: np.ndarray, res: dict) -> None:
        """vecs：先 lines 后 chunks。追加可以追加的，然后核对：追加前后若有隐私删除，
        行没了 / 段落成员变少 —— 刚追加的向量就地清零（读取方本来就按墓碑屏蔽，但盘上不能留）。"""
        n_lines = len(lines)
        li, ci = res["lines_ok"], res["chunks_ok"]
        ids = [lines[i].id for i in li] + [res["chunk_ids"][i] for i in ci]
        if not ids:
            return
        rows = li + [n_lines + i for i in ci]
        units = [lines[i] for i in li] + [chunks[i] for i in ci]
        self.vs.append(ids, vecs[rows], [u.ts for u in units], [u.segment_id for u in units])
        conn = self.ctx.conn
        dead: list[int] = []
        line_ids = [lines[i].id for i in li]
        for s in range(0, len(line_ids), 500):
            part = line_ids[s:s + 500]
            ph = ", ".join("?" * len(part))
            have = {int(r[0]) for r in conn.execute(f"SELECT line_id FROM lines WHERE line_id IN ({ph})", part)}
            dead += [x for x in part if x not in have]
        want = {res["chunk_ids"][i]: len(chunks[i].line_ids) for i in ci}
        cids = list(want)
        for s in range(0, len(cids), 500):
            part = cids[s:s + 500]
            ph = ", ".join("?" * len(part))
            have_n = {int(r[0]): int(r[1]) for r in conn.execute(
                f"SELECT chunk_id, COUNT(*) FROM text_chunks WHERE chunk_id IN ({ph}) GROUP BY chunk_id", part)}
            dead += [c for c in part if have_n.get(c, 0) != want[c]]
        if dead:
            log.info("%d 个文字向量在计算期间内容被删除，已清掉", len(dead))
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
        # 剩下的行自己的单行向量没受影响；只重拼段落（只剩一行的不再算段落）
        chunks = [c for c in make_chunks(rows, max_lines=cfg.chunk_lines, max_tokens=cfg.chunk_tokens,
                                         count_tokens=self._count(enc)) if len(c.line_ids) >= 2]
        vecs = self._encode(enc, [c.text for c in chunks])
        res = ctx.writer.call(_put_units, [], [c.line_ids for c in chunks], None, int(job["job_id"]),
                              ctx.clock(), old).result(60)
        self._append([], chunks, vecs, res)
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
