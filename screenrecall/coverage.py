"""捕获覆盖实验（方案 6.3「捕获覆盖」）：python -m screenrecall.coverage <子命令>

  plan       生成冻结的事件脚本（JSON）：事件类型 × 持续时间 × 背景变化，按种子随机排序并随机错开开始时间
  stimulus   打开受控测试窗口，按脚本逐个播放事件，把每个阶段实际显示的时间写进日志（JSONL）
  analyze    对照日志与数据目录里的录制结果，统计每个事件是否被保存（覆盖率），以及没保存时有没有中断标记

实验步骤（Windows 实机，普通 / 排错模式分开各跑一次）：
  1. plan --mode normal --out results/coverage/<日期>/plan-normal.json
  2. 启动后台和页面；stimulus --plan ... --log ...（窗口标题固定为 STIMULUS_TITLE）
  3. 在页面里选这个窗口、按脚本模式开始录制（排错模式要在页面里切到排错）；stimulus 先倒计时，再播放
  4. 播放结束后停止录制，等 OCR 跑完（可选，用于文字确认），再 analyze

事件定义：每个试次先显示基线画面 A（等它稳定、被保存），再显示事件状态 d 秒，然后回到 A。
  single_char      基线里一个数字变化（小于 1% 像素）
  passive_message  右下角弹出一条被动消息（约 5–10% 像素）
  aba              整屏换成另一页内容 B，再回到 A（A→B→A）
背景：none 主体静止；cursor_blink 小光标闪烁；big_animation 大面积动画；scroll 日志区持续滚动。
覆盖：存在一张该窗口的新保存画面，抓拍时间落在事件实际显示的 [开始, 结束] 内。若 OCR 已完成，再核对事件令牌。

方案目标（只作冻结脚本中的待验证目标，不外推）：主体静止或只有小光标闪烁、普通模式事件 ≥6 秒、排错模式事件 ≥4 秒、
未触发过载（排错模式未提前退回普通）时覆盖率 ≥95%。其余条件逐项报告。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

KINDS = ("single_char", "passive_message", "aba")
DURATIONS = (1, 3, 4, 6, 10)
BACKGROUNDS = ("none", "cursor_blink", "big_animation", "scroll")
MODES = ("normal", "debug")
TARGET_MIN_DURATION = {"normal": 6, "debug": 4}
TARGET_BACKGROUNDS = ("none", "cursor_blink")
TARGET_RATE = 0.95
STIMULUS_TITLE = "ScreenRecall Coverage Stimulus"
SETTLE_S = 14.0     # 基线停留：普通模式 10 秒兜底 + 一个采样间隔，保证基线先被保存
SETTLE_DEBUG_S = 6.0  # 排错模式不等稳，2 秒自动保存间隔 + 余量即可
POST_S = 4.0        # 事件结束后回到 A 的停留
PART_MAX_S = {"normal": None, "debug": 25 * 60.0}  # 排错模式 30 分钟自动退出（config debug_max_minutes），每段留余量
PLAN_VERSION = 2


# ======================= plan =======================

def make_plan(mode: str, *, reps: int, seed: int, interval_s: float, kinds=KINDS, durations=DURATIONS,
              backgrounds=BACKGROUNDS) -> dict:
    """全因子 × reps，按种子打乱顺序；每个试次的开始相对采样网格随机错开 [0, interval)。

    排错模式会在 30 分钟后自动回到普通模式，所以排错脚本按 PART_MAX_S 切成几段：每段是一次单独的排错会话，
    段内时间从 0 开始（stimulus --part N 播放一段，播放前在页面里重新进入排错模式）。
    """
    if mode not in MODES:
        raise ValueError(f"mode 只能是 {MODES}")
    rng = random.Random(seed)
    cells = [(k, d, b) for k in kinds for d in durations for b in backgrounds for _ in range(reps)]
    rng.shuffle(cells)
    settle = SETTLE_S if mode == "normal" else SETTLE_DEBUG_S
    limit = PART_MAX_S[mode]
    trials, t, part = [], 0.0, 0
    for i, (k, d, b) in enumerate(cells):
        jitter = round(rng.uniform(0, interval_s), 3)
        length = settle + jitter + d + POST_S
        if limit is not None and t > 0 and t + length > limit:
            part, t = part + 1, 0.0
        base_counter = rng.randint(1000, 8998)
        token = f"{rng.randint(10000, 99999)}"
        trial = {"trial": i, "part": part, "kind": k, "duration_s": d, "background": b, "jitter_s": jitter,
                 "base_counter": base_counter, "token": token,
                 "t_base": round(t, 3), "t_event": round(t + settle + jitter, 3)}
        trial["t_return"] = round(trial["t_event"] + d, 3)
        trial["t_end"] = round(trial["t_return"] + POST_S, 3)
        trials.append(trial)
        t = trial["t_end"]
    parts = [round(max(tr["t_end"] for tr in trials if tr["part"] == p), 3) for p in range(part + 1)]
    return {"version": PLAN_VERSION, "mode": mode, "seed": seed, "reps": reps, "interval_s": interval_s,
            "settle_s": settle, "post_s": POST_S, "title": STIMULUS_TITLE, "parts_s": parts,
            "total_s": round(sum(parts), 3), "trials": trials}


def state_at(plan: dict, t: float, part: int = 0) -> tuple[dict | None, str]:
    """第 part 段开始后的时刻 t 应显示哪个试次的哪个阶段（base / event / return）；超出该段返回 (None, 'done')。"""
    for tr in plan["trials"]:
        if tr.get("part", 0) == part and tr["t_base"] <= t < tr["t_end"]:
            if t < tr["t_event"]:
                return tr, "base"
            if t < tr["t_return"]:
                return tr, "event"
            return tr, "return"
    return None, "done"


# ======================= stimulus (Tk) =======================

LOG_LINES = [f"[{i:02d}] worker-{i % 4} processed batch {100 + i * 7} ok" for i in range(40)]


def run_stimulus(plan: dict, log_path: Path, *, start_delay_s: float = 15.0, part: int = 0) -> None:  # pragma: no cover - 需要显示器
    import tkinter as tk

    W, H = 1100, 720
    root = tk.Tk()
    root.title(plan.get("title", STIMULUS_TITLE))
    root.geometry(f"{W}x{H}+80+60")
    root.resizable(False, False)
    cv = tk.Canvas(root, width=W, height=H, bg="#1e1e1e", highlightthickness=0)
    cv.pack()
    mono = ("Courier New", 18)
    big = ("Courier New", 26, "bold")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "a", encoding="utf-8")

    def log(**rec):
        rec["t_ms"] = int(time.time() * 1000)
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()

    def draw(tr: dict | None, phase: str, now: float, t_rel: float) -> None:
        cv.delete("all")
        if tr is None:
            cv.create_text(40, 40, anchor="nw", fill="#d4d4d4", font=big, text=phase)
            return
        n = len(plan["trials"])
        cv.create_text(30, 20, anchor="nw", fill="#9cdcfe", font=mono,
                       text=f"coverage trial {tr['trial'] + 1:03d}/{n:03d}   mode={plan['mode']}")
        if phase == "event" and tr["kind"] == "aba":
            cv.create_rectangle(0, 60, W, H, fill="#f3f3f3", outline="")
            cv.create_text(40, 90, anchor="nw", fill="#1a1a1a", font=big, text=f"SCREEN B  {tr['token']}")
            for j in range(8):
                cv.create_text(40, 160 + j * 42, anchor="nw", fill="#333333", font=mono,
                               text=f"B-{tr['token']} row {j}: invoice total {(j + 3) * 117}.00 SGD")
        else:
            counter = tr["base_counter"] + (1 if phase == "event" and tr["kind"] == "single_char" else 0)
            cv.create_text(40, 80, anchor="nw", fill="#d4d4d4", font=big, text=f"SCREEN A  counter = {counter}")
            cv.create_text(40, 130, anchor="nw", fill="#ce9178", font=mono,
                           text="status: running   epoch 3/10   loss 0.4182")
            # 日志区：scroll 背景时持续上移
            off = int(t_rel / 0.7) % len(LOG_LINES) if tr["background"] == "scroll" else 0
            for j in range(9):
                cv.create_text(40, 190 + j * 34, anchor="nw", fill="#b5cea8", font=mono,
                               text=LOG_LINES[(off + j) % len(LOG_LINES)])
            if tr["background"] == "cursor_blink" and int(now / 0.53) % 2 == 0:
                cv.create_rectangle(560, 500, 572, 528, fill="#d4d4d4", outline="")
            if phase == "event" and tr["kind"] == "passive_message":
                cv.create_rectangle(W - 430, H - 170, W - 30, H - 40, fill="#264f78", outline="#4fc1ff", width=2)
                cv.create_text(W - 410, H - 150, anchor="nw", fill="#ffffff", font=mono,
                               text=f"New message  MSG {tr['token']}")
                cv.create_text(W - 410, H - 110, anchor="nw", fill="#cccccc", font=mono, text="from: course-bot")
        if tr["background"] == "big_animation":
            x0, y0, x1, y1 = 600, 160, 1060, 470
            shift = int(now * 120) % 60
            for k in range(-1, (x1 - x0) // 30 + 2):
                x = x0 + k * 30 + shift
                cv.create_rectangle(max(x0, x), y0, min(x1, x + 15), y1,
                                    fill=("#c586c0", "#4ec9b0", "#dcdcaa")[k % 3], outline="")

    t_start = time.time() + start_delay_s
    log(event="start_scheduled", start_ms=int(t_start * 1000), plan_seed=plan["seed"], mode=plan["mode"], part=part)
    last = (None, None)
    try:
        while True:
            now = time.time()
            if now < t_start:
                draw(None, f"starting in {int(t_start - now) + 1} s  - select this window in ScreenRecall", now, 0)
                root.update()
                time.sleep(0.05)
                continue
            t_rel = now - t_start
            tr, phase = state_at(plan, t_rel, part)
            draw(tr, phase if tr else "done - stop recording now", now, t_rel)
            root.update()
            key = (tr["trial"] if tr else None, phase)
            if key != last:
                log(event="phase", trial=key[0], phase=phase)  # 记录的是画好之后的时刻
                last = key
            if tr is None:
                break
            time.sleep(0.02)
        time.sleep(2)
    finally:
        fh.close()
        root.destroy()


# ======================= analyze =======================

def read_log(path: Path) -> dict[int, dict[str, int]]:
    """{trial: {phase: 首次显示的毫秒}}。"""
    out: dict[int, dict[str, int]] = defaultdict(dict)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("event") == "phase" and r.get("trial") is not None:
            out[int(r["trial"])].setdefault(r["phase"], int(r["t_ms"]))
    return out


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)


def _rate(rows: list[dict]) -> dict:
    n = len(rows)
    k = sum(r["covered"] for r in rows)
    return {"covered": k, "n": n, "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


@dataclass
class _Frame:
    frame_id: str
    at: int
    mode: str
    text: str


def analyze(conn: sqlite3.Connection, plan: dict, log: dict[int, dict[str, int]]) -> dict:
    title = plan.get("title", STIMULUS_TITLE)
    rows = conn.execute(
        "SELECT f.frame_id, f.captured_at_ms, f.capture_meta, COALESCE(t.text_fold, '') FROM frames f "
        "JOIN occurrences o ON o.frame_id = f.frame_id AND o.first_seen_ms = f.captured_at_ms "
        "LEFT JOIN frame_text t ON t.frame_id = f.frame_id WHERE o.window_title = ? ORDER BY f.captured_at_ms",
        (title,),
    ).fetchall()
    frames = []
    for fid, at, meta, text in rows:
        try:
            mode = json.loads(meta or "{}").get("mode", "")
        except ValueError:
            mode = ""
        frames.append(_Frame(fid, int(at), mode, text))
    gaps = conn.execute("SELECT started_at_ms, COALESCE(ended_at_ms, started_at_ms), reason FROM gaps").fetchall()

    per = []
    for tr in plan["trials"]:
        ph = log.get(tr["trial"], {})
        if "event" not in ph or "return" not in ph:
            per.append({**_key(tr), "status": "not_played", "covered": 0})
            continue
        s, e = ph["event"], ph["return"]
        base_t = ph.get("base", s)
        inside = [f for f in frames if s <= f.at <= e]
        trial_frames = [f for f in frames if base_t <= f.at <= e]
        tok = tr["token"]
        ocr_known = [f for f in inside if f.text]
        mismatch = [f.mode for f in trial_frames if f.mode and f.mode != plan["mode"]]
        marked = any(r == "unsaved_change" and a <= e and b >= s for a, b, r in gaps)
        rec = {**_key(tr), "status": "played", "event_ms": [s, e], "shown_ms": e - s,
               "covered": int(bool(inside)), "frames_in_event": len(inside),
               "first_save_after_ms": (inside[0].at - s) if inside else None,
               "ocr_confirmed": (any(tok in f.text for f in ocr_known) if ocr_known else None),
               "base_saved_before_event": any(base_t <= f.at < s for f in frames),
               "mode_mismatch": sorted(set(mismatch)), "unsaved_change_marked": marked}
        per.append(rec)

    played = [r for r in per if r["status"] == "played"]
    clean = [r for r in played if not r["mode_mismatch"]]
    target = [r for r in clean if r["background"] in TARGET_BACKGROUNDS
              and r["duration_s"] >= TARGET_MIN_DURATION[plan["mode"]]]
    by = {}
    for key in ("kind", "duration_s", "background"):
        by[key] = {str(v): _rate([r for r in clean if r[key] == v]) for v in sorted({r[key] for r in clean})}
    cells = defaultdict(list)
    for r in clean:
        cells[f"{r['kind']}|{r['duration_s']}s|{r['background']}"].append(r)
    missed = [r for r in clean if not r["covered"]]
    tg = _rate(target)
    return {
        "mode": plan["mode"], "plan_seed": plan["seed"], "interval_s": plan["interval_s"],
        "frames_from_stimulus": len(frames), "trials_planned": len(plan["trials"]), "trials_played": len(played),
        "trials_mode_mismatch": len(played) - len(clean),
        "overall": _rate(clean),
        "target": {**tg, "condition": f"背景 {TARGET_BACKGROUNDS}，事件 ≥{TARGET_MIN_DURATION[plan['mode']]} 秒，"
                                      "未提前退出排错模式", "goal": TARGET_RATE,
                   "meets_goal": (tg["rate"] is not None and tg["rate"] >= TARGET_RATE)},
        "by_factor": by,
        "cells": {k: _rate(v) for k, v in sorted(cells.items())},
        "missed": {"n": len(missed), "marked_unsaved_change": sum(r["unsaved_change_marked"] for r in missed)},
        "ocr_confirmed": {"checked": sum(r.get("ocr_confirmed") is not None for r in clean if r["covered"]),
                          "token_found": sum(bool(r.get("ocr_confirmed")) for r in clean if r["covered"])},
        "base_not_saved_before_event": sum(not r["base_saved_before_event"] for r in played),
        "per_trial": per,
    }


def _key(tr: dict) -> dict:
    return {k: tr[k] for k in ("trial", "kind", "duration_s", "background", "jitter_s", "token")}


def to_markdown(rep: dict) -> str:
    def fmt(r):
        if not r["n"]:
            return "—"
        ci = r["wilson95"]
        return f"{r['covered']}/{r['n']}（{r['rate']:.0%}，95% CI {ci[0]:.0%}–{ci[1]:.0%}）"

    t = rep["target"]
    lines = [f"# 捕获覆盖：{rep['mode']} 模式（脚本种子 {rep['plan_seed']}，采样间隔 {rep['interval_s']} 秒）", "",
             f"- 播放试次 {rep['trials_played']}/{rep['trials_planned']}；排错模式提前退出等模式不符而排除 "
             f"{rep['trials_mode_mismatch']}；来自测试窗口的保存画面 {rep['frames_from_stimulus']} 张",
             f"- **目标条件**（{t['condition']}）：{fmt(t)}；目标 ≥{t['goal']:.0%}，"
             f"{'达到' if t['meets_goal'] else '未达到'}（只针对这一冻结脚本，不外推）",
             f"- 全部条件：{fmt(rep['overall'])}",
             f"- 漏掉的事件 {rep['missed']['n']} 个，其中有可见中断标记（unsaved_change）"
             f"{rep['missed']['marked_unsaved_change']} 个",
             f"- 文字核对：已覆盖且 OCR 完成 {rep['ocr_confirmed']['checked']} 个，找到事件令牌 "
             f"{rep['ocr_confirmed']['token_found']} 个",
             f"- 事件前基线没被保存的试次：{rep['base_not_saved_before_event']}", ""]
    for key, name in (("kind", "事件类型"), ("duration_s", "持续秒数"), ("background", "背景")):
        lines += [f"## 按{name}", "", "| 取值 | 覆盖 |", "| --- | --- |"]
        lines += [f"| {v} | {fmt(r)} |" for v, r in rep["by_factor"][key].items()]
        lines.append("")
    lines += ["## 各条件组合", "", "| 事件 / 秒数 / 背景 | 覆盖 |", "| --- | --- |"]
    lines += [f"| {k} | {fmt(r)} |" for k, r in rep["cells"].items()]
    return "\n".join(lines) + "\n"


# ======================= CLI =======================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.coverage", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--mode", choices=MODES, required=True)
    p.add_argument("--reps", type=int, default=2, help="每个条件组合重复几次（默认 2：60 组合 × 2 ≈ 50 分钟）")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--interval", type=float, help="采样间隔秒数，用于随机错开；默认普通 3、排错 1（与 config 一致）")
    p.add_argument("--quick", action="store_true", help="冒烟：每种事件只取 1、6 秒与 none、big_animation")
    p.add_argument("--out", required=True)
    s = sub.add_parser("stimulus")
    s.add_argument("--plan", required=True)
    s.add_argument("--log", required=True)
    s.add_argument("--start-delay", type=float, default=15.0, help="开始前倒计时秒数，用来在页面里选中窗口并开始录制")
    s.add_argument("--part", type=int, default=0, help="排错脚本分段播放：第几段（从 0 开始），每段前在页面里重新进入排错模式")
    a = sub.add_parser("analyze")
    a.add_argument("--data-dir")
    a.add_argument("--plan", required=True)
    a.add_argument("--log", required=True)
    a.add_argument("--out", required=True, help="报告 JSON；同名 .md 一并写出")
    args = ap.parse_args(argv)

    if args.cmd == "plan":
        interval = args.interval or (3.0 if args.mode == "normal" else 1.0)
        kw = dict(durations=(1, 6), backgrounds=("none", "big_animation")) if args.quick else {}
        plan = make_plan(args.mode, reps=args.reps, seed=args.seed, interval_s=interval, **kw)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(plan['trials'])} 个试次，共 {len(plan['parts_s'])} 段，约 {plan['total_s'] / 60:.0f} 分钟"
              f"（各段 {[round(x / 60) for x in plan['parts_s']]} 分钟），写入 {args.out}")
        return 0
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.cmd == "stimulus":
        run_stimulus(plan, Path(args.log), start_delay_s=args.start_delay, part=args.part)
        return 0
    from . import db
    from .config import load_config

    cfg = load_config(args.data_dir)
    conn = db.connect(cfg.db_path)
    try:
        rep = analyze(conn, plan, read_log(Path(args.log)))
    finally:
        conn.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_suffix(".md").write_text(to_markdown(rep), encoding="utf-8")
    t = rep["target"]
    print(f"目标条件覆盖 {t['covered']}/{t['n']}，全部 {rep['overall']['covered']}/{rep['overall']['n']}；"
          f"报告 {out} 与 {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
