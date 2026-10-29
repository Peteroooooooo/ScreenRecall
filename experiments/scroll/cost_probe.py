"""开销实测：截图 + 算指纹 + 比对整套连着跑，CPU 用多少。

独立 Edge 打开测试页，分几段，每段 10 秒：
  静止（不动）、连续滚动且不截图（对照）、连续滚动 + 最多 10 张/秒、连续滚动 + 不限速。
每张图到了就在回调里做完整的"算指纹 + 和上一张比对"。只输出数字，不存图。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import psutil
from windows_capture import WindowsCapture

sys.path.insert(0, str(Path(__file__).parent))
import motion as M  # noqa: E402
import motion_fast  # noqa: E402
from edge_probe import CDP, EDGE, PAGE, find_hwnd  # noqa: E402

FAST = "fast" in sys.argv[1:]
A = motion_fast if FAST else M
PORT = 9334
SECONDS = 10
NCPU = psutil.cpu_count()


class Pipeline:
    """真实流程：每来一张 → 取绿色通道、算指纹并比对（motion.analyze）。记下每张花的时间。"""

    def __init__(self, hwnd, interval_ms):
        self.lock = threading.Lock()
        self.prev = None
        self.copy_ms, self.cmp_ms = [], []
        self.size = None
        cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd, minimum_update_interval=interval_ms)

        @cap.event
        def on_frame_arrived(frame, control):
            t0 = time.perf_counter()
            g = M.green(frame.frame_buffer).copy()
            t1 = time.perf_counter()
            if FAST:
                g = motion_fast.Fp(g)
            if self.prev is not None:
                A.analyze(self.prev, g)
            t2 = time.perf_counter()
            with self.lock:
                shape = g.g.shape if FAST else g.shape
                self.size = f"{shape[1]}x{shape[0]}"
                self.copy_ms.append((t1 - t0) * 1000)
                if self.prev is not None:
                    self.cmp_ms.append((t2 - t1) * 1000)
            self.prev = g

        @cap.event
        def on_closed():
            pass

        self.ctl = cap.start_free_threaded()

    def stop(self):
        self.ctl.stop()


def cpu_of(procs):
    t = 0.0
    for p in procs:
        try:
            c = p.cpu_times()
            t += c.user + c.system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return t


def scroller(cdp, stop_evt, speed):
    """来回连续滚：每 50 ms 发一次滚轮，到底就反向。"""
    step = speed * 0.05
    direction = 1
    while not stop_evt.is_set():
        y, maxy = cdp.js("[scrollY, document.documentElement.scrollHeight - innerHeight]")
        if y >= maxy - step:
            direction = -1
        elif y <= step:
            direction = 1
        cdp.wheel(400, 400, 0, step * direction)
        time.sleep(0.05)


def measure(name, cdp, hwnd, edge_procs, scroll, interval_ms, capture=True):
    cdp.js("scrollTo(0, 0)")
    time.sleep(1.5)
    me = psutil.Process()
    pipe = Pipeline(hwnd, interval_ms) if capture else None
    time.sleep(0.5)
    if pipe:
        with pipe.lock:
            pipe.copy_ms.clear()
            pipe.cmp_ms.clear()
    stop_evt = threading.Event()
    th = threading.Thread(target=scroller, args=(cdp, stop_evt, 2000), daemon=True) if scroll else None
    me0, edge0, sys0, w0 = me.cpu_times(), cpu_of(edge_procs()), psutil.cpu_times(), time.perf_counter()
    if th:
        th.start()
    time.sleep(SECONDS)
    me1, edge1, sys1, w1 = me.cpu_times(), cpu_of(edge_procs()), psutil.cpu_times(), time.perf_counter()
    stop_evt.set()
    if th:
        th.join()
    if pipe:
        pipe.stop()
    wall = w1 - w0
    mine = (me1.user + me1.system - me0.user - me0.system) / wall
    busy0 = sum(sys0) - sys0.idle
    busy1 = sum(sys1) - sys1.idle
    out = {
        "情况": name,
        "我们的进程 CPU（占一个核的 %）": round(mine * 100, 1),
        "我们的进程 CPU（占整台电脑的 %）": round(mine * 100 / NCPU, 2),
        "Edge 自己的 CPU（占一个核的 %）": round((edge1 - edge0) / wall * 100, 1),
        "整台电脑 CPU 忙的比例 %": round((busy1 - busy0) / (sum(sys1) - sum(sys0)) * 100, 1),
    }
    if pipe:
        with pipe.lock:
            n, cm, cp = len(pipe.copy_ms), sorted(pipe.cmp_ms), sorted(pipe.copy_ms)
        out["截图尺寸"] = pipe.size
        out["10 秒收到几张"] = n
        if cp:
            out["取图 每张 ms（中位/最慢）"] = [round(cp[len(cp) // 2], 1), round(cp[-1], 1)]
        if cm:
            out["算指纹+比对 每张 ms（中位/最慢）"] = [round(cm[len(cm) // 2], 1), round(cm[-1], 1)]
    return out


def main():
    prof = tempfile.mkdtemp(prefix="sr-edge-cost-")
    proc = subprocess.Popen([EDGE, f"--user-data-dir={prof}", f"--remote-debugging-port={PORT}", "--no-first-run",
                             "--no-default-browser-check", "--disable-extensions", "--window-position=60,40",
                             "--window-size=1400,1000", "--disable-features=CalculateNativeWinOcclusion",
                             "--new-window", PAGE])
    res = {"比对用的版本": "省重复版（motion_fast）" if FAST else "原版（Python 逐行）", "逻辑核数": NCPU, "CPU": None}
    try:
        for _ in range(60):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.5)
        cdp = CDP(page["webSocketDebuggerUrl"])
        time.sleep(1.5)
        hwnd = find_hwnd("SR-SCROLL-TEST")
        root = psutil.Process(proc.pid)

        def edge_procs():
            try:
                return [root] + root.children(recursive=True)
            except psutil.NoSuchProcess:
                return []

        res["CPU"] = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                                    capture_output=True, text=True).stdout.strip()
        res["结果"] = [
            measure("静止，截图开着（最多 1 张/秒）", cdp, hwnd, edge_procs, scroll=False, interval_ms=1000),
            measure("连续滚动，不截图（对照）", cdp, hwnd, edge_procs, scroll=True, interval_ms=None, capture=False),
            measure("连续滚动，最多 10 张/秒", cdp, hwnd, edge_procs, scroll=True, interval_ms=100),
            measure("连续滚动，不限速", cdp, hwnd, edge_procs, scroll=True, interval_ms=None),
        ]
    finally:
        proc.terminate()
        time.sleep(1)
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    Path(__file__).with_name("cost_results_fast.json" if FAST else "cost_results.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
