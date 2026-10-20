"""cao 正式采集：一次准备多个真实应用任务，用户一条命令打开，之后由本工具识别窗口、截图、记日志。

  terminal  每个任务一个真实 Terminal 窗口（<中性名>.command），步骤见 scenes/terminal_tasks.py
  docs      每个文件一个真实应用窗口（Numbers / Excel / 预览），文件见 scenes/doc_tasks.py

    T=data-dataset/.tools/venv/bin/python
    $T datasets/capture_real_batch.py terminal dryrun  --tasks cao-code-02,cao-code-06   # 无窗口试跑每条命令
    $T datasets/capture_real_batch.py terminal prepare --tasks cao-code-02,cao-code-06   # 打印给用户的 open 命令
    $T datasets/capture_real_batch.py terminal attach  --tasks cao-code-02,cao-code-06 --out data-dataset/raw/cao
    $T datasets/capture_real_batch.py docs prepare --tasks cao-table-01
    $T datasets/capture_real_batch.py docs attach  --tasks cao-table-01 --out data-dataset/raw/cao

截图编号接着任务文件夹里已有的最大编号往后排，不覆盖已有文件；attach 可以中断后重跑（已截过的步骤跳过）。
沙箱不允许本工具让其他应用打开文档，所以 open 由用户执行；本工具不点击、不打字、不移动窗口。
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scenes"))
from capture_mac import append_log, looks_blank  # noqa: E402
from capture_real_mac import PY, neutral_dir, window_by_id, window_titled, windows  # noqa: E402

UV_DIR = Path(shutil.which("uv") or "/Applications/KiroCrew.app/Contents/Resources/backend-dist/"
              "kirocrew-backend-arm64/bin/uv").parent
# 本机 CommandLineTools 默认 SDK（27.0）的 libSystem.tbd 链接器读不了（tapi error），固定用已装的 26.5
SDKROOT = "/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk"
SCENE = "terminal2"   # 与 2026-10-18 的 cao-code-01 试采目录（we77ba2）区分，避免重建时删掉它


def next_seq(task_dir: Path) -> int:
    nums = [int(m.group(1)) for p in task_dir.glob("*.png") if (m := re.search(r"_(\d{3})\.png$", p.name))]
    return max(nums, default=0) + 1


def shoot(task_dir: Path, task_id: str, seq: int, wid: int) -> tuple[Path, str | None]:
    dest = task_dir / f"{task_id}_{datetime.now():%Y%m%d-%H%M%S}_{seq:03d}.png"
    if dest.exists():
        raise SystemExit(f"{dest} 已存在，不覆盖")
    subprocess.run(["screencapture", "-x", "-o", f"-l{wid}", str(dest)], check=True)
    return dest, looks_blank(dest)


# ---------------------------------------------------------------- Terminal

def _tasks_mod():
    return importlib.import_module("terminal_tasks")


def env_path() -> str:
    return f"{PY.parent}:{UV_DIR}:/usr/bin:/bin:/usr/sbin:/sbin"


def write_script(work: Path, steps: list[dict]) -> Path:
    lines = ["#!/bin/zsh", f"cd {shlex.quote(str(work))}", f"export PATH={shlex.quote(env_path())}",
             f"export PYTHONDONTWRITEBYTECODE=1 UV_NO_PROGRESS=1 SDKROOT={SDKROOT}", "clear"]
    for i, st in enumerate(steps, 1):
        rows, cols = st["size"]
        lines += [f"printf '\\e[8;{rows};{cols}t'"] + ([] if st.get("keep") else ["clear"]) + [
            "sleep 0.4",
            f"print -r -- {shlex.quote(work.name + ' % ' + st['cmd'])}",
            st["cmd"],
            f"print -rn -- {shlex.quote(work.name + ' % ')}",
            f"touch ready.{i}",
            f"while [ ! -f go.{i} ]; do sleep 0.2; done",
            "print"]
    lines += ["print '[采集脚本结束，可以关闭这个窗口]'", "exit 0"]
    p = work / f"{work.name}.command"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    p.chmod(0o755)
    return p


def terminal_prepare(tasks: list[str]) -> list[Path]:
    mod = _tasks_mod()
    scripts = []
    for t in tasks:
        setup, steps = mod.TASKS[t]
        work = neutral_dir(t, SCENE, fresh=True)
        setup(work)
        scripts.append(write_script(work, steps()))
    return scripts


def terminal_dryrun(tasks: list[str]) -> int:
    """在临时副本里不开窗口逐条运行，检查每条命令有输出、没有意外的 shell 错误（command not found 等）。"""
    mod = _tasks_mod()
    scratch = Path(os.environ.get("KIROCREW_SCRATCH") or "/tmp") / "dryrun-terminal"
    bad = 0
    for t in tasks:
        setup, steps = mod.TASKS[t]
        work = scratch / ("w" + t.split("-")[-1])
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        setup(work)
        env = dict(os.environ, PATH=env_path(), PYTHONDONTWRITEBYTECODE="1", UV_NO_PROGRESS="1", SDKROOT=SDKROOT)
        for i, st in enumerate(steps(), 1):
            r = subprocess.run(["zsh", "-c", st["cmd"]], cwd=work, env=env, capture_output=True, text=True,
                               timeout=120)
            out = (r.stdout + r.stderr).strip()
            flag = ""
            if not out:
                flag = "无输出"
            elif re.search(r"command not found|zsh:\d*: (?:parse error|no matches found|no such file)|tapi error", out):
                flag = "shell 错误"
            if flag:
                bad += 1
            last = out.splitlines()[-1][:90] if out else ""
            print(f"{t} #{i:02d} rc={r.returncode} lines={len(out.splitlines()):3d} {flag:8s} {last}")
        print(f"{t}: {len(steps())} 步")
    return bad


def terminal_attach(tasks: list[str], out: Path) -> int:
    mod = _tasks_mod()
    fails = 0
    for t in tasks:
        _, steps_fn = mod.TASKS[t]
        steps = steps_fn()
        work = neutral_dir(t, SCENE, fresh=False)
        task_dir = out / t
        task_dir.mkdir(parents=True, exist_ok=True)
        win = window_titled("Terminal", work.name)
        if not win:
            print(f"!! {t}: 找不到标题含 {work.name} 的 Terminal 窗口（用户还没打开？）", file=sys.stderr)
            fails += 1
            continue
        done = 0
        for i, st in enumerate(steps, 1):
            if (work / f"go.{i}").exists():
                continue
            deadline = time.time() + 180
            while not (work / f"ready.{i}").exists():
                if time.time() > deadline:
                    print(f"!! {t} 第 {i} 步超时", file=sys.stderr)
                    return fails + 1
                time.sleep(0.3)
            time.sleep(1.0)
            cur = window_by_id("Terminal", win[0])
            if not cur:
                print(f"!! {t}: Terminal 窗口不见了", file=sys.stderr)
                return fails + 1
            dest, bad = shoot(task_dir, t, next_seq(task_dir), cur[0])
            if bad:
                fails += 1
                print(f"!! {dest.name}: {bad}", file=sys.stderr)
            append_log(task_dir, {
                "file": dest.name, "captured_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "time_source": "manual", "app": "Terminal", "window_title": cur[1], "language": st["lang"],
                "facts": st.get("facts", ""),
                "source": f"真实 Terminal 窗口；分步脚本 {work.name}.command 真实运行：{st['cmd'][:160]}",
                "variation": f"窗口 {cur[2][2]}x{cur[2][3]}pt；{st['var']}",
                "note": "受控测试内容（测试脚本 / 示例文件）；提示符由脚本打印，命令与输出为真实运行；窗口由用户用 open 打开"
                        + (f"；{bad}" if bad else ""),
                "source_kind": "real_app"})
            (work / f"go.{i}").touch()
            done += 1
        print(f"  {t}: 本次截 {done} 张")
    return fails


# ---------------------------------------------------------------- 文档窗口（Numbers / Excel / 预览）

def _docs_mod():
    return importlib.import_module("doc_tasks")


def docs_prepare(tasks: list[str]) -> list[str]:
    mod = _docs_mod()
    cmds = []
    for t in tasks:
        work = neutral_dir(t, "docs", fresh=True)
        app, files = mod.build(t, work)
        # 每个文件单独 open 一次：一次 open 多个文件时预览会合进同一个窗口
        quoted = " ".join(shlex.quote(str(p)) for p, _ in files)
        cmds.append(f"for f in {quoted}; do open -a {shlex.quote(app)} \"$f\"; sleep 1.5; done")
    return cmds


def docs_attach(tasks: list[str], out: Path) -> int:
    mod = _docs_mod()
    fails = 0
    for t in tasks:
        work = neutral_dir(t, "docs", fresh=False)
        app, files = mod.listing(t, work)
        owner = mod.OWNER[app]
        task_dir = out / t
        task_dir.mkdir(parents=True, exist_ok=True)
        log = task_dir / "capture_log.csv"
        shot = log.read_text(encoding="utf-8-sig") if log.exists() else ""
        n = 0
        for path, meta in files:
            if f"{work.name}/{path.name}" in shot:
                continue  # 已截过
            cur = window_titled(owner, path.stem)
            if not cur:
                print(f"!! {t}: 找不到标题含 {path.stem} 的 {owner} 窗口", file=sys.stderr)
                fails += 1
                continue
            dest, bad = shoot(task_dir, t, next_seq(task_dir), cur[0])
            if bad:
                fails += 1
                print(f"!! {dest.name}: {bad}", file=sys.stderr)
            append_log(task_dir, {
                "file": dest.name, "captured_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "time_source": "manual", "app": owner, "window_title": cur[1], "language": meta["lang"],
                "facts": meta.get("facts", ""),
                "source": f"真实 {owner} 窗口打开 {work.name}/{path.name}（脚本生成的虚构内容）",
                "variation": f"窗口 {cur[2][2]}x{cur[2][3]}pt；{meta['var']}",
                "note": "虚构数据；未点击、未编辑；文件由用户用 open 打开" + (f"；{bad}" if bad else ""),
                "source_kind": "real_app"})
            n += 1
        print(f"  {t}: 本次截 {n} 张")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scene", choices=("terminal", "docs"))
    ap.add_argument("step", choices=("dryrun", "prepare", "attach", "status"))
    ap.add_argument("--tasks", required=True, help="逗号分隔的任务编号")
    ap.add_argument("--out", help="attach：采集者根目录，如 data-dataset/raw/cao")
    a = ap.parse_args()
    tasks = [t.strip() for t in a.tasks.split(",") if t.strip()]
    if a.step == "dryrun":
        return 1 if terminal_dryrun(tasks) else 0
    if a.step == "prepare":
        if a.scene == "terminal":
            print("open " + " ".join(shlex.quote(str(p)) for p in terminal_prepare(tasks)))
        else:
            print("\n".join(docs_prepare(tasks)))
        return 0
    if a.step == "status":
        owner = "Terminal" if a.scene == "terminal" else None
        print(windows(owner) if owner else {o: windows(o) for o in ("Numbers", "Microsoft Excel", "Preview")})
        return 0
    out = Path(a.out).resolve()
    fails = terminal_attach(tasks, out) if a.scene == "terminal" else docs_attach(tasks, out)
    print(f"完成，问题 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
