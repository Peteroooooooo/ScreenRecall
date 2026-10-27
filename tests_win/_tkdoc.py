"""集成检查用的 tkinter 窗口：一张很长的中英文画布，可以按像素滚动。只截这个窗口。"""

from __future__ import annotations

import ctypes
import random
import time
import tkinter as tk

from screenrecall import winapi

winapi.enable_dpi_awareness()  # 先于 Tk 创建：Tk 和截图都按物理像素

_WORDS = "屏幕 记忆 窗口 滚动 课程 项目 数据 模型 搜索 证据 时间 应用 文字 识别 组会 screen recall window scroll data block".split()


class TkDoc:
    def __init__(self, w: int = 1400, h: int = 900, x: int = 40, y: int = 40, title: str = "sr-rec-tk-check",
                 lines: int = 1500, line_h: int = 34, seed: int = 5):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.cv = tk.Canvas(self.root, bg="white", highlightthickness=0, yscrollincrement=1)
        self.cv.pack(fill="both", expand=True)
        rng = random.Random(seed)
        font = ("Microsoft YaHei UI", 14)
        for i in range(lines):
            text = " ".join(rng.choice(_WORDS) for _ in range(16)) + f" {rng.randint(0, 99999)}"
            self.cv.create_text(40, 20 + i * line_h, anchor="nw", font=font, text=text)
        self.cv.configure(scrollregion=(0, 0, w, lines * line_h + 40))
        self.pump(0.5)
        self.hwnd = int(ctypes.windll.user32.GetAncestor(self.cv.winfo_id(), 2))

    def pump(self, seconds: float, each=None) -> None:
        end = time.perf_counter() + seconds
        while True:
            self.root.update()
            if each is not None:
                each()
            if time.perf_counter() >= end:
                return
            time.sleep(0.005)

    def top(self) -> int:
        return int(self.cv.canvasy(0))

    def scroll(self, px: int) -> None:
        self.cv.yview_scroll(px, "units")
        self.root.update()

    def close(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass
