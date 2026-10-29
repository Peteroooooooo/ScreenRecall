"""核对：motion_fast（省重复版）和 motion（原版）在真实 Edge 画面上结论是否完全一样，各花多少时间。
收集上下滚、左右滑、视差、钉住段落的相邻帧对，两版都算一遍。只输出数字。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import motion as M  # noqa: E402
import motion_fast as F  # noqa: E402
from edge_probe import CDP, EDGE, PAGE, Grabber, find_hwnd, settle  # noqa: E402

PORT = 9335


def collect(cdp, g, setup, steps, point="[400, 400]"):
    cdp.js(setup)
    frames = [settle(g, 1.0)]
    px, py = cdp.js(point)
    for dx, dy in steps:
        cdp.wheel(px, py, dx, dy)
        frames.append(settle(g, 0.3, quiet=0.3))
    return frames


def main():
    prof = tempfile.mkdtemp(prefix="sr-edge-eq-")
    proc = subprocess.Popen([EDGE, f"--user-data-dir={prof}", f"--remote-debugging-port={PORT}", "--no-first-run",
                             "--no-default-browser-check", "--disable-extensions", "--window-position=60,40",
                             "--window-size=1400,1000", "--disable-features=CalculateNativeWinOcclusion",
                             "--new-window", PAGE])
    pairs = []
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
        g = Grabber(find_hwnd("SR-SCROLL-TEST"))
        time.sleep(1.0)
        sec = lambda s: f"document.getElementById('{s}').offsetTop"  # noqa: E731
        hs_pt = "(r => [r.left + r.width / 2, r.top + r.height / 2])(document.getElementById('hs').getBoundingClientRect())"
        groups = [
            collect(cdp, g, "scrollTo(0,0)", [(0, 100)] * 5 + [(0, 300)] * 3 + [(0, -200)] * 3 + [(0, 600)] * 2 + [(0, 1500)]),
            collect(cdp, g, f"scrollTo(0, {sec('hs')} - 200); document.getElementById('hs').scrollLeft = 0",
                    [(200, 0)] * 4 + [(-300, 0)] * 2 + [(600, 0)], point=hs_pt),
            collect(cdp, g, f"scrollTo(0, {sec('parallax')})", [(0, 150)] * 6),
            collect(cdp, g, f"scrollTo(0, {sec('pin')} + 10)", [(0, 200)] * 6),
        ]
        # 连续滚动途中的帧（最密的情况）
        with g.lock:
            g.all, g.keep_all = [g.latest], True
        cdp.js("scrollTo(0,0)")
        for _ in range(30):
            cdp.wheel(400, 400, 0, 100)
            time.sleep(0.05)
        settle(g, 0.3, quiet=0.5)
        with g.lock:
            g.keep_all = False
            groups.append(list(g.all))
        g.stop()
        for fr in groups:
            pairs += [(a, b) for a, b in zip(fr, fr[1:]) if a is not None and b is not None]
    finally:
        proc.terminate()
        time.sleep(1)
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)

    # 快版按真实用法跑：每张图建一次 Fp，下一对接着用
    fps = {}
    fp = lambda x: fps.setdefault(id(x), F.Fp(x))  # noqa: E731
    same, diff, tm, tf = 0, [], [], []
    for a, b in pairs:
        t0 = time.perf_counter()
        rm = M.analyze(a, b)
        t1 = time.perf_counter()
        rf = F.analyze(fp(a), fp(b))
        t2 = time.perf_counter()
        tm.append((t1 - t0) * 1000)
        tf.append((t2 - t1) * 1000)
        # 上下有平移结论时快版不算左右，所以只在上下没平移时比左右
        lr_same = rm["上下结论"] not in (None, 0) or (rm["左右结论"], rm["dx"]) == (rf["左右结论"], rf["dx"])
        if (rm["上下结论"], rm["dy"]) == (rf["上下结论"], rf["dy"]) and lr_same:
            same += 1
        else:
            diff.append({"原版": [rm["上下结论"], rm["左右结论"]], "省重复版": [rf["上下结论"], rf["左右结论"]]})
    med = lambda xs: round(sorted(xs)[len(xs) // 2], 1)  # noqa: E731
    out = {"帧对数": len(pairs), "截图尺寸": f"{pairs[0][0].shape[1]}x{pairs[0][0].shape[0]}",
           "两版结论完全一样（含每条竖带、横带）": f"{same}/{len(pairs)}", "不一样的": diff[:10],
           "原版 每对 ms（中位/最慢）": [med(tm), round(max(tm), 1)], "省重复版 每对 ms（中位/最慢）": [med(tf), round(max(tf), 1)]}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    Path(__file__).with_name("equiv_results.json").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
