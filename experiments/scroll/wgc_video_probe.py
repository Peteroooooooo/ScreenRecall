"""视频一直在播、人没动时，WGC 会送多少帧；用 minimum_update_interval 限速后又是多少。只截自己开的窗口。"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from windows_capture import WindowsCapture

sys.path.insert(0, str(Path(__file__).parent))
import align_probe as P  # noqa: E402


def run(interval_ms):
    app = P.App(video=True)
    n, lock = [0], threading.Lock()
    cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=app.hwnd, minimum_update_interval=interval_ms)

    @cap.event
    def on_frame_arrived(frame, control):
        P.strip_rows(frame.frame_buffer[:, :, :3])
        with lock:
            n[0] += 1

    @cap.event
    def on_closed():
        pass

    ctl = cap.start_free_threaded()
    time.sleep(0.8)
    # 基线：只播视频不截图的 CPU 另测，这里先播 5 秒
    with lock:
        n[0] = 0
    c0 = time.process_time()
    t_end = time.perf_counter() + 5
    played = 0
    while time.perf_counter() < t_end:
        app.animate()
        app.root.update()
        played += 1
        time.sleep(0.03)
    cpu = time.process_time() - c0
    with lock:
        got = n[0]
    ctl.stop()
    app.close()
    return {"视频播了多少帧": played, "WGC 送来几帧": got, "5 秒 CPU 秒（含播放本身）": round(cpu, 2)}


def baseline():
    app = P.App(video=True)
    c0 = time.process_time()
    t_end = time.perf_counter() + 5
    played = 0
    while time.perf_counter() < t_end:
        app.animate()
        app.root.update()
        played += 1
        time.sleep(0.03)
    cpu = time.process_time() - c0
    app.close()
    return {"视频播了多少帧": played, "5 秒 CPU 秒": round(cpu, 2)}


if __name__ == "__main__":
    out = {"只播不截（基线）": baseline(), "WGC 不限速": run(None), "WGC 限速 1 秒一帧": run(1000)}
    text = json.dumps(out, ensure_ascii=False, indent=1)
    print(text)
    Path(__file__).with_name("wgc_video_results.json").write_text(text, encoding="utf-8")
