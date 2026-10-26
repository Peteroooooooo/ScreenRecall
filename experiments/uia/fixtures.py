"""合成测试内容：全部是本实验生成的假数据，不含任何真实账号或用户内容。"""

from __future__ import annotations

from pathlib import Path

PASSWORD_DUMMY = "Dummy-Pa55word!"

CHAT = [
    ("李雷", "09:41", "明天下午三点在 3 号会议室讨论 Q3 预算，记得带上 PPT。"),
    ("Alice Zhang", "09:43", "Sure, I'll bring the draft. Room 3 at 15:00 works for me."),
    ("韩梅梅", "09:45", "我把发票号 INV-20261026-0731 发到群里了，金额 ¥12,480.00。"),
    ("Bob Smith", "09:47", "Reminder: the staging password rotates on Friday (not posting it here)."),
    ("李雷", "09:52", "收到。另外 rn 和 m、0 和 O、1 和 l 在截图里容易认错：rnm 0O0 1l1。"),
    ("Alice Zhang", "09:55", "Tracking link: https://example.com/track?id=A7F3-99Q2"),
]

ORDERS = [
    ("ORD-2026-0001", "机械键盘 K87", "2", "¥ 698.00"),
    ("ORD-2026-0002", "USB-C Hub 7in1", "1", "¥ 239.50"),
    ("ORD-2026-0003", "27英寸显示器", "1", "¥ 1,899.00"),
    ("ORD-2026-0004", "Ergonomic Mouse", "3", "¥ 447.00"),
    ("ORD-2026-0005", "降噪耳机 NC-700", "1", "¥ 1,299.00"),
]

CODE = '''def fibonacci(n: int) -> int:
    """Return the n-th Fibonacci number (CODE-BLOCK-MARKER)."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a

print(fibonacci(42))  # 267914296'''


def article_paragraphs(n: int = 40) -> list[str]:
    out = []
    for i in range(1, n + 1):
        out.append(
            f"ARTICLE-P{i:02d} 第{i}段：屏幕回忆需要在本地记录用户选定窗口的文字，"
            f"并支持之后按关键词检索。This is paragraph {i} of the long article; "
            f"it exists to test scrolling and off-screen text exposure."
        )
    return out


def html_page() -> str:
    msgs = "\n".join(
        f'<div class="msg" role="article" aria-label="消息 来自 {s}"><span class="sender">{s}</span>'
        f'<span class="time">{t}</span><p>{m}</p></div>'
        for s, t, m in CHAT
    )
    rows = "\n".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td><td>{d}</td></tr>" for a, b, c, d in ORDERS)
    paras = "\n".join(f"<p>{p}</p>" for p in article_paragraphs())
    code = CODE.replace("&", "&amp;").replace("<", "&lt;")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>SR-UIA 测试页 Probe</title>
<style>
body{{font-family:"Segoe UI","Microsoft YaHei",sans-serif;font-size:16px;margin:16px 24px;max-width:1050px;color:#111;background:#fff}}
h1{{font-size:24px;margin:4px 0}} h2{{font-size:19px;margin:14px 0 6px}}
.msg{{margin:5px 0;padding:5px 10px;border-radius:8px;background:#eef2ff}} .msg p{{margin:2px 0}}
.sender{{font-weight:600;margin-right:10px}} .time{{color:#555;font-size:13px}}
table{{border-collapse:collapse}} td,th{{border:1px solid #999;padding:3px 10px}}
pre{{background:#f4f4f4;padding:8px;font-size:14px}}
.sr-only{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}}
.row{{display:flex;gap:24px;align-items:center;flex-wrap:wrap}}
</style></head>
<body>
<h1>SR-UIA 探针页面 Probe Page</h1>
<div class="row">
<p id="tick">TICK 0</p>
<label>用户名 <input id="user" value="probe_user_01"></label>
<label>密码 <input id="pw" type="password" value="{PASSWORD_DUMMY}"></label>
</div>
<div class="row">
<canvas id="cv" width="520" height="40"></canvas>
<p aria-hidden="true">ARIAHIDDEN-VISIBLE 屏幕可见但对辅助技术隐藏</p>
</div>
<p class="sr-only">SRONLY-INVISIBLE 只给读屏器的隐藏文字</p>
<p style="display:none">DISPLAYNONE-SECRET 不显示的文字</p>
<h2>聊天记录 Chat</h2>
<section aria-label="聊天">
{msgs}
</section>
<h2>订单表 Orders</h2>
<table><tr><th>订单号</th><th>商品</th><th>数量</th><th>金额</th></tr>
{rows}
</table>
<h2>代码 Code</h2>
<pre><code>{code}</code></pre>
<h2>长文章 Article</h2>
{paras}
<script>
const c=document.getElementById('cv').getContext('2d');
c.font='22px "Segoe UI","Microsoft YaHei"';c.fillStyle='#000';c.fillText('CANVAS-ONLY 画布里的文字 42',4,28);
let n=0;setInterval(()=>{{document.getElementById('tick').textContent='TICK '+(++n);}},500);
</script>
</body></html>
"""


def html_page2() -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>SR-UIA 后台标签页</title></head>
<body><h1>HIDDENTAB-MARKER 后台标签页内容 7781</h1><p>这一页在后台标签里，屏幕上看不到。</p></body></html>
"""


def notepad_text(n: int = 300) -> str:
    return "\n".join(
        f"NOTE-L{i:03d} 记事本第{i}行 quick brown fox 0O1lI| rn/m 3.14159 user{i}@example.com"
        for i in range(1, n + 1)
    )


def code_file(n: int = 150) -> str:
    lines = ['"""SR-UIA 合成代码文件。"""', ""]
    for i in range(1, n + 1):
        lines.append(f"def func_{i:03d}(x: int) -> int:  # CODE-L{i:03d} 中文注释 {i}")
        lines.append(f"    return x * {i} + {i * 7 % 13}")
    return "\n".join(lines)


def console_command(n: int = 200) -> str:
    return (
        "$host.UI.RawUI.WindowTitle='SR-UIA-CONSOLE'; "
        f"1..{n} | ForEach-Object {{ 'CON-L{{0:D3}} 控制台输出 alpha beta {{1}}' -f $_, ($_*7) }}"
    )


def explorer_names(n: int = 40) -> list[str]:
    kinds = ["季度报告", "会议纪要", "invoice", "设计稿", "budget_draft"]
    exts = [".txt", ".md", ".csv", ".log"]
    return [f"EXP-F{i:02d}_{kinds[i % len(kinds)]}_{2026}{i:02d}{exts[i % len(exts)]}" for i in range(1, n + 1)]


def word_paragraphs(n: int = 60) -> list[str]:
    return [
        f"WORD-P{i:02d} 第{i}段 Word 文档测试内容：本段包含中文、English words 与数字 {i * 37}。"
        for i in range(1, n + 1)
    ]


def excel_rows(n: int = 60) -> list[list[str]]:
    rows = [["编号", "姓名", "部门", "金额", "备注"]]
    names = ["张三", "李四", "王五", "Alice", "Bob"]
    for i in range(1, n + 1):
        rows.append([f"XL-R{i:02d}", names[i % 5], f"部门{i % 4}", f"{i * 123.45:.2f}", f"note-{i}"])
    return rows


def write_all(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = {}
    paths["page"] = root / "page.html"
    paths["page"].write_text(html_page(), encoding="utf-8")
    paths["page2"] = root / "page2.html"
    paths["page2"].write_text(html_page2(), encoding="utf-8")
    paths["note"] = root / "sr-uia-notes.txt"
    paths["note"].write_text(notepad_text(), encoding="utf-8")
    code_dir = root / "vscode-proj"
    code_dir.mkdir(exist_ok=True)
    paths["code"] = code_dir / "sample.py"
    paths["code"].write_text(code_file(), encoding="utf-8")
    paths["code_dir"] = code_dir
    exp = root / "SR-UIA-Explorer"
    exp.mkdir(exist_ok=True)
    for name in explorer_names():
        (exp / name).write_text("synthetic\n", encoding="utf-8")
    paths["explorer"] = exp
    return paths
