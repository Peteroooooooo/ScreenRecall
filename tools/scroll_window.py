"""资源 / 就绪延迟实测用的"自动翻页"：向指定窗口投递鼠标滚轮消息（不移动鼠标、不要求窗口在前台）。

  python tools/scroll_window.py test <hwnd> [--target <子窗口>]          滚几格再滚回，报告画面变化比例
  python tools/scroll_window.py run  <hwnd> [--target <子窗口>] --minutes 15 --every 10 --notches 8

Qt 程序（如 WPS）要把消息投给文档视图所在的子窗口（--target），投给顶层窗口不起作用；
子窗口句柄可用 EnumChildWindows 找面积最大、包含窗口中心的那个。节奏：连续向下 4–8 次后向上 3–8 次，循环。
2026-10-23 Windows 实测（results/windows/2026-10-23-DESKTOP-PH4UL0H）用的就是它。
"""
import argparse
import ctypes
import random
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from screenrecall import winapi  # noqa: E402

WM_MOUSEWHEEL = 0x020A


def _user32():
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return u


def post_wheel(user32, hwnd: int, notches: int) -> None:
    """notches < 0 向下；坐标取窗口中心（屏幕物理像素），每格一条消息。"""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    step = 120 if notches > 0 else -120
    for _ in range(abs(notches)):
        user32.PostMessageW(hwnd, WM_MOUSEWHEEL, (step & 0xFFFF) << 16, lparam)
        time.sleep(0.03)


def _grab(hwnd: int) -> np.ndarray:
    c = winapi.capture_window(hwnd)
    return np.frombuffer(c.bgr, np.uint8).reshape(c.height, c.width, 3)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["test", "run"])
    ap.add_argument("hwnd", type=int, help="录制的顶层窗口")
    ap.add_argument("--target", type=int, help="接收滚轮消息的子窗口（默认就是 hwnd）")
    ap.add_argument("--minutes", type=float, default=15)
    ap.add_argument("--every", type=float, default=10, help="平均间隔秒数（实际在 0.7–1.3 倍间随机）")
    ap.add_argument("--notches", type=int, default=8)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    winapi.enable_dpi_awareness()
    user32 = _user32()
    tgt = a.target or a.hwnd
    if a.cmd == "test":
        before = _grab(a.hwnd)
        post_wheel(user32, tgt, -a.notches)
        time.sleep(1.5)
        mid = _grab(a.hwnd)
        post_wheel(user32, tgt, a.notches)
        time.sleep(1.5)
        after = _grab(a.hwnd)
        d1 = float((np.abs(before.astype(int) - mid.astype(int)).sum(2) > 30).mean())
        d2 = float((np.abs(before.astype(int) - after.astype(int)).sum(2) > 30).mean())
        print(f"向下滚动后画面变化 {d1:.1%}；滚回后与开始相比 {d2:.1%}")
        return
    rng = random.Random(a.seed)
    end = time.monotonic() + a.minutes * 60
    direction, run_len, n = -1, rng.randint(4, 8), 0
    print(f"每约 {a.every} 秒滚动窗口 {a.hwnd}，共 {a.minutes} 分钟", flush=True)
    while time.monotonic() < end:
        post_wheel(user32, tgt, direction * a.notches)
        n += 1
        if n >= run_len:
            direction, run_len, n = -direction, rng.randint(3, 8), 0
        time.sleep(max(0.5, a.every * rng.uniform(0.7, 1.3)))
    print("done", flush=True)


if __name__ == "__main__":
    main()
