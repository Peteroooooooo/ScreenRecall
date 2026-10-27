"""v2 录制子进程的真实窗口检查：把 python -m screenrecall.v2.recorder 当子进程跑，只录本测试自己开的窗口。

- 一个 tkinter 画布窗口（UIA 给不出字 → 证据帧要整帧登记待 OCR）；
- 一个临时配置目录的 Edge，打开本地合成页面（UIA 读得到字 → 文字行）；
两个窗口都由页面脚本 / Tk 定时器自己滚动（不注入键鼠），用 control 的 pinned_windows 钉住，
再加 --only-hwnds 测试隔离：用户自己的窗口在前台时录制进程当成"没有可录的前台"，不截、不记。
临时 home / data 目录，跑完删掉；只报次数、耗时、CPU / 内存。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
import pytest

from screenrecall import winapi

winapi.enable_dpi_awareness()

ROOT = Path(__file__).resolve().parent.parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
RUN_S = 26.0

_TK = r"""
import ctypes, random, sys, tkinter as tk
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
W = "屏幕 记忆 窗口 滚动 课程 项目 数据 模型 搜索 证据 时间 应用 文字 识别 screen recall window scroll data block".split()
rng = random.Random(7)
root = tk.Tk(); root.title(sys.argv[1]); root.geometry("900x760+40+40")
cv = tk.Canvas(root, bg="white", highlightthickness=0, yscrollincrement=1); cv.pack(fill="both", expand=True)
for i in range(400):
    cv.create_text(30, 16 + i * 34, anchor="nw", font=("Microsoft YaHei UI", 14),
                   text=f"SRV2TK-{i:03d} " + " ".join(rng.choice(W) for _ in range(10)))
cv.configure(scrollregion=(0, 0, 900, 400 * 34 + 40))
def step(n=[0]):
    if n[0] < 12:
        cv.yview_scroll(300, "units"); n[0] += 1
    root.after(2500, step)
root.after(4000, step)
root.mainloop()
"""

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:18px "Microsoft YaHei UI",sans-serif;margin:24px;line-height:1.6}}</style></head><body>
{paras}
<script>let n = 0; setTimeout(() => {{ setInterval(() => {{ if (n++ < 12) window.scrollBy(0, 300); }}, 2500); }}, 4000);</script>
</body></html>"""


def _find_hwnd(title: str, timeout: float = 30.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in winapi.list_windows():
            if title in w.title:
                return w.hwnd
        time.sleep(0.2)
    raise AssertionError(f"没找到窗口：{title}")


def _kill_tree(pid: int) -> None:
    try:
        p = psutil.Process(pid)
        procs = p.children(recursive=True) + [p]
    except psutil.NoSuchProcess:
        return
    for q in procs:
        try:
            q.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)


def _close_edge(proc: subprocess.Popen, profile: Path) -> None:
    tag = str(profile)
    killed = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "msedge.exe" and any(tag in (c or "") for c in p.info["cmdline"] or []):
                p.kill()
                killed.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(killed, timeout=10)
    _kill_tree(proc.pid)


def _rmtree(path: Path) -> None:
    for _ in range(40):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)


@pytest.fixture
def work():
    base = Path(tempfile.mkdtemp(prefix="sr-v2rec-win-"))
    procs: list = []
    profile = base / "edge-profile"
    yield base, procs, profile
    for kind, p in procs:
        if kind == "close":
            p.close()
        elif kind == "edge":
            _close_edge(p, profile)
        else:
            _kill_tree(p.pid)
    _rmtree(base)


def _recorder_cmd(home: Path, data: Path, hwnds: list[int]) -> list[str]:
    return [sys.executable, "-m", "screenrecall.v2.recorder", "--data-dir", str(data), "--home", str(home),
            "--only-hwnds", ",".join(str(h) for h in hwnds)]


def _env(home: Path) -> dict:
    env = dict(os.environ)
    env["SCREENRECALL_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    env.pop("SCREENRECALL_DATA_DIR", None)
    return env


def _prepare_db(data: Path, pins: list[dict]) -> None:
    from screenrecall.store.writer import Writer

    with Writer(data) as w:
        w.control_set(desired_state="recording", pinned_windows=pins).result(10)


def _final_stats(log_path: Path) -> dict:
    stats = None
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if "recorder stats final " in line:
            stats = json.loads(line.split("recorder stats final ", 1)[1])
    assert stats is not None, "日志里没有收尾统计"
    return stats


@pytest.mark.skipif(not EDGE.exists(), reason="本机没有 Edge")
def test_recorder_process_end_to_end(work):
    base, procs, profile = work
    home, data = base / "home", base / "data"
    marker = uuid.uuid4().hex[:6]

    tk_title = f"sr-v2rec-tk-{marker}"
    tk_script = base / "tkdoc.py"
    tk_script.write_text(_TK, encoding="utf-8")
    tk = subprocess.Popen([sys.executable, str(tk_script), tk_title])
    procs.append(("tk", tk))

    web_title = f"sr-v2rec-web-{marker}"
    # 每段文字长短、用词都不一样（像真的文章）；几乎一样的段落会让按行指纹投票的平移判断接不上
    import random

    rng = random.Random(11)
    words = "屏幕 记忆 窗口 滚动 课程 项目 数据 模型 搜索 证据 时间 应用 文字 识别 组会 报告 screen recall " \
            "window scroll course project data model search evidence query block".split()
    paras = "\n".join(f"<p>SRV2WEB-{i:03d} " + " ".join(rng.choice(words) for _ in range(rng.randint(4, 14)))
                      + "</p>" for i in range(160))
    page = base / "page.html"
    page.write_text(_PAGE.format(title=web_title, paras=paras), encoding="utf-8")
    edge = subprocess.Popen([
        str(EDGE), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--new-window", "--window-size=900,760",
        "--window-position=960,40", "--disable-features=CalculateNativeWinOcclusion",
        "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding", page.as_uri(),
    ])
    procs.append(("edge", edge))
    tk_hwnd = _find_hwnd(tk_title)
    web_hwnd = _find_hwnd(web_title)
    tk_proc = winapi.describe_window(tk_hwnd).process
    web_proc = winapi.describe_window(web_hwnd).process

    _prepare_db(data, [{"hwnd": tk_hwnd, "process": tk_proc, "title": tk_title},
                       {"hwnd": web_hwnd, "process": web_proc, "title": web_title}])
    out = open(base / "recorder.out", "wb")
    procs.append(("close", out))
    rec = subprocess.Popen(_recorder_cmd(home, data, [tk_hwnd, web_hwnd]), stdin=subprocess.PIPE, stdout=out,
                           stderr=subprocess.STDOUT, cwd=str(ROOT), env=_env(home))
    procs.append(("rec", rec))
    # venv 的 python.exe 是个启动器，真正的解释器是它的子进程：CPU / 内存按整棵进程树算
    time.sleep(1.0)
    pr = psutil.Process(rec.pid)
    tree = [pr] + pr.children(recursive=True)

    def cpu_total():
        s = 0.0
        for p in tree:
            try:
                c = p.cpu_times()
                s += c.user + c.system
            except psutil.NoSuchProcess:
                pass
        return s

    cpu0 = cpu_total()
    t0 = time.monotonic()
    rss = []
    while time.monotonic() - t0 < RUN_S:
        assert rec.poll() is None, (base / "recorder.out").read_text(errors="replace")[-2000:]
        total = 0
        for p in tree:
            try:
                total += p.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        rss.append(total)
        time.sleep(1.0)
    cpu_s = cpu_total() - cpu0
    elapsed = time.monotonic() - t0

    # 停：关掉 stdin
    t_stop = time.perf_counter()
    rec.stdin.close()
    code = rec.wait(10)
    stop_s = time.perf_counter() - t_stop
    out.close()
    assert code == 0, (base / "recorder.out").read_text(errors="replace")[-2000:]
    assert stop_s < 3.0, stop_s
    assert not [p for p in psutil.process_iter(["cmdline"])
                if any(str(data) in (c or "") for c in (p.info["cmdline"] or []))], "录制进程有残留"

    conn = sqlite3.connect(f"file:{(data / 'recall.sqlite3').as_posix()}?mode=ro", uri=True)
    procs.append(("close", conn))  # 断言失败时也要关掉，否则临时目录删不掉
    conn.row_factory = sqlite3.Row
    segs = conn.execute("SELECT segment_id, app, title FROM segments").fetchall()
    # 只录了自己的窗口
    assert all(s["title"] == tk_title or s["title"].startswith(web_title) for s in segs), "录到了别的窗口"
    assert {a["process"] for a in conn.execute("SELECT process FROM activity")} <= {tk_proc, web_proc}
    assert all(marker in (a["title"] or "") for a in conn.execute("SELECT title FROM activity"))
    tk_seg = [s["segment_id"] for s in segs if s["title"] == tk_title]
    web_seg = [s["segment_id"] for s in segs if s["title"].startswith(web_title)]

    def count(sql, seg_ids):
        if not seg_ids:
            return 0
        q = ",".join("?" * len(seg_ids))
        return conn.execute(sql.format(q=q), seg_ids).fetchone()[0]

    tk_frames = count("SELECT COUNT(*) FROM frames WHERE segment_id IN ({q}) AND status = 'ok'", tk_seg)
    tk_regions = count("SELECT COUNT(*) FROM ocr_regions r JOIN frames f ON f.frame_id = r.frame_id "
                       "WHERE f.segment_id IN ({q})", tk_seg)
    tk_lines = count("SELECT COUNT(*) FROM lines WHERE segment_id IN ({q}) AND text LIKE 'SRV2TK-%'", tk_seg)
    web_lines = count("SELECT COUNT(*) FROM lines WHERE segment_id IN ({q}) AND text LIKE '%SRV2WEB-%'", web_seg)
    web_distinct = count("SELECT COUNT(DISTINCT text) FROM lines WHERE segment_id IN ({q}) "
                         "AND text LIKE '%SRV2WEB-%'", web_seg)
    # 同一块里重复的行（块之间的重复是"接不上 → 开新块"，留给空闲时按文字拼块）
    web_dupes = count("SELECT COUNT(*) - COUNT(DISTINCT block_id || '|' || text) FROM lines "
                      "WHERE segment_id IN ({q}) AND text LIKE '%SRV2WEB-%'", web_seg)
    web_frames = count("SELECT COUNT(*) FROM frames WHERE segment_id IN ({q}) AND status = 'ok'", web_seg)
    web_links = count("SELECT COUNT(*) FROM line_in_frame l JOIN frames f ON f.frame_id = l.frame_id "
                      "WHERE f.segment_id IN ({q})", web_seg)
    blocks = conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    skims = conn.execute("SELECT COUNT(*) FROM skims").fetchone()[0]
    ctl = conn.execute("SELECT * FROM control").fetchone()
    stopped = conn.execute("SELECT COUNT(*) FROM gaps WHERE reason = 'closed' AND detail = 'recorder stopped' "
                           "AND end_ms IS NULL").fetchone()[0]
    conn.close()

    stats = _final_stats(home / "logs" / "recorder.log")
    log_text = (home / "logs" / "recorder.log").read_text(encoding="utf-8")
    assert "SRV2WEB-" not in log_text and "SRV2TK-" not in log_text and marker not in log_text  # 日志里没有屏幕文字和标题

    t = stats["text"]
    print(f"\ncore: {stats['core']}")
    print(f"\nrecorder run {elapsed:.0f}s: cpu {cpu_s / elapsed * 100:.1f}% of one core, rss max {max(rss) / 2**20:.0f} MiB"
          f" median {sorted(rss)[len(rss) // 2] / 2**20:.0f} MiB; stop {stop_s * 1000:.0f} ms")
    print(f"db: segments={len(segs)} blocks={blocks} skims={skims} tk frames={tk_frames} tk regions={tk_regions} "
          f"tk uia lines={tk_lines} web lines={web_lines} distinct={web_distinct} same-block dupes={web_dupes} "
          f"web frames={web_frames} links={web_links}")
    print(f"stills={t['stills']} identical={t['identical']} uia_reads={t['uia_reads']} usable={t['uia_usable']} "
          f"not_ok={t['uia_not_ok']} stale={t['stale']} ocr_stills={t['ocr_stills']} saved={t['frames_saved']}")
    for k in ("uia_ms", "db_ms", "still_ms"):
        if k in t:
            print(f"{k}: {t[k]}")

    assert ctl["recorder_state"] == "stopped" and stopped == 1
    assert tk_frames >= 2 and tk_regions >= tk_frames and tk_lines == 0
    assert web_lines >= 30 and web_frames >= 1 and web_links > 0
    assert web_dupes <= max(3, web_lines // 20)
    assert t["stills"] >= 4


def test_recorder_exits_on_ctrl_break(work):
    base, procs, _ = work
    home, data = base / "home", base / "data"
    marker = uuid.uuid4().hex[:6]
    tk_title = f"sr-v2rec-brk-{marker}"
    tk_script = base / "tkdoc.py"
    tk_script.write_text(_TK, encoding="utf-8")
    tk = subprocess.Popen([sys.executable, str(tk_script), tk_title])
    procs.append(("tk", tk))
    hwnd = _find_hwnd(tk_title)
    _prepare_db(data, [{"hwnd": hwnd, "process": winapi.describe_window(hwnd).process, "title": tk_title}])
    out = open(base / "recorder.out", "wb")
    procs.append(("close", out))
    rec = subprocess.Popen(_recorder_cmd(home, data, [hwnd]), stdin=subprocess.PIPE, stdout=out,
                           stderr=subprocess.STDOUT, cwd=str(ROOT), env=_env(home),
                           creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    procs.append(("rec", rec))
    time.sleep(6.0)
    assert rec.poll() is None
    t0 = time.perf_counter()
    try:
        os.kill(rec.pid, signal.CTRL_BREAK_EVENT)
    except OSError as e:  # 测试进程没有控制台时发不了控制台事件
        pytest.skip(f"无法发送 Ctrl+Break：{e}")
    code = rec.wait(10)
    stop_s = time.perf_counter() - t0
    out.close()
    rec.stdin.close()
    assert code == 0, (base / "recorder.out").read_text(errors="replace")[-2000:]
    assert stop_s < 3.0
    stats = _final_stats(home / "logs" / "recorder.log")
    print(f"\nctrl+break stop {stop_s * 1000:.0f} ms; stills={stats['text']['stills']} "
          f"saved={stats['text']['frames_saved']}")
