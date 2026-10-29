"""真实 Edge 里的滚动实测：上下、左右、视差、钉住段落、淡入，以及真实网站。

开一个独立的 Edge（单独的配置目录，不碰用户自己的浏览器），用 DevTools 协议发滚轮事件，
用 Windows.Graphics.Capture 截这个窗口，只靠像素估偏移，再和浏览器报告的真实滚动位置对比。
不存任何截图，只输出数字。
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

import numpy as np
import websocket
from windows_capture import WindowsCapture

sys.path.insert(0, str(Path(__file__).parent))
import motion as M  # noqa: E402

from screenrecall import winapi  # noqa: E402

winapi.enable_dpi_awareness()
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333
PAGE = (Path(__file__).parent / "pages" / "test_page.html").resolve().as_uri()
SITES = {
    "维基百科长文": "https://zh.wikipedia.org/wiki/%E5%A4%A7%E5%9E%8B%E8%AF%AD%E8%A8%80%E6%A8%A1%E5%9E%8B",
    "苹果落地页": "https://www.apple.com/airpods-pro/",
}


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        self.i = 0

    def call(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r["result"].get("value")

    def wheel(self, x, y, dx=0, dy=0):
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)


class Grabber:
    """WGC：画面变了才送帧。记住最新一帧；需要时也记下全部帧。"""

    def __init__(self, hwnd, interval_ms=None):
        self.lock = threading.Lock()
        self.latest = None
        self.n = 0
        self.keep_all = False
        self.all = []
        cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd, minimum_update_interval=interval_ms)

        @cap.event
        def on_frame_arrived(frame, control):
            g = M.green(frame.frame_buffer).copy()
            with self.lock:
                self.latest = g
                self.n += 1
                if self.keep_all:
                    self.all.append(g)

        @cap.event
        def on_closed():
            pass

        self.ctl = cap.start_free_threaded()

    def frame(self):
        with self.lock:
            return self.latest

    def stop(self):
        self.ctl.stop()


def find_hwnd(title_part, timeout=20):
    t_end = time.time() + timeout
    while time.time() < t_end:
        for w in winapi.list_windows():
            if title_part in w.title and w.process.lower() == "msedge.exe":
                return w.hwnd
        time.sleep(0.3)
    raise RuntimeError("找不到 Edge 窗口")


def settle(g: Grabber, wait=0.4, quiet=0.4, max_wait=4.0):
    """等到连续 quiet 秒没有新帧（平滑滚动和动画都停了），再取最新一帧。"""
    time.sleep(wait)
    t_end = time.time() + max_wait
    last_n, last_t = g.n, time.time()
    while time.time() < t_end:
        time.sleep(0.05)
        if g.n != last_n:
            last_n, last_t = g.n, time.time()
        elif time.time() - last_t >= quiet:
            break
    return g.frame()


def judge(res, truth_dy, truth_dx):
    tol = 2
    vy, vx = res["上下结论"], res["左右结论"]
    if truth_dx:
        if vx is None:
            return "接不上"
        return "对" if abs(vx - truth_dx) <= tol else f"错（估 {vx}）"
    if vy is None:
        return "接不上"
    if vy == 0 and truth_dy != 0:
        return "原地变（画面没平移）"  # 页面在滚，但画面是钉住的、原地在变：没有可对齐的平移
    return "对" if abs(vy - truth_dy) <= tol else f"错（估 {vy}）"


def run_steps(cdp, g, dpr, name, setup_js, steps, point_js="[400, 400]", strip=False):
    cdp.js(setup_js)
    prev = settle(g, 1.0)
    st = json.loads(cdp.js("JSON.stringify({y:scrollY, x:document.getElementById('hs')?document.getElementById('hs').scrollLeft:0})"))
    px, py = cdp.js(point_js)
    rows = []
    for dx, dy in steps:
        cdp.wheel(px, py, dx, dy)
        cur = settle(g)
        nt = json.loads(cdp.js("JSON.stringify({y:scrollY, x:document.getElementById('hs')?document.getElementById('hs').scrollLeft:0})"))
        tdy, tdx = round((nt["y"] - st["y"]) * dpr), round((nt["x"] - st["x"]) * dpr)
        r = M.analyze(prev, cur)
        row = {"滚轮": [dx, dy], "真实上下": tdy, "真实左右": tdx, "估上下": r["上下结论"], "估左右": r["左右结论"],
               "同意(上下)": r["上下同意"], "同意(左右)": r["左右同意"], "整行没变": r["整行没变的比例"], "判定": judge(r, tdy, tdx)}
        if strip:
            row["各竖带"] = r["dy"]
        if row["判定"] != "对":
            later = settle(g, 1.5)
            r2 = M.analyze(prev, later)
            row["补等 1.5 秒后重估"] = judge(r2, tdy, tdx)
            row["补等期间新帧"] = bool((later != cur).any())
            cur = later
        rows.append(row)
        prev, st = cur, nt
    ok = sum(r["判定"] == "对" for r in rows)
    return {"场景": name, "对": f"{ok}/{len(rows)}", "接不上": sum(r["判定"] == "接不上" for r in rows),
            "原地变": sum(r["判定"].startswith("原地") for r in rows),
            "估错": sum(r["判定"].startswith("错") for r in rows), "明细": rows}


def fling(cdp, g, dpr, speed_css_per_s, seconds=1.5, notch=100):
    """连续滑动：滚轮一格 100 CSS 像素，按给定速度连续发。期间收到的帧两两估偏移，累加后和真实距离比。
    规则：滚轮在滚、却估出"没动"或给不出结论、而且画面大半变了 → 记为"接不上"，不当成没动。"""
    cdp.js("scrollTo(0, 0)")
    time.sleep(1.0)
    y0 = cdp.js("scrollY")
    with g.lock:
        g.all = [g.latest]
        g.keep_all = True
    gap = notch / speed_css_per_s
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        cdp.wheel(400, 400, 0, notch)
        time.sleep(gap)
    settle(g, 0.3, quiet=0.6)
    with g.lock:
        g.keep_all = False
        frames = list(g.all)
    y1 = cdp.js("scrollY")
    total, breaks, joined = 0, 0, 0
    for a, b in zip(frames, frames[1:]):
        r = M.analyze(a, b)
        v = r["上下结论"]
        if v in (None, 0):
            if r["整行没变的比例"] < 0.6:
                breaks += 1
        else:
            total += v
            joined += 1
    return {"速度(CSS px/秒)": speed_css_per_s, "收到帧数": len(frames), "真实总距离(px)": round((y1 - y0) * dpr),
            "接上的帧对": joined, "接上部分的累加(px)": total, "接不上": breaks,
            "全程都接上": breaks == 0 and abs(total - round((y1 - y0) * dpr)) <= 3 * max(joined, 1)}


def revisit(cdp, g):
    """动画页面：回到同一个滚动位置，画面是不是一模一样（能不能靠这个去重）。"""
    out = {}
    for name, sec in [("钉住段落", "pin"), ("视差", "parallax")]:
        top = cdp.js(f"document.getElementById('{sec}').offsetTop")
        target = top + 700
        cdp.js(f"scrollTo(0, {target})")
        a = settle(g, 1.0)
        cdp.js(f"scrollTo(0, {target + 900})")
        settle(g, 0.8)
        cdp.js(f"scrollTo(0, {target})")
        b = settle(g, 1.0)
        same = float(np.mean(a == b))
        out[name] = f"回到同一位置后，像素完全一样的比例 {same:.1%}"
    return out


def main():
    prof = tempfile.mkdtemp(prefix="sr-edge-")
    proc = subprocess.Popen([EDGE, f"--user-data-dir={prof}", f"--remote-debugging-port={PORT}", "--no-first-run",
                             "--no-default-browser-check", "--disable-extensions", "--window-position=60,40",
                             "--window-size=1400,1000", "--disable-features=CalculateNativeWinOcclusion",
                             "--new-window", PAGE])
    res = {}
    g = None
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
        g = Grabber(hwnd)
        dpr = cdp.js("devicePixelRatio")
        res["环境"] = {"devicePixelRatio": dpr, "视口(CSS px)": cdp.js("[innerWidth, innerHeight]"), "截图尺寸": None}
        time.sleep(1.0)
        f = g.frame()
        res["环境"]["截图尺寸"] = f"{f.shape[1]}x{f.shape[0]}" if f is not None else "没收到帧"
        sec = lambda s: f"document.getElementById('{s}').offsetTop"  # noqa: E731
        hs_pt = "(r => [r.left + r.width / 2, r.top + r.height / 2])(document.getElementById('hs').getBoundingClientRect())"
        res["上下：长文章"] = run_steps(cdp, g, dpr, "长文章", "scrollTo(0,0)",
                                    [(0, 100)] * 5 + [(0, 300)] * 3 + [(0, -200)] * 3 + [(0, 600)] * 2 + [(0, -500), (0, 750)])
        res["左右：卡片横滑"] = run_steps(cdp, g, dpr, "卡片横滑", f"scrollTo(0, {sec('hs')} - 200)",
                                     [(200, 0)] * 4 + [(-300, 0)] * 2 + [(600, 0), (150, 0)], point_js=hs_pt)
        res["视差"] = run_steps(cdp, g, dpr, "视差", f"scrollTo(0, {sec('parallax')})", [(0, 150)] * 6 + [(0, -150)] * 2, strip=True)
        res["钉住段落"] = run_steps(cdp, g, dpr, "钉住段落", f"scrollTo(0, {sec('pin')} + 10)", [(0, 200)] * 8 + [(0, -200)] * 2, strip=True)
        res["淡入"] = run_steps(cdp, g, dpr, "淡入", f"scrollTo(0, {sec('reveal')} - 500)", [(0, 150)] * 6)
        res["左右：卡片横滑（重跑 3 次）"] = [
            {k: v for k, v in run_steps(cdp, g, dpr, "卡片横滑", f"scrollTo(0, {sec('hs')} - 200); document.getElementById('hs').scrollLeft = 0",
                                        [(200, 0)] * 4 + [(-300, 0)] * 2 + [(600, 0), (150, 0)], point_js=hs_pt).items() if k != "明细"}
            for _ in range(3)]
        res["动画页回到同一位置"] = revisit(cdp, g)
        g.stop()
        for fps, iv in [(10, 100), (4, 250)]:
            g = Grabber(hwnd, interval_ms=iv)
            time.sleep(0.5)
            res[f"连续滑动（最多 {fps} 帧/秒）"] = [fling(cdp, g, dpr, s) for s in (1000, 2000, 4000, 7000)]
            g.stop()
        g = Grabber(hwnd)
        for name, url in SITES.items():
            cdp.call("Page.enable")
            cdp.call("Page.navigate", url=url)
            time.sleep(8)
            ok = cdp.js("document.readyState")
            res[name] = run_steps(cdp, g, dpr, name, "scrollTo(0, 0)", [(0, 300)] * 10 + [(0, -300)] * 3)
            res[name]["页面状态"] = ok
            res[name]["页面高度(CSS)"] = cdp.js("document.documentElement.scrollHeight")
    finally:
        if g:
            g.stop()
        proc.terminate()
        time.sleep(1)
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    text = json.dumps(res, ensure_ascii=False, indent=1)
    Path(__file__).with_name("edge_results.json").write_text(text, encoding="utf-8")
    # 打印时去掉明细，明细在文件里
    print(json.dumps({k: ({kk: vv for kk, vv in v.items() if kk != "明细"} if isinstance(v, dict) else v) for k, v in res.items()},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
