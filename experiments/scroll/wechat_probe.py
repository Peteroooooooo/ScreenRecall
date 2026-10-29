"""微信实测（用户已允许）：只统计数字，不存图、不输出任何文字内容。

用 WGC 截微信主窗口，往聊天区发滚轮消息（往上翻 5 次、再往下翻回 5 次），只靠像素估偏移。
没有"真实答案"，所以检查两件事：每格滚轮估出的像素是否一致；翻上去再翻回来，画面是否回到原样。
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
from windows_capture import WindowsCapture

sys.path.insert(0, str(Path(__file__).parent))
import motion as M  # noqa: E402

from screenrecall import winapi  # noqa: E402

winapi.enable_dpi_awareness()
user32 = ctypes.windll.user32
WM_MOUSEWHEEL = 0x020A


def main():
    wins = [w for w in winapi.list_windows() if w.process.lower() == "weixin.exe" and w.width > 400]
    if not wins:
        print(json.dumps({"结果": "没找到可见的微信主窗口（可能最小化或没开）"}, ensure_ascii=False))
        return
    w = max(wins, key=lambda x: x.width * x.height)
    lock, state = threading.Lock(), {"g": None, "n": 0}
    cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=w.hwnd)

    @cap.event
    def on_frame_arrived(frame, control):
        g = M.green(frame.frame_buffer).copy()
        with lock:
            state["g"], state["n"] = g, state["n"] + 1

    @cap.event
    def on_closed():
        pass

    ctl = cap.start_free_threaded()

    def settle():
        time.sleep(0.4)
        last, t = state["n"], time.time()
        while time.time() - t < 0.5:
            time.sleep(0.05)
            if state["n"] != last:
                last, t = state["n"], time.time()
        with lock:
            return state["g"]

    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

    rc = RECT()
    user32.GetWindowRect(w.hwnd, ctypes.byref(rc))
    x, y = int(rc.l + (rc.r - rc.l) * 0.65), int(rc.t + (rc.b - rc.t) * 0.5)  # 右侧聊天区中间

    def wheel(notches):
        wparam = (ctypes.c_short(120 * notches).value & 0xFFFF) << 16
        lparam = (y & 0xFFFF) << 16 | (x & 0xFFFF)
        user32.PostMessageW(w.hwnd, WM_MOUSEWHEEL, wparam, lparam)

    first = settle()
    if first is None:
        print(json.dumps({"结果": "WGC 没收到微信的画面"}, ensure_ascii=False))
        ctl.stop()
        return
    prev, rows = first, []
    for notches in [3] * 5 + [-3] * 5:  # 正 = 往上翻历史
        wheel(notches)
        cur = settle()
        r = M.analyze(prev, cur)
        rows.append({"滚轮格数": notches, "估上下": r["上下结论"], "同意": r["上下同意"], "整行没变": r["整行没变的比例"],
                     "各竖带": r["dy"]})
        prev = cur
    ctl.stop()
    ests = [r["估上下"] for r in rows if r["估上下"] not in (None, 0)]
    out = {
        "窗口尺寸": f"{first.shape[1]}x{first.shape[0]}",
        "每步": [{k: v for k, v in r.items()} for r in rows],
        "估出的偏移（像素）": [r["估上下"] for r in rows],
        "往上合计 / 往下合计": [sum(r["估上下"] or 0 for r in rows[:5]), sum(r["估上下"] or 0 for r in rows[5:])],
        "翻回来后和一开始像素完全一样的比例": f"{float(np.mean(first == prev)):.1%}",
        "有结论的步数": f"{len(ests)}/{len(rows)}",
    }
    text = json.dumps(out, ensure_ascii=False, indent=1)
    Path(__file__).with_name("wechat_results.json").write_text(text, encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "每步"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
