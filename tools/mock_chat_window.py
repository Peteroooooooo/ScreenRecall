"""编码对照 / 资源实测用的聊天模拟窗口：python tools/mock_chat_window.py [--minutes 10]

不是真实聊天：联系人、群名和消息全部是虚构的，窗口标题写明"模拟"。窗口自己动：每隔几秒来一条新消息并滚到底，
不时向上翻看历史再回到底部，约每 90 秒切换一个会话。画面里有中英文、数字、金额、时间、房间号、链接和代码片段，
用来检查有损编码会不会改掉小字和数字。（数据集里同类自制界面标 mock_ui、不进正式名单；这里只用于编码对照。）
"""
import argparse
import ctypes
import random
import time
import tkinter as tk
import tkinter.font as tkfont

TITLE = "ScreenRecall 聊天模拟窗口（编码对照用，非真实聊天）"

CONTACTS = [
    ("算法刷题互助群", "#4e7fd6"), ("PRS 课程项目组", "#d6744e"), ("周末出游小分队", "#4eae7c"),
    ("Lin Wei", "#9b6bd6"), ("房东-陈先生", "#c9a227"), ("ML Reading Group", "#d64e8c"),
]
PEOPLE = ["小周", "Marco D", "文静", "Cao (test)", "阿杰", "Mia", "老王", "Sophie"]
LINES = [
    "今天的题是 LeetCode 239 滑动窗口最大值，单调队列 O(n) 能过",
    "Has anyone finished the Table 5 ablation? I got 0.8731 macro-F1 on val",
    "组会改到周四 14:30，地点 COM1-02-12 讨论室",
    "周六去千岛湖，高铁 G7351 08:12 发车，二等座 ¥127.5",
    "我把 slides 放在 /shared/prs/week9_v3.pptx 了，大家看下第 7 页",
    "房租这个月是 4,850 元，水电另算，请在 25 号前转账",
    "def merge(a, b): return sorted(a + b)  # 先这样，后面再优化",
    "The deadline is Oct 31, 23:59 SGT — report + slides + 12-min video",
    "酒店订了两间：杭州西湖店 ¥368/晚，订单号 HZ20261025-0042",
    "刚跑完：Hit@5 = 22/30，MRR@10 = 0.61，比上周高一点",
    "git push 之前记得 pull --rebase，不然又冲突",
    "明早 9:00 腾讯会议 728-561-903，密码 2026",
    "Could you share the notebook? The link expired: https://example.org/nb/7f3a",
    "二叉树层序遍历用队列，深度优先用递归或者栈",
    "我这边温度 31°C，记得带伞，下午可能有雷阵雨",
    "批量导入 120 张截图，OCR 平均 3.7 秒一张",
    "pip install -r requirements.txt  # Python 3.12，别用 3.13",
    "好，那我订 COM1 的讨论室，2 小时，到时候见",
    "电费单：上月 236.4 kWh，金额 ¥142.80",
    "Quick sync at 16:00? I'll bring the confusion matrix (web→table_doc 18)",
]


def build_history(rng: random.Random, n: int) -> list[tuple[str, str, bool, str]]:
    out, h, m = [], rng.randint(8, 11), rng.randint(0, 59)
    for _ in range(n):
        m += rng.randint(1, 9)
        h, m = h + m // 60, m % 60
        mine = rng.random() < 0.3
        out.append(("我" if mine else rng.choice(PEOPLE), rng.choice(LINES), mine, f"{h % 24:02d}:{m:02d}"))
    return out


class ChatWindow:
    def __init__(self, root: tk.Tk, rng: random.Random, scale: float):
        self.root, self.rng, self.s = root, rng, scale
        root.title(TITLE)
        root.configure(bg="#ededed")
        W, H = int(1500 * scale), int(950 * scale)
        root.geometry(f"{W}x{H}+{int(60 * scale)}+{int(40 * scale)}")
        self.f_name = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self.f_text = tkfont.Font(family="Microsoft YaHei UI", size=11)
        self.f_small = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.side = tk.Canvas(root, width=int(300 * scale), bg="#2b2d31", highlightthickness=0)
        self.side.pack(side="left", fill="y")
        right = tk.Frame(root, bg="#f5f5f5")
        right.pack(side="left", fill="both", expand=True)
        self.header = tk.Label(right, anchor="w", bg="#f5f5f5", fg="#1a1a1a", font=self.f_name,
                               padx=int(20 * scale), pady=int(12 * scale))
        self.header.pack(fill="x")
        tk.Frame(right, height=1, bg="#dcdcdc").pack(fill="x")
        self.chat = tk.Canvas(right, bg="#f5f5f5", highlightthickness=0)
        self.chat.pack(fill="both", expand=True)
        tk.Frame(right, height=1, bg="#dcdcdc").pack(fill="x")
        inp = tk.Text(right, height=4, bd=0, bg="#ffffff", font=self.f_text, padx=int(16 * scale), pady=int(10 * scale))
        inp.insert("1.0", "输入消息…（模拟窗口，不会发送）")
        inp.configure(state="disabled", fg="#9a9a9a")
        inp.pack(fill="x")
        self.histories = {name: build_history(rng, rng.randint(28, 40)) for name, _ in CONTACTS}
        self.current = 0
        self.show(0)

    def draw_side(self) -> None:
        c, s = self.side, self.s
        c.delete("all")
        y = int(16 * s)
        for i, (name, color) in enumerate(CONTACTS):
            if i == self.current:
                c.create_rectangle(0, y - int(8 * s), int(300 * s), y + int(56 * s), fill="#3a3d43", width=0)
            c.create_oval(int(16 * s), y, int(64 * s), y + int(48 * s), fill=color, width=0)
            c.create_text(int(40 * s), y + int(24 * s), text=name[0], fill="white", font=self.f_name)
            last = self.histories[name][-1]
            c.create_text(int(78 * s), y + int(4 * s), text=name, anchor="nw", fill="#f0f0f0", font=self.f_name)
            c.create_text(int(285 * s), y + int(6 * s), text=last[3], anchor="ne", fill="#9a9ca3", font=self.f_small)
            c.create_text(int(78 * s), y + int(30 * s), text=(last[1][:15] + "…"), anchor="nw", fill="#9a9ca3",
                          font=self.f_small)
            y += int(72 * s)

    def draw_chat(self) -> None:
        c, s = self.chat, self.s
        c.delete("all")
        width = max(400, c.winfo_width() or int(1200 * s))
        y, maxw = int(20 * s), int(width * 0.55)
        for who, text, mine, hhmm in self.histories[CONTACTS[self.current][0]]:
            c.create_text(width // 2, y, text=hhmm, fill="#a0a0a0", font=self.f_small)
            y += int(22 * s)
            x_av = width - int(56 * s) if mine else int(16 * s)
            c.create_rectangle(x_av, y, x_av + int(40 * s), y + int(40 * s), fill="#95a5c6" if mine else "#c6a595", width=0)
            if not mine:
                c.create_text(int(68 * s), y, text=who, anchor="nw", fill="#8a8a8a", font=self.f_small)
            ty = y + (0 if mine else int(20 * s))
            tid = c.create_text(0, 0, text=text, anchor="nw", width=maxw, font=self.f_text, fill="#1a1a1a")
            x0, y0, x1, y1 = c.bbox(tid)
            bw, bh = x1 - x0 + int(24 * s), y1 - y0 + int(18 * s)
            bx = width - int(68 * s) - bw if mine else int(68 * s)
            c.create_rectangle(bx, ty, bx + bw, ty + bh, fill="#a9e98e" if mine else "#ffffff", outline="#e2e2e2")
            c.coords(tid, bx + int(12 * s), ty + int(9 * s))
            c.tag_raise(tid)
            y = ty + bh + int(18 * s)
        c.configure(scrollregion=(0, 0, width, y + int(20 * s)))

    def show(self, idx: int) -> None:
        self.current = idx
        name = CONTACTS[idx][0]
        self.header.configure(text=f"{name}（{len(self.histories[name])} 条消息 · 模拟）")
        self.draw_side()
        self.root.update_idletasks()
        self.draw_chat()
        self.chat.yview_moveto(1.0)

    def tick(self) -> None:
        r = self.rng.random()
        if r < 0.45:  # 新消息
            name = CONTACTS[self.current][0]
            last = self.histories[name][-1][3]
            h, m = map(int, last.split(":"))
            m += self.rng.randint(1, 4)
            mine = self.rng.random() < 0.3
            self.histories[name].append(("我" if mine else self.rng.choice(PEOPLE), self.rng.choice(LINES), mine,
                                         f"{(h + m // 60) % 24:02d}:{m % 60:02d}"))
            self.show(self.current)
        elif r < 0.75:  # 向上翻看历史
            self.chat.yview_scroll(-self.rng.randint(4, 9), "units")
        else:  # 回到底部
            self.chat.yview_moveto(1.0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--every", type=float, default=7, help="平均每隔几秒动一次")
    ap.add_argument("--switch", type=float, default=90, help="每隔几秒切换会话")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    try:  # 按显示器 DPI 感知，避免 150% 缩放下被系统拉伸发糊
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    scale = root.winfo_fpixels("1i") / 96
    root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72)
    rng = random.Random(a.seed)
    w = ChatWindow(root, rng, scale)
    end = time.monotonic() + a.minutes * 60
    last_switch = time.monotonic()

    def loop() -> None:
        nonlocal last_switch
        if time.monotonic() >= end:
            root.destroy()
            return
        if time.monotonic() - last_switch >= a.switch:
            w.show((w.current + 1) % len(CONTACTS))
            last_switch = time.monotonic()
        else:
            w.tick()
        root.after(int(1000 * a.every * rng.uniform(0.6, 1.4)), loop)

    root.after(3000, loop)
    root.mainloop()


if __name__ == "__main__":
    main()
