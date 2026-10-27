"""录制快路径的开销实测（不是测试，直接运行）：

    python tests_win/bench_rec.py            # 合成画面：每帧各步耗时 + 10 秒 10 帧/秒连续滚动的 CPU
    python tests_win/bench_rec.py --real     # 另外在自己开的 tkinter 窗口上跑真实 WGC 会话

只用 PIL 合成的画面和本脚本自己开的窗口，只打印数字。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]

from screenrecall.rec import motion as M  # noqa: E402
from screenrecall.rec.frames import Frame, FrameMap  # noqa: E402
from screenrecall.rec.tracker import NullSink, WindowTracker  # noqa: E402
from tests import _rec_synth as S  # noqa: E402

NCPU = psutil.cpu_count()
STEP_PX = 60  # 每帧滚 60 像素（10 帧/秒 = 600 px/s）


def med(xs):
    return round(float(np.median(xs)), 2)


def doc_bgra(w: int, h: int, frames: int) -> np.ndarray:
    bgr = S.document(w, h + STEP_PX * frames + 40, 21)
    out = np.empty(bgr.shape[:2] + (4,), np.uint8)
    out[:, :, :3] = bgr
    out[:, :, 3] = 255
    return out


def per_frame(w: int, h: int, n: int = 40) -> dict:
    doc = doc_bgra(w, h, n + 2)
    views = [doc[i * STEP_PX: i * STEP_PX + h] for i in range(n + 1)]
    buf = np.empty((h, w, 4), np.uint8)
    t_copy, t_gray, t_fp, t_cmp, t_an, t_feed = [], [], [], [], [], []
    prev = None
    for v in views:
        t0 = time.perf_counter()
        np.copyto(buf, v)                       # 截图回调里唯一做的事
        t1 = time.perf_counter()
        fp = M.Fp.from_image(buf)               # take() 之后：绿色通道 >> 4
        t2 = time.perf_counter()
        fp.v                                    # 竖带指纹
        t3 = time.perf_counter()
        if prev is not None:
            eq = M.rows_equal(prev, fp)
            tiles = M.tile_changes(prev, fp)
            t4 = time.perf_counter()
            m = M.analyze(prev, fp, rows_eq=eq, tiles=tiles)
            t5 = time.perf_counter()
            assert m.kind == "shift" and m.dy == STEP_PX
            t_cmp.append((t4 - t3) * 1000)
            t_an.append((t5 - t4) * 1000)
        t_copy.append((t1 - t0) * 1000)
        t_gray.append((t2 - t1) * 1000)
        t_fp.append((t3 - t2) * 1000)
        prev = fp
    # 整个 tracker.feed（每帧新拷一份，和真实流程一样）
    tr = WindowTracker(1, NullSink())
    t = 0
    for v in views:
        t += 100
        f = Frame.from_bgra(1, t, v.copy(), FrameMap(0, 0, w, h))
        t0 = time.perf_counter()
        tr.feed(f)
        t_feed.append((time.perf_counter() - t0) * 1000)
    # 一次静止：转 BGR + 和已存帧比对 + 算"没存过的格子"
    t0 = time.perf_counter()
    tr.tick(t + 1100)
    t_still = (time.perf_counter() - t0) * 1000
    return {
        "尺寸": f"{w}x{h}",
        "回调拷贝 BGRA ms": med(t_copy),
        "转灰度 ms": med(t_gray),
        "竖带指纹 ms": med(t_fp),
        "整行比较+小块变化 ms": med(t_cmp),
        "analyze ms": med(t_an),
        "tracker.feed 整帧（含灰度、指纹、比对）ms": med(t_feed[1:]),
        "一次静止（转 BGR + 覆盖判断）ms": round(t_still, 1),
    }


def cpu_10s(w: int, h: int, fps: float = 10.0, seconds: float = 10.0) -> dict:
    """模拟录制进程：截图线程每 1/fps 秒送一帧（拷进槽），分析线程 40 ms 一轮取帧、喂 tracker、tick。"""
    import threading

    from screenrecall.rec.wgc import _SlotSource

    n = int(fps * seconds) + 2
    doc = doc_bgra(w, h, n)
    src = _SlotSource(1, None, None, fps, None)
    from screenrecall.rec.wgc import _BufferPool

    pool = _BufferPool()
    stop = threading.Event()

    def producer():
        i = 0
        nxt = time.perf_counter()
        while not stop.is_set() and i < n:
            v = doc[i * STEP_PX: i * STEP_PX + h]
            b = pool.get(v.shape)
            np.copyto(b, v)
            src._put(b, src._clock(), (0, 0, w, h), True)
            i += 1
            nxt += 1 / fps
            time.sleep(max(0.0, nxt - time.perf_counter()))

    tr = WindowTracker(1, NullSink())
    me = psutil.Process()
    th = threading.Thread(target=producer, daemon=True)
    c0, w0 = me.cpu_times(), time.perf_counter()
    th.start()
    fed = 0
    while time.perf_counter() - w0 < seconds:
        f = src.take()
        if f is not None:
            tr.feed(f)
            fed += 1
        tr.tick(src._clock())
        time.sleep(0.04)
    stop.set()
    th.join()
    c1, w1 = me.cpu_times(), time.perf_counter()
    busy = (c1.user + c1.system - c0.user - c0.system) / (w1 - w0)
    return {"尺寸": f"{w}x{h}", "帧率": fps, "喂帧": fed, "跟踪位置": tr.pos_y,
            "CPU（占一个核 %）": round(busy * 100, 1), "CPU（占整机 %）": round(busy * 100 / NCPU, 2)}


def real_wgc(seconds: float = 10.0) -> dict:
    """真实 WGC：自己的 tkinter 窗口 2082x1491，每 100 ms 滚 60 像素。对照组：同样滚动但不截图。"""
    from _tkdoc import TkDoc

    from screenrecall.rec.clock import now_ms
    from screenrecall.rec.wgc import CaptureSession

    d = TkDoc(w=2082, h=1491 - 31, x=20, y=20, lines=1200)
    me = psutil.Process()
    out = {}
    try:
        def run(capture: bool):
            d.cv.yview_moveto(0)
            d.pump(1.0)
            src = CaptureSession(d.hwnd, max_fps=10) if capture else None
            tr = WindowTracker(d.hwnd, NullSink())
            if src:
                src.start()
                d.pump(0.5)
            feed_ms = []
            c0, w0 = me.cpu_times(), time.perf_counter()
            nxt = w0
            while time.perf_counter() - w0 < seconds:
                d.scroll(STEP_PX)
                nxt += 0.1
                while time.perf_counter() < nxt:
                    d.root.update()
                    if src:
                        f = src.take()
                        if f is not None:
                            t0 = time.perf_counter()
                            tr.feed(f)
                            feed_ms.append((time.perf_counter() - t0) * 1000)
                        tr.tick(now_ms())
                    time.sleep(0.01)
            c1, w1 = me.cpu_times(), time.perf_counter()
            if src:
                src.stop()
            busy = (c1.user + c1.system - c0.user - c0.system) / (w1 - w0)
            res = {"CPU（占一个核 %）": round(busy * 100, 1)}
            if src:
                res.update({"收到帧": src.frames_seen, "喂帧": tr.frames, "接不上（新开的块）": tr.block,
                            "帧尺寸": f"{tr.latest.shape[1]}x{tr.latest.shape[0]}" if tr.latest else None,
                            "tracker.feed ms（中位）": med(feed_ms) if feed_ms else None})
            return res

        base = run(False)
        cap = run(True)
        out = {"只滚不截（对照）": base, "滚动 + WGC 10 帧/秒 + 跟踪": cap,
               "截图和跟踪额外 CPU（占一个核 %）": round(cap["CPU（占一个核 %）"] - base["CPU（占一个核 %）"], 1)}
    finally:
        d.close()
    return out


def main():
    res = {"逻辑核数": NCPU,
           "每帧耗时（中位）": [per_frame(2082, 1491), per_frame(3840, 2160)],
           "10 秒连续滚动 10 帧/秒（合成）": [cpu_10s(2082, 1491), cpu_10s(3840, 2160)]}
    if "--real" in sys.argv:
        res["真实 WGC（tkinter 窗口）"] = real_wgc()
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
