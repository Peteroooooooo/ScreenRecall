"""只靠截图判断"哪块在滚、滚了多少"的实测。

只截本脚本自己开的 tkinter 窗口（模拟聊天软件 / 视频网页），不碰任何真实应用。
用法：python experiments/scroll/align_probe.py  → 结果打印为 JSON，并写到 experiments/scroll/align_results.json
"""
from __future__ import annotations

import ctypes
import json
import random
import time
import tkinter as tk
from pathlib import Path

import numpy as np

from screenrecall import winapi

winapi.enable_dpi_awareness()
K = 4  # 纵向每 K 行像素取一行
STRIPS = 12  # 窗口竖着切成几条
FONT = ("Microsoft YaHei UI", 13)
WORDS = "检索 增强 模型 编造 数据集 网盘 组会 下午 实验 结果 表格 方法 引言 附录 代码 报错 修复 版本 发布 测试".split()


def sentence(rng: random.Random) -> str:
    if rng.random() < 0.12:
        return "好的"  # 聊天里常见的重复短句
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 12))) + f" {rng.randint(0, 9999)}"


# ---------------- 截图 → 每条竖带的行指纹 ----------------

def grab(hwnd: int) -> np.ndarray:
    c = winapi.capture_window(hwnd)
    return np.frombuffer(c.bgr, np.uint8).reshape(c.height, c.width, 3)


def strip_rows(img: np.ndarray, strips: int = STRIPS) -> list[list[int]]:
    """每一行像素都算指纹（横向隔一个取一个）。旧图只用其中每 K 行，新图全部用，这样任何滚动量都能对上。"""
    g = img[:, :, 1] >> 4
    w = g.shape[1]
    edges = np.linspace(0, w, strips + 1).astype(int)
    return [[hash(r.tobytes()) for r in g[:, edges[i]:edges[i + 1]:2]] for i in range(strips)]


def shift_of(prev: list[int], cur: list[int]) -> tuple[int | None, float]:
    """一条竖带的偏移（像素，正 = 内容往上移 = 往下滚）和把握。对不上返回 None。"""
    pos: dict[int, list[int]] = {}
    for i in range(0, len(prev), K):
        pos.setdefault(prev[i], []).append(i)
    uniq = {r: v[0] for r, v in pos.items() if len(v) == 1}
    if len(uniq) < 6:
        return None, 0.0  # 这一条基本是空白，判断不了
    votes: dict[int, int] = {}
    for j, r in enumerate(cur):
        if r in uniq:
            d = uniq[r] - j
            votes[d] = votes.get(d, 0) + 1
    if not votes:
        return None, 0.0
    ranked = sorted(votes.values(), reverse=True)
    d, v = max(votes.items(), key=lambda x: x[1])
    second = ranked[1] if len(ranked) > 1 else 0
    # 不按"占全部行的比例"判断（视频、动画的行永远对不上，会把比例拉低），
    # 而是看：对上的行够不够多（≥ 8 行，约 32 像素），且明显多于第二名（≥ 3 倍）
    ok = v >= 8 and v >= 3 * second
    return (d if ok else None), round(v / max(second, 1), 1)


def regions(prev_strips, cur_strips):
    """每条竖带的偏移，再把相邻、偏移相同的竖带合成一块区域。"""
    per = [shift_of(p, c) for p, c in zip(prev_strips, cur_strips)]
    out, start = [], 0
    for i in range(1, len(per) + 1):
        if i == len(per) or per[i][0] != per[start][0]:
            out.append({"竖带": f"{start}-{i - 1}", "偏移": per[start][0]})
            start = i
    return per, out


# ---------------- 模拟窗口 ----------------

class App:
    """左：会话列表（独立滚动）；右：页面（上面一块"视频" + 弹幕，下面是文字），整页一起滚。"""

    def __init__(self, w=1600, h=1000, left_w=420, video=True):
        self.rng = random.Random(7)
        self.root = tk.Tk()
        self.root.title("sr-scroll-probe")
        self.root.geometry(f"{w}x{h}+0+0")
        self.left = tk.Canvas(self.root, width=left_w, bg="#f3f3f3", highlightthickness=0, yscrollincrement=1)
        self.right = tk.Canvas(self.root, bg="white", highlightthickness=0, yscrollincrement=1)
        self.left.pack(side="left", fill="y")
        self.right.pack(side="left", fill="both", expand=True)
        for i in range(400):
            self.left.create_text(16, 20 + i * 56, anchor="nw", font=FONT, text=f"会话 {i:03d}  {sentence(self.rng)[:14]}")
        y = 20
        self.video_cells, self.danmaku = [], []
        if video:
            vw, vh, cell = 1000, 560, 20
            self.vbox = (40, y, 40 + vw, y + vh)
            for cy in range(y, y + vh, cell):
                for cx in range(40, 40 + vw, cell):
                    self.video_cells.append(self.right.create_rectangle(cx, cy, cx + cell, cy + cell, width=0, fill="#000"))
            for k in range(14):
                self.danmaku.append(self.right.create_text(40 + self.rng.randint(0, vw // 2), y + 20 + k * 38, anchor="nw",
                                                           font=FONT, fill="white", text=sentence(self.rng)[:12]))
            y += vh + 30
        for i in range(600):
            self.right.create_text(40, y, anchor="nw", font=FONT, text=f"{sentence(self.rng)}")
            y += 34
        self.left.configure(scrollregion=(0, 0, left_w, 400 * 56 + 40))
        self.right.configure(scrollregion=(0, 0, w, y + 40))
        self.tick = 0
        self.root.update()
        time.sleep(0.4)
        self.root.update()
        self.hwnd = ctypes.windll.user32.GetAncestor(self.right.winfo_id(), 2)

    def animate(self):
        """视频换一帧：所有色块随机变色；弹幕往左飘。"""
        if not self.video_cells:
            return
        self.tick += 1
        r = self.rng
        for c in self.video_cells:
            self.right.itemconfigure(c, fill=f"#{r.randrange(0x1000000):06x}")
        for d in self.danmaku:
            self.right.move(d, -37, 0)
            x0, _, x1, _ = self.right.bbox(d)
            if x0 < self.vbox[0]:
                self.right.move(d, self.vbox[2] - x1 - 10, 0)  # 弹幕只在视频框里飘

    def scroll(self, canvas: str, px: int):
        getattr(self, canvas).yview_scroll(px, "units")

    def look(self):
        self.root.update()
        return grab(self.hwnd)

    def close(self):
        self.root.destroy()


# ---------------- 各项实测 ----------------

def t_two_panes():
    """一个窗口里两块各滚各的。"""
    app = App(video=False)
    cases = [("只滚右边 +120", 0, 120), ("只滚左边 +168", 168, 0), ("左 +112、右 -200", 112, -200), ("都不动", 0, 0)]
    prev = strip_rows(app.look())
    out = []
    for name, l, r in cases:
        if l:
            app.scroll("left", l)
        if r:
            app.scroll("right", r)
        cur = strip_rows(app.look())
        per, reg = regions(prev, cur)
        out.append({"操作": name, "左栏竖带(0-2)": [p[0] for p in per[:3]], "右栏竖带(3-11)": [p[0] for p in per[3:]], "合成的区域": reg})
        prev = cur
    # 整行比（旧方法）对照：只滚右边时会怎样
    app2 = App(video=False)
    a = app2.look()
    app2.scroll("right", 120)
    b = app2.look()
    whole = shift_of(strip_rows(a, 1)[0], strip_rows(b, 1)[0])
    app.close(), app2.close()
    return {"结果": out, "对照：整行一起比（只滚右边 +120）": {"偏移": whole[0], "把握": whole[1]}}


def t_video_scroll():
    """视频在播、弹幕在飘，同时整页往下/往上滚。"""
    app = App(video=True)
    steps = [80, 150, 60, -100, 200, 300, -250] + [random.Random(3).choice([-1, 1]) * random.Random(i).randint(30, 400) for i in range(20)]
    prev = strip_rows(app.look())
    out = []
    last_top = int(app.right.canvasy(0))
    for s in steps:
        app.animate()
        app.scroll("right", s)
        cur = strip_rows(app.look())
        per, _ = regions(prev, cur)
        right = [p for p in per[3:]]
        ok = [p[0] for p in right if p[0] is not None]
        voted = max(set(ok), key=ok.count) if ok else None
        top = int(app.right.canvasy(0))
        real = top - last_top
        last_top = top
        out.append({"真实滚动": real, "多数结论": voted, "对": voted == real, "有结论的竖带": len(ok)})
        prev = cur
    app.close()
    return {"对": f"{sum(o['对'] for o in out)}/{len(out)}", "估错（不是接不上，是给了错数）": [o for o in out if o["多数结论"] is not None and not o["对"]],
            "接不上": [o for o in out if o["多数结论"] is None], "明细前 7 步": out[:7]}


def t_video_idle(looks=8):
    """视频在播、人没动：哪些小块会被认成"一直在变"，以及排除后还算不算"画面变了"。"""
    app = App(video=True)
    tile = 32
    frames = []
    for _ in range(looks):
        app.animate()
        frames.append(app.look()[:, :, 1].astype(np.int16))
    # 截图包含标题栏，换算出右侧画布在截图里的起点
    cap_left, cap_top = winapi.capture_window(app.hwnd).rect[:2]
    ox, oy = app.right.winfo_rootx() - cap_left, app.right.winfo_rooty() - cap_top
    app.close()
    h, w = frames[0].shape
    th, tw = h // tile, w // tile

    def tiles(a):
        return a[: th * tile, : tw * tile].reshape(th, tile, tw, tile).mean(axis=(1, 3))

    changed = np.zeros((th, tw), int)
    for a, b in zip(frames, frames[1:]):
        changed += (np.abs(tiles(a) - tiles(b)) > 2).astype(int)
    volatile = changed >= 3  # 连续几眼里至少变了 3 次
    # 真实视频区域（窗口坐标）：左栏 420 宽，视频在右边 (40,20)-(1040,580)
    vx0, vy0, vx1, vy1 = ox + 40, oy + 20, ox + 1040, oy + 20 + 560
    truth = np.zeros((th, tw), bool)
    for ty in range(th):
        for tx in range(tw):
            # 小块和视频框有任何重叠就算"在视频里"（压在视频边上的小块确实会变）
            x0, y0 = tx * tile, ty * tile
            truth[ty, tx] = x0 < vx1 and x0 + tile > vx0 and y0 < vy1 and y0 + tile > vy0
    last_change = np.abs(tiles(frames[-2]) - tiles(frames[-1])) > 2
    return {
        "小块总数": int(th * tw),
        "视频里的小块": int(truth.sum()),
        "认成一直在变的": int(volatile.sum()),
        "其中确实在视频里": int((volatile & truth).sum()),
        "视频外被误认": int((volatile & ~truth).sum()),
        "最后一眼：变了的小块占比（不排除）": f"{last_change.mean():.0%}",
        "最后一眼：排除一直在变的之后": f"{(last_change & ~volatile).mean():.1%}",
    }


def t_fling():
    """两眼之间滚多远还能接上（窗口高约 1000 像素）。"""
    app = App(video=False, left_w=10)
    out = []
    for step in [100, 300, 500, 700, 800, 850, 900, 940, 970, 1000, 1100]:
        a = strip_rows(app.look())
        app.scroll("right", step)
        b = strip_rows(app.look())
        per, _ = regions(a, b)
        ok = [p[0] for p in per[1:] if p[0] is not None]
        voted = max(set(ok), key=ok.count) if ok else None
        out.append({"一步滚动像素": step, "估出": voted, "对": voted == step})
    h = app.right.winfo_height()
    app.close()
    return {"内容区高度": h, "结果": out}


def t_cost():
    """切竖带之后，算指纹和偏移的耗时。"""
    app = App(video=True)
    imgs = []
    t_cap = []
    for _ in range(10):
        app.animate()
        app.scroll("right", 90)
        t0 = time.perf_counter()
        imgs.append(app.look())
        t_cap.append(time.perf_counter() - t0)
    app.close()
    t0 = time.perf_counter()
    rows = [strip_rows(i) for i in imgs]
    t1 = time.perf_counter()
    for a, b in zip(rows, rows[1:]):
        regions(a, b)
    t2 = time.perf_counter()
    return {"窗口": f"{imgs[0].shape[1]}x{imgs[0].shape[0]}",
            "截图+刷新 ms": round(float(np.median(t_cap)) * 1000, 1),
            "12 条竖带指纹 ms": round((t1 - t0) / len(imgs) * 1000, 2),
            "估偏移 ms": round((t2 - t1) / (len(imgs) - 1) * 1000, 2)}


if __name__ == "__main__":
    res = {
        "两块各滚各的": t_two_panes(),
        "视频在播时滚动": t_video_scroll(),
        "视频在播、人没动": t_video_idle(),
        "一步滚多远还能接上": t_fling(),
        "耗时": t_cost(),
    }
    text = json.dumps(res, ensure_ascii=False, indent=1)
    print(text)
    Path(__file__).with_name("align_results.json").write_text(text, encoding="utf-8")
