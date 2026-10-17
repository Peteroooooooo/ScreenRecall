"""受控数据集的采集清单、检查、导入与标注表（方案 6.1；流程见 datasets/COLLECTION.md）：
python -m screenrecall.collect <子命令>

  manifest  为一个任务文件夹生成 / 刷新 manifest.csv（逐张截图一行；已填写的列保留）
  check     检查全部清单：必填字段、图片可解码、分辨率、完全重复、近重复（跨人跨任务）、各类数量
  import    把检查通过、keep=yes 的截图按任务导入数据目录（每个任务一个导入批次，带应用 / 标题 / 时间）
  labels    从数据目录生成标注表 CSV（dataset.py 的列 + 采集者、图片编号、事实编号、OCR 字数），已填的标注保留
  facts     列出每个事实编号对应的 frame_id；给 --queries 时把查询里 facts 对应的帧写进 relevant

数据集目录（不进 git，截图和清单都放这里）：
  <root>/tasks.csv、<root>/<采集者>/facts.csv、<root>/<采集者>/<任务编号>/{截图原件, manifest.csv}
任务编号形如 cao-code-03：采集者-场景-两位序号；场景为 code / chat / web / table。

时间规则：只认系统截图工具在截图当时写进文件名的时间（macOS「截屏 / Screenshot」、Windows「屏幕截图 /
Screenshot」），或采集者当场手填的时间；都没有就是 unknown。不用文件修改时间、不按顺序推算。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from . import dataset, db

SCENARIOS = ("code", "chat", "web", "table")
LANGS = ("zh", "en", "mixed")
TASK_RE = re.compile(r"^([a-z]+)-(code|chat|web|table)-(\d{2})$")
FACT_RE = re.compile(r"^([a-z]+-(?:code|chat|web|table)-\d{2})#(\d+)$")
SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
MANIFEST = "manifest.csv"
MANIFEST_COLS = ["image_id", "file", "sha", "width", "height", "captured_local", "time_source", "collector",
                 "device", "task_id", "scenario", "app", "window_title", "language", "facts", "keep",
                 "drop_reason", "note"]
FILLED_BY_HUMAN = ["captured_local", "time_source", "device", "app", "window_title", "language", "facts", "keep",
                   "drop_reason", "note"]
EXTRA_LABEL_COLS = ["collector", "image_id", "source_file", "facts", "ocr_chars", "time_known"]
MIN_SHORT_SIDE = 500          # 短边像素下限：低于它通常是裁得太小或缩放过的图
MAX_TASK_FRAMES = 40          # 单个任务有效帧上限，避免一个任务撑起整个类别
NEAR_HAMMING = 16             # 256 位差分哈希的候选阈值（约 6%）
NEAR_DIFF_FRAC = 0.02         # 候选确认：缩略灰度图上明显不同的像素不到 2% 视为近重复
NEAR_PIX_DELTA = 24

# 截图工具写进文件名的时间
_TIME_PATTERNS = [
    # macOS：截屏2026-10-17 09.30.12 / 截屏2026-10-17 下午2.30.12 / Screenshot 2026-10-17 at 9.30.12 AM
    re.compile(r"(\d{4})-(\d{2})-(\d{2})\D{1,6}?(上午|下午)?\s*(\d{1,2})[.:](\d{2})[.:](\d{2})\s*(AM|PM)?", re.I),
    # Windows 截图工具：屏幕截图 2026-10-17 093012 / Screenshot 2026-10-17 093012
    re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+()(\d{2})(\d{2})(\d{2})()(?!\d)"),
]


class CollectError(ValueError):
    pass


def time_from_filename(name: str) -> str | None:
    """返回 "YYYY-MM-DD HH:MM:SS"（本地时间）；文件名里没有截图工具写的时间就返回 None。"""
    stem = Path(name).stem
    if not re.search(r"截屏|屏幕截图|screenshot|screen shot", stem, re.I):
        return None
    for pat in _TIME_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        y, mo, d, ampm_zh, h, mi, s, ampm_en = m.groups()
        h = int(h)
        pm = (ampm_zh == "下午") or (ampm_en or "").upper() == "PM"
        am = (ampm_zh == "上午") or (ampm_en or "").upper() == "AM"
        if pm and h < 12:
            h += 12
        if am and h == 12:
            h = 0
        try:
            return datetime(int(y), int(mo), int(d), h, int(mi), int(s)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def file_sha(data: bytes) -> str:
    """与 importer.content_hash 相同，导入后可以用它找回 frame_id。"""
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def parse_task(task_id: str) -> tuple[str, str]:
    m = TASK_RE.match(task_id)
    if not m:
        raise CollectError(f"任务编号 {task_id!r} 不符合 采集者-场景-两位序号（如 cao-code-03），场景为 {SCENARIOS}")
    return m.group(1), m.group(2)


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:  # Excel 直接打开不乱码
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    tmp.replace(path)


# ---------------- manifest ----------------

def build_manifest(task_dir: Path, *, device: str = "", app: str = "", language: str = "") -> tuple[int, int, list[str]]:
    """生成或刷新 task_dir/manifest.csv。返回（新增行数, 总行数, 提示）。

    已有行按 sha 保留人工填写的列；文件被删掉的行会提示但不自动删，避免误删已填的信息。
    """
    task_id = task_dir.name
    collector, scenario = parse_task(task_id)
    path = task_dir / MANIFEST
    old = {r["sha"]: r for r in read_csv(path)} if path.exists() else {}
    if not device:  # 没给就沿用本任务已有行的设备（一个任务只在一台机器上采）
        devs = {r.get("device") for r in old.values() if r.get("device")}
        device = devs.pop() if len(devs) == 1 else ""
    used_ids = {r["image_id"] for r in old.values()}
    files = sorted(p for p in task_dir.iterdir() if p.is_file() and p.suffix.lower() in SUFFIXES)
    entries = []
    for p in files:
        data = p.read_bytes()
        entries.append((time_from_filename(p.name) or "", p.name, file_sha(data), data))
    entries.sort(key=lambda e: (e[0] or "9999", e[1]))  # 有时间的按时间，其余按文件名
    rows, notes, added = [], [], 0
    seen: dict[str, str] = {}
    next_seq = 1 + max((int(i.rsplit("-", 1)[1]) for i in used_ids if re.search(r"-\d{3}$", i)), default=0)
    for t, name, sha, data in entries:
        if sha in seen:
            notes.append(f"{name} 与 {seen[sha]} 内容完全相同，只保留一行；请删除多余的文件")
            continue
        seen[sha] = name
        try:
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001 - 坏图在 check 里报
            w = h = ""
        if sha in old:
            r = dict(old[sha])
            r.update(file=name, width=w, height=h, collector=collector, task_id=task_id, scenario=scenario)
        else:
            r = {c: "" for c in MANIFEST_COLS}
            r.update(image_id=f"{task_id}-{next_seq:03d}", file=name, sha=sha, width=w, height=h,
                     captured_local=t, time_source="filename" if t else "unknown", collector=collector,
                     device=device, task_id=task_id, scenario=scenario, app=app, language=language, keep="yes")
            next_seq += 1
            added += 1
        rows.append(r)
    gone = set(old) - seen.keys()
    for sha in gone:
        notes.append(f"清单里的 {old[sha]['file']}（{old[sha]['image_id']}）找不到文件；"
                     f"如果是故意删除，请在清单里删掉这一行，否则把文件放回")
        rows.append(old[sha])
    write_csv(path, MANIFEST_COLS, rows)
    return added, len(rows), notes


# ---------------- check ----------------

def _dhash(gray: Image.Image, size: int = 16) -> int:
    small = gray.resize((size + 1, size), Image.Resampling.BILINEAR)
    px = small.tobytes()
    bits = 0
    for y in range(size):
        row = px[y * (size + 1):(y + 1) * (size + 1)]
        for x in range(size):
            bits = (bits << 1) | (row[x] > row[x + 1])
    return bits


def _thumb(gray: Image.Image, width: int = 256) -> Image.Image:
    h = max(1, round(gray.height * width / gray.width))
    return gray.resize((width, h), Image.Resampling.BILINEAR)


def near_duplicate(a: Image.Image, b: Image.Image) -> bool:
    """两张灰度图是否近重复：宽高比接近，且缩略图上明显不同的像素少于 NEAR_DIFF_FRAC。"""
    ra, rb = a.width / a.height, b.width / b.height
    if abs(ra - rb) / max(ra, rb) > 0.03:
        return False
    ta = _thumb(a)
    tb = b.resize(ta.size, Image.Resampling.BILINEAR)
    pa, pb = ta.tobytes(), tb.tobytes()
    diff = sum(1 for x, y in zip(pa, pb) if abs(x - y) > NEAR_PIX_DELTA)
    return diff / len(pa) < NEAR_DIFF_FRAC


@dataclass
class Item:
    row: dict
    path: Path
    keep: bool


@dataclass
class CheckResult:
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    near_pairs: list[tuple[str, str, bool]] = field(default_factory=list)  # (image_id, image_id, 同任务)
    counts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_items(root: Path) -> tuple[list[Item], list[str]]:
    items, errors = [], []
    for mf in sorted(root.glob(f"*/*/{MANIFEST}")):
        task_dir = mf.parent
        with open(mf, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in MANIFEST_COLS if c not in (reader.fieldnames or [])]
            if missing:
                errors.append(f"{mf.relative_to(root)}：缺少列 {missing}")
                continue
            for r in reader:
                items.append(Item(r, task_dir / (r.get("file") or ""), (r.get("keep") or "").strip() == "yes"))
    for d in sorted(p for p in root.glob("*/*") if p.is_dir() and not p.name.startswith(".")):
        if not (d / MANIFEST).exists() and any(c.suffix.lower() in SUFFIXES for c in d.iterdir()):
            errors.append(f"{d.relative_to(root)}：有截图但还没有 manifest.csv（先运行 manifest）")
    return items, errors


def _facts_table(root: Path) -> dict[str, dict]:
    """事实表：每人维护自己的 <root>/<采集者>/facts.csv（避免两人同时改一个文件），也接受 <root>/facts.csv。"""
    out: dict[str, dict] = {}
    for p in [root / "facts.csv", *sorted(root.glob("*/facts.csv"))]:
        if p.exists():
            for r in read_csv(p):
                fid = (r.get("fact_id") or "").strip()
                if fid:
                    out[fid] = r
    return out


def check(root: Path, *, cache_path: Path | None = None, near: bool = True) -> CheckResult:
    res = CheckResult()
    res.items, res.errors = load_items(root)
    facts = _facts_table(root)
    tasks_csv = root / "tasks.csv"
    tasks = {r["task_id"].strip(): r for r in read_csv(tasks_csv)} if tasks_csv.exists() else {}
    if not tasks:
        res.warnings.append("没有 tasks.csv（或为空）：无法核对任务是否在共同任务表里")
    for fid, fr in facts.items():
        if not FACT_RE.match(fid):
            res.errors.append(f"facts.csv：事实编号 {fid!r} 应形如 cao-code-03#1")
    cache_path = cache_path or root / ".collect_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    ids: Counter = Counter()
    by_px: dict[str, list[str]] = defaultdict(list)
    hashes: list[tuple[Item, int]] = []
    per_task: Counter = Counter()

    for it in res.items:
        r = it.row
        where = f"{r.get('task_id')}/{r.get('file')}（{r.get('image_id')}）"
        ids[r.get("image_id")] += 1
        try:
            collector, scenario = parse_task(r.get("task_id", ""))
        except CollectError as exc:
            res.errors.append(f"{where}：{exc}")
            continue
        if it.path.parent.name != r["task_id"] or it.path.parent.parent.name != collector:
            res.errors.append(f"{where}：文件夹应为 {collector}/{r['task_id']}/")
        if r.get("collector") != collector or r.get("scenario") != scenario:
            res.errors.append(f"{where}：collector / scenario 与任务编号不一致")
        if tasks and r["task_id"] not in tasks:
            res.warnings.append(f"{where}：任务 {r['task_id']} 不在 tasks.csv 里")
        keep = (r.get("keep") or "").strip()
        if keep not in ("yes", "drop"):
            res.errors.append(f"{where}：keep 只能填 yes 或 drop")
            continue
        if keep == "drop":
            if not (r.get("drop_reason") or "").strip():
                res.errors.append(f"{where}：keep=drop 要写 drop_reason")
            continue
        # 以下只检查有效帧
        for col in ("device", "app", "language"):
            if not (r.get(col) or "").strip():
                res.errors.append(f"{where}：{col} 没填")
        if (r.get("language") or "").strip() and r["language"].strip() not in LANGS:
            res.errors.append(f"{where}：language 只能是 {LANGS}")
        ts = (r.get("time_source") or "").strip()
        cap = (r.get("captured_local") or "").strip()
        if ts not in ("filename", "manual", "unknown"):
            res.errors.append(f"{where}：time_source 只能是 filename / manual / unknown")
        elif ts == "unknown" and cap:
            res.errors.append(f"{where}：time_source=unknown 时 captured_local 必须留空，不要补猜的时间")
        elif ts != "unknown":
            try:
                datetime.fromisoformat(cap)
            except ValueError:
                res.errors.append(f"{where}：captured_local 应为 YYYY-MM-DD HH:MM:SS")
        for fid in filter(None, (x.strip() for x in (r.get("facts") or "").split(";"))):
            if fid not in facts:
                res.errors.append(f"{where}：事实 {fid} 不在 facts.csv 里")
        if not it.path.is_file():
            res.errors.append(f"{where}：文件不存在")
            continue
        data = it.path.read_bytes()
        sha = file_sha(data)
        if sha != r.get("sha"):
            res.errors.append(f"{where}：文件内容与清单里的 sha 不符（文件被改过？重新运行 manifest）")
            continue
        info = cache.get(sha)
        if info is None:
            try:
                with Image.open(io.BytesIO(data)) as im:
                    if im.format not in ("PNG", "JPEG", "WEBP") or getattr(im, "is_animated", False):
                        raise ValueError(f"格式 {im.format} 不是静态 PNG / JPEG / WebP")
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    gray = im.convert("L")
                    info = {"w": im.width, "h": im.height,
                            "px": hashlib.blake2b(im.tobytes(), digest_size=16).hexdigest(),
                            "dh": format(_dhash(gray), "x")}
            except Exception as exc:  # noqa: BLE001
                res.errors.append(f"{where}：无法解码（{exc}）")
                continue
            cache[sha] = info
        if min(info["w"], info["h"]) < MIN_SHORT_SIDE:
            res.errors.append(f"{where}：尺寸 {info['w']}×{info['h']}，短边小于 {MIN_SHORT_SIDE}，文字多半看不清")
        by_px[info["px"]].append(r["image_id"])
        hashes.append((it, int(info["dh"], 16)))
        per_task[r["task_id"]] += 1

    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    for iid, n in ids.items():
        if n > 1:
            res.errors.append(f"image_id {iid} 重复 {n} 次")
    for px, group in by_px.items():
        if len(group) > 1:
            res.errors.append(f"像素完全相同（复制或重复截图）：{group}；只保留一张，其余 keep=drop")
    for t, n in per_task.items():
        if n > MAX_TASK_FRAMES:
            res.warnings.append(f"任务 {t} 有 {n} 个有效帧，超过 {MAX_TASK_FRAMES}；请拆成多个任务或减少相似帧")

    if near:
        cand = [(a, b) for i, (a, ha) in enumerate(hashes) for (b, hb) in hashes[i + 1:]
                if bin(ha ^ hb).count("1") <= NEAR_HAMMING]
        grays: dict[str, Image.Image] = {}

        def gray(it: Item) -> Image.Image:
            k = it.row["image_id"]
            if k not in grays:
                with Image.open(it.path) as im:
                    grays[k] = ImageOps.exif_transpose(im).convert("L")
            return grays[k]

        for a, b in cand:
            if cache[a.row["sha"]]["px"] == cache[b.row["sha"]]["px"]:
                continue  # 完全相同已经作为错误报过
            if near_duplicate(gray(a), gray(b)):
                same = a.row["task_id"] == b.row["task_id"]
                res.near_pairs.append((a.row["image_id"], b.row["image_id"], same))
                if not same:
                    res.errors.append(
                        f"跨任务近重复：{a.row['image_id']} 与 {b.row['image_id']}；同一画面不能分在两个任务里，"
                        f"把其中一张 keep=drop（drop_reason 写 near-dup of ...）")
                elif not (a.row.get("note") or "").strip() and not (b.row.get("note") or "").strip():
                    res.warnings.append(
                        f"任务内近重复：{a.row['image_id']} 与 {b.row['image_id']}；有意义的变化（如 16→18）请在"
                        f"note 写明变化，否则删一张")

    kept = [it for it in res.items if it.keep]
    res.counts = {
        "by_collector_scenario": dict(Counter(f"{i.row['collector']}/{i.row['scenario']}" for i in kept)),
        "tasks": dict(Counter(f"{i.row['collector']}/{i.row['scenario']}" for i in
                              {i.row["task_id"]: i for i in kept}.values())),
        "language": dict(Counter(i.row.get("language") for i in kept)),
        "time_unknown": sum(1 for i in kept if i.row.get("time_source") == "unknown"),
        "kept": len(kept), "dropped": len(res.items) - len(kept),
    }
    return res


def render_check(res: CheckResult) -> str:
    lines = [f"有效帧（keep=yes）{res.counts.get('kept', 0)}，已剔除 {res.counts.get('dropped', 0)}；"
             f"时间未知 {res.counts.get('time_unknown', 0)}",
             "各采集者 / 场景帧数：" + json.dumps(res.counts.get("by_collector_scenario", {}), ensure_ascii=False),
             "各采集者 / 场景任务数：" + json.dumps(res.counts.get("tasks", {}), ensure_ascii=False),
             "语言：" + json.dumps(res.counts.get("language", {}), ensure_ascii=False)]
    lines += [f"错误：{e}" for e in res.errors]
    lines += [f"注意：{w}" for w in res.warnings]
    lines.append("检查通过" if res.ok else f"检查未通过：{len(res.errors)} 个错误")
    return "\n".join(lines)


# ---------------- import ----------------

def _open_existing(data_dir: str) -> sqlite3.Connection:
    from .config import load_config

    path = load_config(data_dir).db_path
    if not path.exists():
        raise CollectError(f"{path} 不存在：先运行 import 导入截图")
    return db.connect(path)


def _imported(conn: sqlite3.Connection) -> dict[str, str]:
    """已成功导入且画面仍在的文件：sha → frame_id。"""
    return {
        r[0]: r[1] for r in conn.execute(
            "SELECT i.content_hash, i.frame_id FROM import_items i JOIN frames f ON f.frame_id = i.frame_id "
            "WHERE i.status = 'ok' AND f.image_status != 'deleted'")
    }


def import_root(root: Path, data_dir: str, *, only_collector: str | None = None,
                only_task: str | None = None) -> list[str]:
    from . import importer
    from .config import load_config

    res = check(root)
    if not res.ok:
        raise CollectError("清单检查未通过，先修正：\n" + render_check(res))
    cfg = load_config(data_dir)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    out = []
    try:
        done = _imported(conn)
        by_task: dict[str, list[Item]] = defaultdict(list)
        for it in res.items:
            if it.keep and (not only_collector or it.row["collector"] == only_collector) and \
                    (not only_task or it.row["task_id"] == only_task):
                by_task[it.row["task_id"]].append(it)
        for task_id, its in sorted(by_task.items()):
            todo = [it for it in sorted(its, key=lambda i: i.row["image_id"]) if it.row["sha"] not in done]
            if not todo:
                out.append(f"{task_id}：{len(its)} 张都已导入，跳过")
                continue
            files, metas = [], []
            for it in todo:
                r = it.row
                cap = (r.get("captured_local") or "").strip()
                ms = int(datetime.fromisoformat(cap).timestamp() * 1000) if r.get("time_source") != "unknown" and cap \
                    else None
                files.append(importer.ImportFile(r["image_id"] + it.path.suffix.lower(), it.path.read_bytes()))
                metas.append(importer.FileMeta(ms, r.get("app") or None, (r.get("window_title") or "").strip() or None))
            br = importer.import_batch(conn, cfg, files, label=task_id, metas=metas)
            out.append(f"{task_id}：成功 {br.count(importer.OK)}、失败 {br.count(importer.FAILED)}、"
                       f"跳过 {br.count(importer.SKIPPED)}" + "".join(
                           f"\n  {i.name}: {i.status} — {i.reason}" for i in br.items if i.status != importer.OK))
    finally:
        conn.close()
    return out


# ---------------- labels ----------------

def build_labels(root: Path, data_dir: str, out: Path, *, collector: str | None = None,
                 keep_from: list[Path] = ()) -> tuple[int, int, list[str]]:
    """标注表：每个已导入的有效帧一行。

    task_id / scenario / language 以清单为准（要改就改 manifest 再重新生成）；out 以及 keep_from 里的
    文件中人工填写的 label、annotator、review_label、reviewer、final_label、change_reason 按 frame_id 保留。
    两人各自在自己的分表里标注，最后用 keep_from 合成一份总表；同一帧在两份表里填得不一样会报错。
    collector 给定时只输出该采集者的帧（分表）。
    """
    from .importer import is_time_known
    from .timeutil import fmt_local

    human_cols = [c for c in dataset.LABEL_COLS if c not in ("task_id", "scenario", "language")]
    items, errors = load_items(root)
    if errors:
        raise CollectError("\n".join(errors))
    prev: dict[str, dict] = {}
    for src in ([out] if out.exists() else []) + [p for p in keep_from if p != out]:
        for r in read_csv(src):
            filled = {c: (r.get(c) or "").strip() for c in human_cols if (r.get(c) or "").strip()}
            if not filled:
                continue
            old = prev.get(r["frame_id"])
            if old is not None and any(old.get(c) and old[c] != v for c, v in filled.items()):
                raise CollectError(f"{r.get('image_id') or r['frame_id']} 在 {src.name} 里的标注与另一份表不一致，请先统一")
            prev.setdefault(r["frame_id"], {}).update(filled)
    conn = _open_existing(data_dir)
    try:
        done = _imported(conn)
        rows, missing = [], []
        for it in sorted((i for i in items if i.keep and (not collector or i.row["collector"] == collector)),
                         key=lambda i: i.row["image_id"]):
            r = it.row
            fid = done.get(r["sha"])
            if fid is None:
                missing.append(r["image_id"])
                continue
            f = conn.execute(
                "SELECT f.image_path, f.capture_meta, o.first_seen_ms, o.app, o.window_title, o.session_id, "
                "o.source_type, t.text FROM frames f JOIN occurrences o ON o.frame_id = f.frame_id "
                "LEFT JOIN frame_text t ON t.frame_id = f.frame_id WHERE f.frame_id = ? "
                "ORDER BY o.first_seen_ms LIMIT 1", (fid,)).fetchone()
            known = is_time_known(json.loads(f["capture_meta"] or "{}"))
            row = {
                "frame_id": fid, "image_path": f["image_path"],
                "captured_local": fmt_local(f["first_seen_ms"]) if known else "",
                "app": f["app"] or "", "window_title": f["window_title"] or "", "session_id": f["session_id"],
                "source_type": f["source_type"], "task_id": r["task_id"], "scenario": r["scenario"],
                "language": r.get("language", ""), "collector": r["collector"], "image_id": r["image_id"],
                "source_file": f"{r['collector']}/{r['task_id']}/{r['file']}", "facts": r.get("facts", ""),
                "ocr_chars": "" if f["text"] is None else len("".join(f["text"].split())),
                "time_known": "yes" if known else "no",
            }
            old = prev.get(fid) or {}
            for c in human_cols:
                row[c] = (old.get(c) or "").strip()
            rows.append(row)
        write_csv(out, dataset.ALL_COLS + EXTRA_LABEL_COLS, rows)
        unindexed = sum(1 for r in rows if r["ocr_chars"] == "")
        notes = [f"{len(missing)} 张有效帧还没导入：{missing[:10]}{'…' if len(missing) > 10 else ''}"] if missing else []
        if unindexed:
            notes.append(f"{unindexed} 帧还没有 OCR 结果（ocr_chars 为空）：先运行 python -m screenrecall.indexer")
        return len(rows), len(prev), notes
    finally:
        conn.close()


# ---------------- facts ----------------

def fact_frames(root: Path, data_dir: str) -> dict[str, list[str]]:
    items, _ = load_items(root)
    conn = _open_existing(data_dir)
    try:
        done = _imported(conn)
    finally:
        conn.close()
    out: dict[str, list[str]] = defaultdict(list)
    for it in items:
        if not it.keep or it.row["sha"] not in done:
            continue
        for fid in filter(None, (x.strip() for x in (it.row.get("facts") or "").split(";"))):
            out[fid].append(done[it.row["sha"]])
    return {k: sorted(v) for k, v in sorted(out.items())}


def fill_relevant(queries_path: Path, mapping: dict[str, list[str]]) -> list[str]:
    """查询里写 "facts": ["cao-code-03#1", ...]，relevant 由这些事实出现的帧生成。

    relevant 已有内容且与事实推出的不同时不覆盖，只提示（冻结后的查询不应再改）。
    """
    qs = json.loads(queries_path.read_text(encoding="utf-8"))
    notes = []
    for q in qs:
        fs = q.get("facts") or []
        if not fs:
            continue
        rel = sorted({f for fid in fs for f in mapping.get(fid, [])})
        miss = [fid for fid in fs if not mapping.get(fid)]
        if miss:
            notes.append(f"{q.get('id')}：事实 {miss} 没有对应的已导入帧")
        cur = sorted(q.get("relevant") or [])
        if cur and cur != rel:
            notes.append(f"{q.get('id')}：relevant 已有 {len(cur)} 帧，与事实推出的 {len(rel)} 帧不同，未覆盖")
            continue
        q["relevant"] = rel
    queries_path.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
    return notes


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.collect", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest", help="生成 / 刷新一个或多个任务文件夹的 manifest.csv")
    m.add_argument("task_dirs", nargs="+")
    m.add_argument("--device", default="", help="新行的设备编号，如 cao-mbp")
    m.add_argument("--app", default="", help="新行的应用名默认值（之后可逐行改）")
    m.add_argument("--language", default="", choices=("",) + LANGS)
    c = sub.add_parser("check")
    c.add_argument("root")
    c.add_argument("--no-near", action="store_true", help="跳过近重复比较（只做快速格式检查）")
    for name in ("import", "labels", "facts"):
        p = sub.add_parser(name)
        p.add_argument("root")
        p.add_argument("--data-dir", required=True)
    sub.choices["import"].add_argument("--collector")
    sub.choices["import"].add_argument("--task")
    sub.choices["labels"].add_argument("--out", required=True)
    sub.choices["labels"].add_argument("--collector", help="只输出这个采集者的帧（各自的分表）")
    sub.choices["labels"].add_argument("--keep-from", nargs="*", default=[], help="沿用这些表里已填的标注（合成总表时用）")
    sub.choices["facts"].add_argument("--queries", help="把 facts 对应的帧写进这个查询文件的 relevant")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "manifest":
            for d in args.task_dirs:
                added, total, notes = build_manifest(Path(d), device=args.device, app=args.app,
                                                     language=args.language)
                print(f"{Path(d).name}：新增 {added} 行，共 {total} 行")
                for n in notes:
                    print("  注意：" + n)
            return 0
        if args.cmd == "check":
            res = check(Path(args.root), near=not args.no_near)
            print(render_check(res))
            return 0 if res.ok else 1
        if args.cmd == "import":
            for line in import_root(Path(args.root), args.data_dir, only_collector=args.collector,
                                    only_task=args.task):
                print(line)
            print("下一步：python -m screenrecall.indexer --data-dir " + args.data_dir)
            return 0
        if args.cmd == "labels":
            n, kept, notes = build_labels(Path(args.root), args.data_dir, Path(args.out), collector=args.collector,
                                         keep_from=[Path(x) for x in args.keep_from])
            print(f"标注表 {args.out}：{n} 帧（沿用已有标注的文件 {kept} 行）")
            for x in notes:
                print("注意：" + x)
            return 0
        if args.cmd == "facts":
            mapping = fact_frames(Path(args.root), args.data_dir)
            if args.queries:
                for x in fill_relevant(Path(args.queries), mapping):
                    print("注意：" + x)
                print(f"已更新 {args.queries} 的 relevant")
            else:
                print(json.dumps(mapping, ensure_ascii=False, indent=2))
            return 0
    except CollectError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
