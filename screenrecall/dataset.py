"""受控数据的标注与分组划分（方案 6.1、详细方案 7.1）：python -m screenrecall.dataset <子命令>

  export   从数据库导出待标注清单 CSV（每帧一行，附应用、标题、录制段、图片路径，标注列留空）
  check    校验标注 CSV：类别在冻结的类别表内、复核结论齐全、帧仍存在
  split    按来源组划分 train / val / test：同一任务、录制批次、重复画面（同像素或同 OCR 文字）必须同组

标注只写在 CSV 里（随数据集提交），不写进数据库；frame_id 只对产生它的那份数据库有效。
类别表与划分比例都由团队在采集前冻结，这里不预设 60/20/20 之类的比例，必须显式给出并记录在划分清单里。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import db, timeutil

# 标注清单列：前半由 export 填，后半由标注 / 复核人填
EXPORT_COLS = ["frame_id", "image_path", "captured_local", "app", "window_title", "session_id", "source_type"]
LABEL_COLS = ["task_id", "scenario", "language", "label", "annotator", "review_label", "reviewer", "final_label",
              "change_reason"]
ALL_COLS = EXPORT_COLS + LABEL_COLS
SPLITS = ("train", "val", "test")
# 画面来源（与 collect 清单同一套取值）：mock_ui 是自制仿界面，只作开发测试
SOURCE_KINDS = ("real_app", "real_web", "mock_ui")
DEV_ONLY_KINDS = {"mock_ui"}


class DatasetError(ValueError):
    pass


# ---------------- export ----------------

def export_frames(conn: sqlite3.Connection, out: Path, *, include_unprocessed: bool = False) -> int:
    """每个仍存在的画面一行，取最早一次出现的应用与标题。默认只导出 OCR 完成、原图可用的画面。"""
    cond = "f.image_status = 'ok'" + ("" if include_unprocessed else " AND f.ocr_status = 'done'")
    rows = conn.execute(
        "SELECT f.frame_id, f.image_path, o.first_seen_ms, o.app, o.window_title, o.session_id, o.source_type "
        "FROM frames f JOIN occurrences o ON o.occurrence_id = ("
        "  SELECT occurrence_id FROM occurrences WHERE frame_id = f.frame_id ORDER BY first_seen_ms, occurrence_id LIMIT 1) "
        f"WHERE {cond} ORDER BY o.first_seen_ms, f.frame_id"
    ).fetchall()
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:  # utf-8-sig：Windows Excel 直接打开不乱码
        w = csv.writer(fh)
        w.writerow(ALL_COLS + ["source_kind"])
        for r in rows:
            # 实时录制的是真实应用窗口；导入的截图来源未知，留空由人填
            kind = "" if r["source_type"] == "import" else "real_app"
            w.writerow([r["frame_id"], r["image_path"], timeutil.fmt_local(r["first_seen_ms"]), r["app"] or "",
                        r["window_title"] or "", r["session_id"], r["source_type"]] + [""] * len(LABEL_COLS)
                       + [kind])
    return len(rows)


# ---------------- check ----------------

@dataclass
class Labeled:
    frame_id: str
    label: str
    task_id: str
    scenario: str
    language: str
    app: str
    session_id: str


@dataclass
class CheckReport:
    items: list[Labeled] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unlabeled: int = 0
    changed_by_review: int = 0
    dev_only_skipped: int = 0   # source_kind=mock_ui 的仿界面样本：只作开发测试，不进正式数据集

    @property
    def ok(self) -> bool:
        return not self.errors


def load_labelset(path: Path) -> list[str]:
    """类别表 JSON：{"labels": ["code", "chat", ...]}。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    labels = [str(x).strip() for x in data.get("labels", []) if str(x).strip()]
    if len(labels) < 2 or len(set(labels)) != len(labels):
        raise DatasetError("类别表至少要有 2 个互不相同的类别")
    return labels


def read_labels(conn: sqlite3.Connection, csv_path: Path, labels: list[str], *,
                require_review: bool = True, include_dev_only: bool = False) -> CheckReport:
    """正式（require_review）：每条都要有初标 label + annotator 和复核 review_label + reviewer，两人不同；
    初标与复核不一致（分歧）时还要填定稿 final_label 和 change_reason。只填 final_label 不算标注。
    试标（require_review=False）：有初标即可，最终类别 = final_label / review_label / label 中第一个非空。

    source_kind 必须是 real_app / real_web / mock_ui；mock_ui 只作开发测试，默认排除。旧表没有这一列时，
    正式模式报错（用 collect labels 重新生成或显式补列），试标模式按未知来源照常读取。
    """
    rep = CheckReport()
    allowed = set(labels)
    alive = {r[0] for r in conn.execute("SELECT frame_id FROM frames WHERE image_status != 'deleted'")}
    seen: set[str] = set()
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in ALL_COLS if c not in (reader.fieldnames or [])]
        if missing:
            raise DatasetError(f"标注文件缺少列：{missing}")
        has_kind = "source_kind" in (reader.fieldnames or [])
        if require_review and not has_kind:
            raise DatasetError("标注文件没有 source_kind 列（旧表）：正式检查要逐行知道画面来源，"
                               f"请用 collect labels 重新生成，或补上这一列并逐行填 {SOURCE_KINDS}")
        for n, row in enumerate(reader, start=2):
            fid = (row["frame_id"] or "").strip()
            where = f"第 {n} 行（{fid[:8]}）"
            kind = (row.get("source_kind") or "").strip()
            if has_kind and kind not in SOURCE_KINDS:
                rep.errors.append(f"{where}：source_kind {kind!r} 不合法，应为 {SOURCE_KINDS}")
                continue
            if not include_dev_only and kind in DEV_ONLY_KINDS:
                rep.dev_only_skipped += 1
                continue
            first = (row["label"] or "").strip()
            ann, rev = (row["annotator"] or "").strip(), (row["reviewer"] or "").strip()
            review, fin = (row["review_label"] or "").strip(), (row["final_label"] or "").strip()
            reason = (row["change_reason"] or "").strip()
            if not any((first, ann, review, rev, fin)):
                rep.unlabeled += 1
                continue
            if fid in seen:
                rep.errors.append(f"{where}：frame_id 重复")
                continue
            seen.add(fid)
            if fid not in alive:
                rep.errors.append(f"{where}：数据库里没有这个画面（已删除或不是这份数据库）")
                continue
            if require_review:
                lack = [c for c, v in (("label", first), ("annotator", ann), ("review_label", review),
                                       ("reviewer", rev)) if not v]
                if lack:
                    rep.errors.append(f"{where}：初标与复核记录不完整，缺 {lack}（只填 final_label 不算标注）")
                    continue
                if rev == ann:
                    rep.errors.append(f"{where}：复核人 reviewer 不能与标注人 annotator 相同")
                    continue
                if first != review and (not fin or not reason):
                    rep.errors.append(f"{where}：初标 {first} 与复核 {review} 不一致，要填定稿 final_label 和 change_reason")
                    continue
                final = fin or review
            else:
                if not first:
                    rep.errors.append(f"{where}：没有初标 label（只填复核或定稿列不算标注）")
                    continue
                final = fin or review or first
            for v in {first, review, final} - {""}:
                if v not in allowed:
                    rep.errors.append(f"{where}：类别 {v!r} 不在类别表 {labels} 中")
            if final != first:
                rep.changed_by_review += 1
                if not reason:
                    rep.errors.append(f"{where}：复核改了类别（{first} → {final}），要写 change_reason")
            if final in allowed:
                rep.items.append(Labeled(fid, final, (row["task_id"] or "").strip(), (row["scenario"] or "").strip(),
                                         (row["language"] or "").strip(), (row["app"] or "").strip(),
                                         (row["session_id"] or "").strip()))
    return rep


# ---------------- split ----------------

class _UF:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def source_groups(conn: sqlite3.Connection, items: list[Labeled]) -> dict[str, str]:
    """来源组：task_id（标注人填的任务编号；没填时用录制段 / 导入批次）为起点，
    再把像素完全相同或 OCR 全文相同（非空）的画面并到同一组。返回 frame_id → 组编号。"""
    uf = _UF()
    ids = [it.frame_id for it in items]
    for it in items:
        uf.union("f:" + it.frame_id, "t:" + (it.task_id or "s:" + it.session_id))
    meta = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT f.frame_id, f.pixel_hash, t.text_fold FROM frames f LEFT JOIN frame_text t ON t.frame_id = f.frame_id "
            "WHERE f.frame_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),),
        )
    }
    for fid, (px, text) in meta.items():
        uf.union("f:" + fid, "p:" + px)
        if text and text.strip():
            uf.union("f:" + fid, "x:" + hashlib.blake2b(text.strip().encode(), digest_size=16).hexdigest())
    roots = {fid: uf.find("f:" + fid) for fid in ids}
    names = {r: f"g{i:04d}" for i, r in enumerate(sorted(set(roots.values())))}
    return {fid: names[r] for fid, r in roots.items()}


@dataclass
class SplitResult:
    assignment: dict[str, str]          # frame_id → train / val / test
    groups: dict[str, str]              # frame_id → 组编号
    fractions: dict[str, float]
    seed: int
    counts: dict[str, dict[str, int]]   # split → label → 帧数
    warnings: list[str]

    def manifest(self, labels: list[str], csv_sha: str) -> dict:
        return {
            "fractions": self.fractions, "seed": self.seed, "labels": labels, "labels_csv_blake2b": csv_sha,
            "n_groups": len(set(self.groups.values())), "counts": self.counts, "warnings": self.warnings,
            "frames": {f: {"split": s, "group": self.groups[f]} for f, s in sorted(self.assignment.items())},
        }


def grouped_split(items: list[Labeled], groups: dict[str, str], fractions: dict[str, float],
                  seed: int) -> SplitResult:
    """按组分配：组按帧数从大到小（同大小按种子打乱）依次放进当前最缺帧的划分。整组同去同留。"""
    if set(fractions) != set(SPLITS) or any(v < 0 for v in fractions.values()) or abs(sum(fractions.values()) - 1) > 1e-6:
        raise DatasetError("划分比例要给 train、val、test 三项，且非负、合计为 1")
    by_group: dict[str, list[Labeled]] = defaultdict(list)
    for it in items:
        by_group[groups[it.frame_id]].append(it)
    order = sorted(by_group)
    random.Random(seed).shuffle(order)
    order.sort(key=lambda g: -len(by_group[g]))  # 稳定排序：同大小保持打乱后的顺序
    total = len(items)
    have = {s: 0 for s in SPLITS}
    assign: dict[str, str] = {}
    for g in order:
        deficit = {s: fractions[s] * total - have[s] for s in SPLITS if fractions[s] > 0}
        s = max(deficit, key=lambda k: (deficit[k], -SPLITS.index(k)))
        for it in by_group[g]:
            assign[it.frame_id] = s
        have[s] += len(by_group[g])
    counts = {s: dict(Counter(it.label for it in items if assign[it.frame_id] == s)) for s in SPLITS}
    labels = sorted({it.label for it in items})
    warnings = []
    for s in SPLITS:
        if fractions[s] > 0:
            miss = [lb for lb in labels if not counts[s].get(lb)]
            if miss:
                warnings.append(f"{s} 中没有类别 {miss}：组太少或类别集中在少数组，需要补采或调整比例")
    if len(by_group) < 3 * len(labels):
        warnings.append(f"只有 {len(by_group)} 个来源组，类别 {len(labels)} 个；组数太少时划分结果不稳定")
    return SplitResult(assign, groups, fractions, seed, counts, warnings)


def file_hash(path: Path) -> str:
    return hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()


def write_candidates(path: Path, frame_ids: list[str], labels_sha: str) -> None:
    """正式候选名单：# 开头的行是说明（来源标注表指纹），其余每行一个 frame_id。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# accepted frames from labels blake2b={labels_sha}; n={len(frame_ids)}\n"
                    + "".join(f + "\n" for f in sorted(set(frame_ids))), encoding="utf-8")


def read_candidates(path: Path) -> frozenset[str]:
    ids = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    out = frozenset(x for x in ids if x and not x.startswith("#"))
    if not out:
        raise DatasetError(f"{path} 里没有任何候选帧")
    return out


def raw_gate(raw_root: Path, labels_csv: Path, frame_ids: list[str]) -> list[str]:
    """出正式名单前对原始截图根目录再跑一次 collect check（含近重复）。返回阻断问题，空表示通过。

    阻断：清单有错误；任务内近重复还有没在 near_dups.csv 逐对判定的；名单里的帧在清单里已剔除或找不到。
    """
    from . import collect  # collect 在模块级 import 了本模块，这里延迟导入

    res = collect.check(raw_root, near=True)
    problems = [f"清单检查错误：{e}" for e in res.errors]
    pending = res.counts.get("near_pending", 0)
    if pending:
        problems.append(f"任务内近重复还有 {pending} 对没有在 near_dups.csv 判定（keep_both + 理由 + 判定人），"
                        "或删掉其中一张；判完再出正式名单")
    keep = {it.row["image_id"]: it.keep for it in res.items}
    image_of = {(r.get("frame_id") or "").strip(): (r.get("image_id") or "").strip()
                for r in csv.DictReader(open(labels_csv, encoding="utf-8-sig", newline=""))}
    for fid in frame_ids:
        iid = image_of.get(fid, "")
        if iid not in keep:
            problems.append(f"{fid[:8]}（{iid or '无 image_id'}）：原始清单里找不到这张图")
        elif not keep[iid]:
            problems.append(f"{fid[:8]}（{iid}）：清单里已 keep=drop，却仍在数据库和标注表中；先从数据集库删掉")
    return problems


# ---------------- CLI ----------------

def _parse_fractions(text: str) -> dict[str, float]:
    parts = [float(x) for x in text.split(",")]
    if len(parts) != 3:
        raise DatasetError("--fractions 格式为 train,val,test，例如 0.6,0.2,0.2（比例由团队按组数决定）")
    return dict(zip(SPLITS, parts))


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(prog="python -m screenrecall.dataset", description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    e.add_argument("--include-unprocessed", action="store_true")
    for name in ("check", "split", "accepted"):
        p = sub.add_parser(name)
        p.add_argument("--labels", required=True, help="标注 CSV")
        p.add_argument("--labelset", required=True, help="冻结的类别表 JSON")
        p.add_argument("--allow-unreviewed", action="store_true", help="只用于试标阶段；正式划分必须有复核结论")
    sub.choices["accepted"].add_argument("--out", required=True,
                                         help="正式候选帧名单（每行一个 frame_id），给 evaluate --candidates 用")
    sub.choices["accepted"].add_argument("--raw", required=True,
                                         help="原始截图根目录（如 data-dataset/raw）；近重复没判完或帧已剔除时拒绝出名单")
    sp = sub.choices["split"]
    sp.add_argument("--fractions", required=True, help="train,val,test，例如 0.6,0.2,0.2")
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--out", required=True, help="划分清单 JSON")
    args = ap.parse_args(argv)
    cfg = load_config(args.data_dir)
    conn = db.connect(cfg.db_path)
    try:
        if args.cmd == "export":
            n = export_frames(conn, Path(args.out), include_unprocessed=args.include_unprocessed)
            print(f"导出 {n} 帧到 {args.out}")
            return 0
        labels = load_labelset(Path(args.labelset))
        rep = read_labels(conn, Path(args.labels), labels, require_review=not args.allow_unreviewed)
        print(f"已标注 {len(rep.items)} 帧，未标注 {rep.unlabeled} 帧，复核改动 {rep.changed_by_review} 帧；"
              f"仿界面开发样本（mock_ui，已排除）{rep.dev_only_skipped} 帧；"
              f"类别分布 {dict(Counter(i.label for i in rep.items))}")
        for err in rep.errors:
            print("错误：" + err, file=sys.stderr)
        if not rep.ok:
            return 1
        if args.cmd == "check":
            return 0
        if args.cmd == "accepted":
            if args.allow_unreviewed:
                print("错误：正式候选名单只能来自完成双人复核的标注，不能加 --allow-unreviewed", file=sys.stderr)
                return 1
            problems = raw_gate(Path(args.raw), Path(args.labels), [i.frame_id for i in rep.items])
            if problems:
                for p in problems[:30]:
                    print("错误：" + p, file=sys.stderr)
                print(f"错误：共 {len(problems)} 个问题，未写出正式名单", file=sys.stderr)
                return 1
            write_candidates(Path(args.out), [i.frame_id for i in rep.items], file_hash(Path(args.labels)))
            print(f"正式候选 {len(rep.items)} 帧（已双人复核、非仿界面）写入 {args.out}")
            return 0
        res = grouped_split(rep.items, source_groups(conn, rep.items), _parse_fractions(args.fractions), args.seed)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(res.manifest(labels, file_hash(Path(args.labels))), ensure_ascii=False,
                                             indent=2), encoding="utf-8")
        print(f"{len(set(res.groups.values()))} 个来源组；各划分类别分布 {res.counts}")
        for w in res.warnings:
            print("注意：" + w)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
