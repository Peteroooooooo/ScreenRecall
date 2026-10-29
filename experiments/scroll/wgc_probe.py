"""PrintWindow 和 Windows.Graphics.Capture（走显卡）在大窗口上的开销对比。

只截本脚本自己开的 tkinter 窗口。需要 windows-capture==2.0.1（实验时装在临时目录，用 PYTHONPATH 引入）。
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import numpy as np
from windows_capture import WindowsCapture

sys.path.insert(0, str(Path(__file__).parent))
import align_probe as P  # noqa: E402

from screenrecall import winapi  # noqa: E402

STEPS, STEP_PX, GAP = 40, 90, 0.125  # 40 次滚动，每次 90 像素，每秒 8 次


def make_window(w, h):
    root = tk.Tk()
    root.title("sr-wgc-probe")
    root.geometry(f"{w}x{h}+0+0")
    cv = tk.Canvas(root, bg="white", highlightthickness=0, yscrollincrement=1)
    cv.pack(fill="both", expand=True)
    import random
    rng = random.Random(5)
    for i in range(1500):
        cv.create_text(40, 20 + i * 34, anchor="nw", font=P.FONT, text=P.sentence(rng) + "  " + P.sentence(rng))
    cv.configure(scrollregion=(0, 0, w, 1500 * 34 + 40))
    root.update()
    time.sleep(0.5)
    root.update()
    return root, cv, ctypes.windll.user32.GetAncestor(cv.winfo_id(), 2)


def scroll_loop(root, cv, on_step=None):
    tops = []
    for _ in range(STEPS):
        cv.yview_scroll(STEP_PX, "units")
        root.update()
        tops.append(int(cv.canvasy(0)))
        if on_step:
            on_step()
        t_end = time.perf_counter() + GAP
        while time.perf_counter() < t_end:
            root.update()
            time.sleep(0.005)
    return tops


def cpu_of(fn):
    c0, w0 = time.process_time(), time.perf_counter()
    r = fn()
    return r, time.process_time() - c0, time.perf_counter() - w0


def run(w, h):
    res = {}
    # 0) 不截图，只滚：作为基线
    root, cv, hwnd = make_window(w, h)
    _, cpu0, wall0 = cpu_of(lambda: scroll_loop(root, cv))
    res["只滚不截（基线）CPU 秒"] = round(cpu0, 2)

    # 1) PrintWindow：每次滚完截一张 + 算竖带指纹
    pw_ms = []

    def pw_step():
        t = time.perf_counter()
        img = P.grab(hwnd)
        P.strip_rows(img)
        pw_ms.append((time.perf_counter() - t) * 1000)

    _, cpu1, wall1 = cpu_of(lambda: scroll_loop(root, cv, pw_step))
    res["PrintWindow 每张(截+指纹) ms"] = round(float(np.median(pw_ms)), 1)
    res["PrintWindow 额外 CPU 秒/40 张"] = round(cpu1 - cpu0, 2)

    # 2) WGC：画面变了系统才送一帧过来；回调里直接算竖带指纹
    frames, lock = [], threading.Lock()
    cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

    @cap.event
    def on_frame_arrived(frame, control):
        t = time.perf_counter()
        rows = P.strip_rows(frame.frame_buffer[:, :, :3])
        with lock:
            frames.append((time.perf_counter(), (time.perf_counter() - t) * 1000, rows, frame.width, frame.height))

    @cap.event
    def on_closed():
        pass

    ctl = cap.start_free_threaded()
    time.sleep(1.0)
    root.update()
    with lock:
        frames.clear()
    # 2a) 静止 2 秒：会不会一直送帧
    t_end = time.perf_counter() + 2
    while time.perf_counter() < t_end:
        root.update()
        time.sleep(0.01)
    with lock:
        res["WGC 静止 2 秒收到的帧数"] = len(frames)
        frames.clear()
    # 2b) 滚动 40 次
    tops, cpu2, wall2 = cpu_of(lambda: scroll_loop(root, cv))
    time.sleep(0.3)
    with lock:
        got = list(frames)
        frames.clear()
    res["WGC 滚动 40 次收到的帧数"] = len(got)
    res["WGC 帧尺寸"] = f"{got[0][3]}x{got[0][4]}" if got else None
    res["WGC 回调里算指纹 ms"] = round(float(np.median([g[1] for g in got])), 1) if got else None
    res["WGC 额外 CPU 秒/40 次滚动"] = round(cpu2 - cpu0, 2)
    # 用相邻帧估偏移，看总和对不对得上真实滚动距离
    shifts = []
    for a, b in zip(got, got[1:]):
        per = [P.shift_of(x, y)[0] for x, y in zip(a[2], b[2])]
        ok = [p for p in per if p is not None]
        shifts.append(max(set(ok), key=ok.count) if ok else None)
    res["WGC 相邻帧都能接上"] = f"{sum(s is not None for s in shifts)}/{len(shifts)}"
    res["WGC 估出的滚动总和 / 真实"] = f"{sum(s for s in shifts if s)} / {tops[-1] - tops[0] + STEP_PX}"

    # 3) 被别的窗口完全挡住时，WGC 还收不收得到帧
    cover = tk.Toplevel(root)
    cover.geometry(f"{w}x{h}+0+0")
    cover.attributes("-topmost", True)
    tk.Label(cover, text="挡在上面的窗口", font=("Microsoft YaHei UI", 40)).pack(expand=True)
    root.update()
    time.sleep(0.5)
    with lock:
        frames.clear()
    scroll_loop(root, cv)
    time.sleep(0.3)
    with lock:
        hidden = list(frames)
    cover.destroy()
    shifts = []
    for a, b in zip(hidden, hidden[1:]):
        per = [P.shift_of(x, y)[0] for x, y in zip(a[2], b[2])]
        ok = [p for p in per if p is not None]
        shifts.append(max(set(ok), key=ok.count) if ok else None)
    res["被完全挡住时：收到帧数"] = len(hidden)
    res["被完全挡住时：估出滚动、每步"] = sorted(set(shifts))
    ctl.stop()
    root.destroy()
    return res


if __name__ == "__main__":
    out = {"1600x1000": run(1600, 1000), "3800x2050": run(3800, 2050)}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    print(text)
    Path(__file__).with_name("wgc_results.json").write_text(text, encoding="utf-8")
