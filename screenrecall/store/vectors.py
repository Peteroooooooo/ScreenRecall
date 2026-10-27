"""追加式向量库：独立文件、numpy 暴力检索（不用 ANN，实测 1M x 384 一次 28-40 ms 足够）。

目录结构（每种向量一个目录，例如 data/vectors/text-minilm/）：
- manifest.json：维度、每段行数、各段当前代号（压缩后代号 +1）、待删旧文件；
- sNNNNN.gG.vec / .ids / .ts / .grp：一段的数据，无文件头的小端 float32 / int64 原始数组，
  用 np.memmap 打开。没用 .npy 是因为 .npy 的头里写着行数，追加一行就得改头；原始数组直接追加即可；
- tombstones.i64：删除的 id（只追加）。

规则：
- 只有一个写入方（空闲整理进程；隐私删除时是主进程），写操作之间用 lock 文件互斥；
- 追加顺序 vec → ts → grp → ids，读取方的行数取四个文件的最小值，写到一半的行自然不可见；
- 删除：先追加墓碑，再把这些行的向量就地清零（隐私：向量是从文字算出来的，不能留在盘上）；
  读取方按墓碑屏蔽；compact() 把有死行的段重写成新一代文件，旧文件没人占用时删除；
- 读取方 refresh() 只读新增的行（看文件大小）和新增的墓碑，不整库重载；
- 检索：每段一次矩阵乘，时间范围按段的最早/最晚时间整段跳过，argpartition 取前 k，
  按组（如内容源）取每组最高分也全是向量化运算。向量应已归一化（余弦 = 点积）。
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

_KINDS = (("vec", np.float32), ("ts", np.int64), ("grp", np.int64), ("ids", np.int64))
_WRITE_ORDER = ("vec", "ts", "grp", "ids")


@dataclass(frozen=True)
class VectorHit:
    id: int
    score: float
    ts: int
    group: int

    def to_dict(self) -> dict:
        return {"id": self.id, "score": self.score, "ts": self.ts, "group": self.group}


class _Segment:
    """读取方缓存的一段：memmap + 存活掩码 + 时间范围。"""

    def __init__(self, index: int, gen: int):
        self.index = index
        self.gen = gen
        self.rows = 0
        self.vec: np.ndarray = np.zeros((0, 0), np.float32)
        self.ids = np.zeros(0, np.int64)
        self.ts = np.zeros(0, np.int64)
        self.grp = np.zeros(0, np.int64)
        self.alive = np.zeros(0, bool)
        self.ts_min = 0
        self.ts_max = -1


@contextmanager
def _file_lock(path: Path, timeout: float = 30.0) -> Iterator[None]:
    """跨进程互斥（Windows 用 msvcrt.locking，其他平台用 fcntl）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        deadline = time.monotonic() + timeout
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"向量库被占用：{path.parent}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _replace_retry(src: Path, dst: Path, tries: int = 50) -> None:
    """os.replace；目标正被别的进程短暂打开时（Windows 不共享删除）重试。"""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.02)


class VectorStore:
    def __init__(self, root: Path | str, dim: int = 384, *, segment_rows: int = 65536, readonly: bool = False):
        self.root = Path(root)
        self.readonly = readonly
        self.dim = int(dim)
        self.segment_rows = int(segment_rows)
        self._segments: dict[int, _Segment] = {}
        self._tomb_bytes = 0
        self._tombs = np.zeros(0, np.int64)
        self._manifest_mtime: float | None = None
        self._manifest: dict = {}
        if not readonly:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._lock():
                m = self._read_manifest()
                if m is None:
                    m = {"dim": self.dim, "segment_rows": self.segment_rows, "segments": {}, "garbage": []}
                    self._write_manifest(m)
                self._check_manifest(m)
                self._repair(m)
                self._collect_garbage(m)
        self.refresh()

    # ---------------- 文件与清单 ----------------

    def _lock(self):
        return _file_lock(self.root / "lock")

    def _path(self, index: int, gen: int, kind: str) -> Path:
        return self.root / f"s{index:05d}.g{gen}.{kind}"

    def _read_manifest(self) -> dict | None:
        p = self.root / "manifest.json"
        for _ in range(20):
            try:
                return json.loads(p.read_text("utf-8"))
            except FileNotFoundError:
                return None
            except (PermissionError, json.JSONDecodeError):
                time.sleep(0.02)
        return json.loads(p.read_text("utf-8"))

    def _write_manifest(self, m: dict) -> None:
        tmp = self.root / "manifest.json.tmp"
        tmp.write_text(json.dumps(m), "utf-8")
        _replace_retry(tmp, self.root / "manifest.json")

    def _check_manifest(self, m: dict) -> None:
        if int(m["dim"]) != self.dim:
            raise ValueError(f"向量库维度 {m['dim']} 与请求的 {self.dim} 不符：{self.root}")
        self.segment_rows = int(m["segment_rows"])

    def _file_rows(self, index: int, gen: int) -> int:
        rows = []
        for kind, dt in _KINDS:
            p = self._path(index, gen, kind)
            size = p.stat().st_size if p.exists() else 0
            width = self.dim * 4 if kind == "vec" else np.dtype(dt).itemsize
            rows.append(size // width)
        return min(rows)

    def _repair(self, m: dict) -> None:
        """上次追加写到一半退出：把最后一段的四个文件截到相同行数。"""
        if not m["segments"]:
            return
        index = max(int(k) for k in m["segments"])
        gen = int(m["segments"][str(index)])
        n = self._file_rows(index, gen)
        for kind, dt in _KINDS:
            p = self._path(index, gen, kind)
            width = self.dim * 4 if kind == "vec" else np.dtype(dt).itemsize
            if p.exists() and p.stat().st_size != n * width:
                with open(p, "r+b") as fh:
                    fh.truncate(n * width)

    def _collect_garbage(self, m: dict) -> None:
        left = []
        for name in m.get("garbage", []):
            try:
                (self.root / name).unlink(missing_ok=True)
            except PermissionError:  # 读取方还占着旧文件：下次再删（死行已清零）
                left.append(name)
        if left != m.get("garbage", []):
            m["garbage"] = left
            self._write_manifest(m)

    # ---------------- 写入 ----------------

    def append(self, ids: Sequence[int], vecs: np.ndarray, ts: Sequence[int], groups: Sequence[int] | None = None) -> int:
        """追加若干行；返回写入行数。ids 不应与已有的（含已删除的）重复。"""
        if self.readonly:
            raise RuntimeError("只读向量库不能写入")
        vecs = np.ascontiguousarray(vecs, dtype="<f4").reshape(-1, self.dim)
        ids_a = np.ascontiguousarray(ids, dtype="<i8").reshape(-1)
        ts_a = np.ascontiguousarray(ts, dtype="<i8").reshape(-1)
        grp_a = (np.zeros_like(ids_a) if groups is None
                 else np.ascontiguousarray(groups, dtype="<i8").reshape(-1))
        n = len(ids_a)
        if not (len(vecs) == len(ts_a) == len(grp_a) == n):
            raise ValueError("ids / vecs / ts / groups 行数不一致")
        if n == 0:
            return 0
        with self._lock():
            m = self._read_manifest()
            self._check_manifest(m)
            pos = 0
            while pos < n:
                if m["segments"]:
                    index = max(int(k) for k in m["segments"])
                    gen = int(m["segments"][str(index)])
                    have = self._file_rows(index, gen)
                else:
                    index, gen, have = -1, 0, self.segment_rows
                if have >= self.segment_rows:
                    index, gen, have = index + 1, 0, 0
                    m["segments"][str(index)] = 0
                    self._write_manifest(m)
                take = min(n - pos, self.segment_rows - have)
                part = {"vec": vecs[pos:pos + take], "ts": ts_a[pos:pos + take],
                        "grp": grp_a[pos:pos + take], "ids": ids_a[pos:pos + take]}
                for kind in _WRITE_ORDER:
                    with open(self._path(index, gen, kind), "ab") as fh:
                        fh.write(part[kind].tobytes())
                        fh.flush()
                pos += take
        return n

    def delete(self, ids: Sequence[int]) -> int:
        """墓碑 + 把对应行的向量就地清零。返回清零的行数。"""
        if self.readonly:
            raise RuntimeError("只读向量库不能删除")
        dead = np.unique(np.asarray(ids, dtype=np.int64).reshape(-1))
        if dead.size == 0:
            return 0
        zeroed = 0
        with self._lock():
            with open(self.root / "tombstones.i64", "ab") as fh:
                fh.write(dead.astype("<i8").tobytes())
                fh.flush()
            m = self._read_manifest()
            row_bytes = self.dim * 4
            zeros = bytes(row_bytes)
            for key, gen in m["segments"].items():
                index, gen = int(key), int(gen)
                n = self._file_rows(index, gen)
                if n == 0:
                    continue
                seg_ids = np.fromfile(self._path(index, gen, "ids"), dtype="<i8", count=n)
                rows = np.flatnonzero(np.isin(seg_ids, dead))
                if rows.size == 0:
                    continue
                with open(self._path(index, gen, "vec"), "r+b") as fh:
                    for r in rows:
                        fh.seek(int(r) * row_bytes)
                        fh.write(zeros)
                    fh.flush()
                    os.fsync(fh.fileno())
                zeroed += int(rows.size)
        return zeroed

    def compact(self, min_dead_fraction: float = 0.0) -> int:
        """把含死行的段重写为新一代（只保留活行）；返回重写的段数。最后一段（仍在追加）不动。"""
        if self.readonly:
            raise RuntimeError("只读向量库不能压缩")
        rewritten = 0
        with self._lock():
            m = self._read_manifest()
            tombs = self._load_all_tombs()
            if not m["segments"] or tombs.size == 0:
                return 0
            last = max(int(k) for k in m["segments"])
            for key in sorted(m["segments"], key=int):
                index, gen = int(key), int(m["segments"][key])
                if index == last:
                    continue
                n = self._file_rows(index, gen)
                data = {kind: np.fromfile(self._path(index, gen, kind), dtype=dt,
                                          count=n * (self.dim if kind == "vec" else 1))
                        for kind, dt in _KINDS}
                keep = ~np.isin(data["ids"], tombs)
                if keep.all() or (1 - keep.mean()) < min_dead_fraction:
                    continue
                new_gen = gen + 1
                vec = data["vec"].reshape(n, self.dim)[keep]
                for kind in _WRITE_ORDER:
                    arr = vec if kind == "vec" else data[kind][keep]
                    tmp = self._path(index, new_gen, kind).with_suffix(f".{kind}.tmp")
                    arr.astype("<f4" if kind == "vec" else "<i8").tofile(tmp)
                    _replace_retry(tmp, self._path(index, new_gen, kind))
                m["segments"][key] = new_gen
                m.setdefault("garbage", []).extend(self._path(index, gen, kind).name for kind, _ in _KINDS)
                self._write_manifest(m)
                rewritten += 1
            self._collect_garbage(m)
        self._segments.clear()  # 本进程自己的映射也换成新一代
        self._manifest_mtime = None
        self.refresh()
        if not self.readonly:
            with self._lock():
                m = self._read_manifest()
                self._collect_garbage(m)
        return rewritten

    def _load_all_tombs(self) -> np.ndarray:
        p = self.root / "tombstones.i64"
        if not p.exists():
            return np.zeros(0, np.int64)
        return np.unique(np.fromfile(p, dtype="<i8"))

    # ---------------- 读取 ----------------

    def refresh(self) -> None:
        """增量刷新：新清单代号、新增行、新增墓碑。"""
        mp = self.root / "manifest.json"
        try:
            mtime = mp.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime != self._manifest_mtime:
            m = self._read_manifest()
            if m is None:
                return
            self._check_manifest(m)
            self._manifest = m
            self._manifest_mtime = mtime
        new_tombs = self._read_new_tombs()
        for key, gen in self._manifest["segments"].items():
            index, gen = int(key), int(gen)
            seg = self._segments.get(index)
            if seg is None or seg.gen != gen:
                seg = self._segments[index] = _Segment(index, gen)
                fresh = True
            else:
                fresh = False
            n = self._file_rows(index, gen)
            if n > seg.rows:
                self._map(seg, n)
            elif new_tombs.size and not fresh and seg.rows:
                seg.alive &= ~np.isin(seg.ids, new_tombs)
        for index in [i for i in self._segments if str(i) not in self._manifest["segments"]]:
            del self._segments[index]

    def _read_new_tombs(self) -> np.ndarray:
        p = self.root / "tombstones.i64"
        if not p.exists():
            return np.zeros(0, np.int64)
        size = p.stat().st_size // 8 * 8
        if size <= self._tomb_bytes:
            return np.zeros(0, np.int64)
        with open(p, "rb") as fh:
            fh.seek(self._tomb_bytes)
            new = np.frombuffer(fh.read(size - self._tomb_bytes), dtype="<i8").astype(np.int64)
        self._tomb_bytes = size
        self._tombs = np.union1d(self._tombs, new)
        return new

    def _map(self, seg: _Segment, n: int) -> None:
        def mm(kind: str, dt, shape):
            return np.memmap(self._path(seg.index, seg.gen, kind), dtype=dt, mode="r", shape=shape)

        seg.vec = mm("vec", "<f4", (n, self.dim))
        seg.ids = np.asarray(mm("ids", "<i8", (n,)))
        seg.ts = np.asarray(mm("ts", "<i8", (n,)))
        seg.grp = np.asarray(mm("grp", "<i8", (n,)))
        seg.alive = ~np.isin(seg.ids, self._tombs) if self._tombs.size else np.ones(n, bool)
        seg.rows = n
        seg.ts_min = int(seg.ts.min()) if n else 0
        seg.ts_max = int(seg.ts.max()) if n else -1

    def __len__(self) -> int:
        return sum(int(s.alive.sum()) for s in self._segments.values())

    def close(self) -> None:
        self._segments.clear()

    def search(
        self,
        query: np.ndarray,
        k: int = 50,
        *,
        time_from: int | None = None,
        time_to: int | None = None,
        ids: Sequence[int] | np.ndarray | None = None,
        groups: Sequence[int] | np.ndarray | None = None,
        best_per_group: bool = False,
        refresh: bool = True,
    ) -> list[VectorHit]:
        """余弦相似度前 k。time 为 [time_from, time_to)；ids / groups 限定候选；
        best_per_group=True 时每组只留最高分的一行，返回前 k 个组。"""
        if refresh:
            self.refresh()
        q = np.asarray(query, dtype=np.float32).reshape(-1)
        if q.size != self.dim:
            raise ValueError(f"查询向量维度 {q.size} 与库 {self.dim} 不符")
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        id_filter = None if ids is None else np.unique(np.asarray(ids, dtype=np.int64))
        grp_filter = None if groups is None else np.unique(np.asarray(groups, dtype=np.int64))
        parts_s, parts_i, parts_t, parts_g = [], [], [], []
        for seg in self._segments.values():
            if seg.rows == 0:
                continue
            if time_from is not None and seg.ts_max < time_from:
                continue
            if time_to is not None and seg.ts_min >= time_to:
                continue
            mask = seg.alive
            if time_from is not None and seg.ts_min < time_from:
                mask = mask & (seg.ts >= time_from)
            if time_to is not None and seg.ts_max >= time_to:
                mask = mask & (seg.ts < time_to)
            if id_filter is not None:
                mask = mask & np.isin(seg.ids, id_filter, assume_unique=False)
            if grp_filter is not None:
                mask = mask & np.isin(seg.grp, grp_filter)
            sel = np.flatnonzero(mask)
            if sel.size == 0:
                continue
            if sel.size > seg.rows // 4:
                scores = (seg.vec @ q)[sel]
            else:
                scores = seg.vec[sel] @ q
            if not best_per_group and sel.size > k:
                top = np.argpartition(-scores, k - 1)[:k]
                sel, scores = sel[top], scores[top]
            parts_s.append(scores)
            parts_i.append(seg.ids[sel])
            parts_t.append(seg.ts[sel])
            parts_g.append(seg.grp[sel])
        if not parts_s:
            return []
        s = np.concatenate(parts_s)
        ii = np.concatenate(parts_i)
        tt = np.concatenate(parts_t)
        gg = np.concatenate(parts_g)
        if best_per_group:
            order = np.lexsort((-s, gg))  # 先按组，组内分数从高到低
            first = np.ones(order.size, bool)
            first[1:] = gg[order][1:] != gg[order][:-1]
            pick = order[first]
            s, ii, tt, gg = s[pick], ii[pick], tt[pick], gg[pick]
        if s.size > k:
            top = np.argpartition(-s, k - 1)[:k]
        else:
            top = np.arange(s.size)
        top = top[np.lexsort((ii[top], -s[top]))]  # 分数降序，同分按 id
        return [VectorHit(int(ii[j]), float(s[j]), int(tt[j]), int(gg[j])) for j in top]
