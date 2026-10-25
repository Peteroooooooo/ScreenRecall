"""A13 测试窗口：python tools/a13_window.py --dpi aware|unaware --word Marigold --tag 125 --out info.json

白底画布上画几行已知文字（大小字号、英文、代码、中文、小字）和四角四个品红方块。窗口画好后把
句柄、本进程看到的窗口矩形和画布原点、每行文字和方块在画布里的位置写进 --out，供 a13_check 核对：
截图边界对不对、OCR 框是不是落在文字上。

--dpi aware   按显示器 DPI 感知（坐标是物理像素，按缩放放大布局，和正常的现代程序一样）
--dpi unaware 不感知 DPI（系统按逻辑尺寸画、再整体放大；后台走 print_window_logical 分支）
"""
import argparse
import ctypes
import json
import os
import tkinter as tk
from ctypes import wintypes

LINES = [
    # (x, y, 字体, 字号, 粗细, 文字)；坐标和字号都按 100% 缩放写，aware 时乘缩放倍数
    (40, 34, "Segoe UI", 26, "bold", "{word} {tag} {mode}"),
    (40, 112, "Segoe UI", 14, "normal", "The quick brown fox jumps over 4718 lazy dogs"),
    (40, 162, "Consolas", 12, "normal", "def merge(a, b): return sorted(a + b)"),
    (40, 208, "Microsoft YaHei UI", 14, "normal", "缩放对齐测试：截图与高亮框应该重合"),
    (300, 300, "Segoe UI", 10, "normal", "Order HZ20261025-0042 at 14:30, total 127.50"),
    (430, 400, "Segoe UI", 18, "normal", "Bottom right 9931"),
]
W, H = 760, 500       # 画布逻辑尺寸
MARK, INSET = 14, 10  # 角标边长、离画布边的距离（逻辑像素）


def set_awareness(mode: str) -> str:
    u = ctypes.windll.user32
    try:
        u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4 if mode == "aware" else -1))  # PMv2 / UNAWARE
    except AttributeError:
        pass
    ctx = u.GetThreadDpiAwarenessContext()
    aw = u.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(ctx))
    return {0: "unaware", 1: "system", 2: "per_monitor"}.get(aw, str(aw))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpi", choices=("aware", "unaware"), required=True)
    ap.add_argument("--word", required=True)
    ap.add_argument("--tag", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--x", type=int, default=120)
    ap.add_argument("--y", type=int, default=90)
    a = ap.parse_args()

    awareness = set_awareness(a.dpi)
    root = tk.Tk()
    dpi = root.winfo_fpixels("1i")
    s = dpi / 96 if a.dpi == "aware" else 1.0  # aware 时布局按物理像素放大；unaware 时系统替我们放大
    root.title(f"ScreenRecall A13 {a.word} ({a.dpi})")
    root.configure(bg="white")
    root.geometry(f"{round(W * s)}x{round(H * s)}+{round(a.x * s)}+{round(a.y * s)}")
    cv = tk.Canvas(root, width=round(W * s), height=round(H * s), bg="white", highlightthickness=0, bd=0)
    cv.pack(fill="both", expand=True)

    items = []
    for x, y, fam, size, weight, text in LINES:
        text = text.format(word=a.word, tag=a.tag, mode=a.dpi)
        # 字号用像素（负数）指定，aware / unaware 下都是同一套逻辑尺寸
        iid = cv.create_text(round(x * s), round(y * s), anchor="nw", text=text, fill="#111111",
                             font=(fam, -round(size * 96 / 72 * s), weight))
        items.append((iid, text))
    m, ins = round(MARK * s), round(INSET * s)
    cw, ch = round(W * s), round(H * s)
    marks = []
    for cx, cy in ((ins, ins), (cw - ins - m, ins), (ins, ch - ins - m), (cw - ins - m, ch - ins - m)):
        cv.create_rectangle(cx, cy, cx + m, cy + m, fill="#ff00ff", outline="")
        marks.append([cx + m / 2, cy + m / 2])

    def report() -> None:
        root.update_idletasks()
        hwnd = int(root.wm_frame(), 16)
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        info = {
            "pid": os.getpid(), "hwnd": hwnd, "mode": a.dpi, "awareness": awareness, "tk_dpi": dpi, "layout_scale": s,
            "title": root.title(), "window_rect": [r.left, r.top, r.right, r.bottom],
            "canvas_root": [cv.winfo_rootx(), cv.winfo_rooty()], "canvas_size": [cv.winfo_width(), cv.winfo_height()],
            "marks": marks,
            "lines": [{"text": t, "bbox": list(cv.bbox(i))} for i, t in items],
        }
        tmp = a.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
        os.replace(tmp, a.out)

    root.after(800, report)
    root.lift()
    root.focus_force()
    root.mainloop()


if __name__ == "__main__":
    main()
