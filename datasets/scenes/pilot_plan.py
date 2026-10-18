"""小批试采计划：cao-code-01 / cao-chat-01 / cao-web-01 / cao-table-01 各 10 帧。

全部是受控场景或公开网站，如实记录来源：
- code：真实运行 datasets/scenes/code01_train.py（真实 PyTorch 报错；OOM 那条是脚本打印的文本），
  把真实输出放进终端样式页面显示。
- chat：合成的 Slack 风格频道（测试工作区，人名和内容都是编的），不是真实聊天。
- web：公开旅游 / 户外网站，原样显示，不登录、不点任何按钮。
- table：合成的个人预算表，合计由脚本真实计算。
"""
from __future__ import annotations

import html
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = ROOT / ".venv" / "bin" / "python"
FULL, HALF = (1400, 860), (900, 860)
esc = html.escape

# ---------------------------------------------------------------- code
CODE_RUNS = [  # (参数, 主题, 窗口, 画面变化说明)
    ("--img 200 --epochs 2", "light", FULL, "首次运行成功，2 个 epoch"),
    ("--batch_size 32 --img 224", "light", FULL, "img 改成 224 → view 形状报错（batch 32）"),
    ("--batch_size 16 --img 224", "dark", FULL, "batch 改 16 后重跑，仍是形状报错；深色终端"),
    ("--batch_size 16 --lr 3e-4 --img 200 --epochs 3", "dark", FULL, "img 改回 200，batch 16 训练成功"),
    ("--batch_size 16 --img 200 --label_bs 8", "dark", HALF, "标签 batch 8 → cross_entropy batch 不一致报错；半屏"),
    ("--simulate-oom --batch_size 128", "light", HALF, "batch 128，脚本打印 CUDA OOM 文本；半屏"),
    ("--batch_size 64 --img 256", "light", FULL, "img 256 → 形状报错（batch 64）"),
    ("--hidden 1024 --lr 1e-3 --epochs 4", "dark", FULL, "hidden 1024、lr 1e-3、未给 --img（默认 224）→ 形状报错 [16, 3, 224, 224]"),
    ("--batch_size 8 --img 200 --label_bs 4 --lr 5e-5", "light", HALF, "batch 8 / 标签 4 报错，lr 5e-5；半屏"),
    ("--simulate-oom --batch_size 256 --img 200", "dark", FULL, "batch 256 打印 OOM 文本；深色全屏"),
]
FACT_CODE_1 = "RuntimeError: shape '[16, 3, 224, 224]' is invalid for input of size 1920000"
FACT_CODE_2 = "batch_size=16 lr=0.0003"


def _run(args: str) -> str:
    cmd = [str(PY), "datasets/scenes/code01_train.py", *shlex.split(args)]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
    out = r.stdout + r.stderr
    return out.replace(str(ROOT) + "/", "")


def _term(prompt_cmds: list[tuple[str, str]], theme: str) -> str:
    bg, fg, pc = ("#1e1e1e", "#d4d4d4", "#6a9955") if theme == "dark" else ("#ffffff", "#1f1f1f", "#0b6e2e")
    body = ""
    for cmd, out in prompt_cmds:
        body += f'<span style="color:{pc}">cao@cao-mbp ScreenRecall %</span> {esc(cmd)}\n{esc(out)}\n'
    return f"""<!doctype html><meta charset=utf-8><title>zsh — train.py — 终端样式受控页面</title>
<body style="margin:0;background:{bg};color:{fg}"><div style="padding:6px 12px;font:13px -apple-system;
background:{'#2d2d2d' if theme=='dark' else '#ececec'};color:{'#bbb' if theme=='dark' else '#444'}">
zsh — ScreenRecall — 120×36 &nbsp;·&nbsp; (terminal-style page; output from a real run of train.py)</div>
<pre style="margin:0;padding:14px 16px;font:15px/1.45 Menlo,monospace;white-space:pre-wrap">{body}<span style="color:{pc}">cao@cao-mbp ScreenRecall %</span> <span style="background:{fg}">&nbsp;</span></pre></body>"""


def code_frames() -> list[dict]:
    frames, history = [], []
    for args, theme, win, var in CODE_RUNS:
        out = _run(args)
        cmd = f".venv/bin/python train.py {args}"
        history = (history + [(cmd, out)])[-2:]          # 屏幕上留最近两次运行
        shown = "".join(o for _, o in history)
        facts = [f for f, s in (("cao-code-01#1", FACT_CODE_1), ("cao-code-01#2", FACT_CODE_2)) if s in shown]
        frames.append(dict(html=_term(history, theme), window=win, language="en", facts=facts,
                           source_kind="mock_ui",
                           variation=f"{var}；{'深色' if theme == 'dark' else '浅色'}终端"))
    return frames


# ---------------------------------------------------------------- chat
FACT_CHAT_1 = "API Reference v2.3"
FACT_CHAT_2 = "Fri 24 Oct 2026, 18:00 SGT"
CHANNEL = [
    ("Alex Tan", "09:12", "Morning all — kicking off the retrieval sprint today."),
    ("Priya Nair", "09:14", "Great. Which embedding model are we pinning for the demo?"),
    ("Alex Tan", "09:15", "paraphrase-multilingual-MiniLM-L12-v2, revision locked in config.toml"),
    ("Jordan Lee", "09:21", "Docs are here: API Reference v2.3 — https://docs.example.com/screenrecall/api-v2.3"),
    ("Priya Nair", "09:23", "Thanks! The /search endpoint now takes mode=text|image, right?"),
    ("Jordan Lee", "09:24", "Yes, and top_k defaults to 20. Response includes rrf_score per hit."),
    ("Mei Chen", "09:40", "Reminder: report draft deadline is Fri 24 Oct 2026, 18:00 SGT."),
    ("Alex Tan", "09:42", "Noted. I'll own sections 4.1–4.3 (capture cadence + OCR)."),
    ("Priya Nair", "09:47", "I'll take evaluation: Hit@5 and MRR@10 on the 20 dev queries."),
    ("Jordan Lee", "10:05", "CLIP model is clip-ViT-B-32, 512-dim. Image index rebuilt in 3.2 s on 12 frames."),
    ("Mei Chen", "10:11", "Can someone check the cold-start time? I saw ~70 s loading models."),
    ("Alex Tan", "10:13", "That's first load from disk; warm queries were ~20 ms."),
    ("Priya Nair", "10:30", "Pushed eval script to feat/dataset-collection, please review."),
    ("Jordan Lee", "10:34", "Reviewed — one nit on the split seed, otherwise LGTM."),
]
DMS = [
    ("Mei Chen", "10:50", "Hey, can you send me the API doc link again? Lost it in the thread."),
    ("You", "10:51", "Sure: API Reference v2.3 — https://docs.example.com/screenrecall/api-v2.3"),
    ("Mei Chen", "10:52", "Thanks! And deadline is still Fri 24 Oct 2026, 18:00 SGT?"),
    ("You", "10:53", "Yes, unchanged."),
]


def _slack(msgs, theme: str, title: str, header: str) -> str:
    dark = theme == "dark"
    bg, fg, side, sub = ("#1a1d21", "#d1d2d3", "#121016", "#9a9b9e") if dark else ("#ffffff", "#1d1c1d", "#3f0e40", "#616061")
    colors = {"Alex Tan": "#e8912d", "Priya Nair": "#2bac76", "Jordan Lee": "#1d9bd1", "Mei Chen": "#e01e5a", "You": "#7c3aed"}
    rows = "".join(
        f'<div style="display:flex;gap:10px;padding:8px 20px"><div style="width:36px;height:36px;border-radius:6px;'
        f'background:{colors.get(n, "#888")};color:#fff;font:700 15px -apple-system;display:flex;align-items:center;'
        f'justify-content:center">{esc(n[0])}</div><div><b>{esc(n)}</b> <span style="color:{sub};font-size:12px">{t}</span>'
        f'<div style="margin-top:2px">{esc(m)}</div></div></div>' for n, t, m in msgs)
    chans = "".join(f'<div style="padding:3px 16px;{"background:#1164a3;color:#fff" if c == header else ""}">{c}</div>'
                    for c in ["# general", "# ml-project", "# random", "Mei Chen", "Jordan Lee"])
    return f"""<!doctype html><meta charset=utf-8><title>{esc(title)}</title>
<body style="margin:0;font:15px/1.4 -apple-system;background:{bg};color:{fg};display:flex;height:100vh">
<div style="width:230px;background:{side};color:#cfc3cf;padding-top:12px"><div style="padding:0 16px 12px;color:#fff;font-weight:700">
SR-Test Workspace<div style="font:11px -apple-system;color:#bba">synthetic test workspace</div></div>{chans}</div>
<div style="flex:1;display:flex;flex-direction:column"><div style="padding:12px 20px;border-bottom:1px solid #8884;font-weight:700">{esc(header)}</div>
<div style="flex:1;overflow:auto;display:flex;flex-direction:column;justify-content:flex-end">{rows}</div>
<div style="margin:12px 20px;border:1px solid #8886;border-radius:8px;padding:10px;color:{sub}">Message {esc(header)}</div></div></body>"""


def chat_frames() -> list[dict]:
    frames = []
    plan = [(5, "light", FULL), (6, "light", FULL), (7, "light", FULL), (9, "light", HALF), (11, "light", FULL),
            (12, "dark", FULL), (14, "dark", FULL), ("dm2", "dark", FULL), ("dm4", "dark", HALF), (14, "light", HALF)]
    for k, theme, win in plan:
        if isinstance(k, int):
            msgs, header, var = CHANNEL[:k], "# ml-project", f"#ml-project 频道显示前 {k} 条消息"
        else:
            n = int(k[2:])
            msgs, header, var = DMS[:n], "Mei Chen", f"与 Mei Chen 私信，{n} 条消息"
        shown = " ".join(m for _, _, m in msgs[-12:])
        facts = [f for f, s in (("cao-chat-01#1", FACT_CHAT_1), ("cao-chat-01#2", FACT_CHAT_2)) if s in shown]
        frames.append(dict(html=_slack(msgs[-12:], theme, f"{header} - SR-Test Workspace (synthetic)", header),
                           window=win, language="en", facts=facts,
                           source_kind="mock_ui",
                           variation=f"{var}；{'深色' if theme == 'dark' else '浅色'}",
                           note="合成聊天：人名、链接、日期都是编的"))
    return frames


# ---------------------------------------------------------------- web（公开网站）
WEB = [
    ("https://www.jungfrau.ch/en-gb/", 0, FULL, 1.0, "Jungfrau 首页顶部"),
    ("https://www.jungfrau.ch/en-gb/", 900, FULL, 1.0, "Jungfrau 首页中部"),
    ("https://www.jungfrau.ch/en-gb/", 0, HALF, 1.0, "Jungfrau 首页顶部，半屏"),
    ("https://www.banfflakelouise.com/", 0, FULL, 1.0, "Banff & Lake Louise 首页顶部"),
    ("https://www.banfflakelouise.com/", 1000, FULL, 1.0, "Banff 首页中部"),
    ("https://www.banfflakelouise.com/", 0, FULL, 1.25, "Banff 首页顶部，放大"),
    ("https://www.zermatt.ch/en", 0, FULL, 1.0, "Zermatt 首页顶部"),
    ("https://www.zermatt.ch/en", 1100, HALF, 1.0, "Zermatt 首页中部，半屏"),
    ("https://www.myswitzerland.com/en/", 0, FULL, 1.0, "MySwitzerland 首页顶部"),
    ("https://www.myswitzerland.com/en/", 700, FULL, 0.8, "MySwitzerland 中部，缩小"),
]


def web_frames() -> list[dict]:
    return [dict(url=u, scroll=s, window=w, zoom=z, language="en", settle=5.0, variation=v, source_kind="real_web",
                 dismiss=["#onetrust-reject-all-handler", "Reject All", "Reject all", "Decline", "Only necessary", "Necessary only"],
                 note="公开网站原样显示，未登录、未点击；画面事实待人工看图后补")
            for u, s, w, z, v in WEB]


# ---------------------------------------------------------------- table
BASE = {"Jan": [("Rent 房租", 1850.00), ("Groceries 买菜", 420.00), ("Transport 交通", 128.50), ("Phone 话费", 38.00),
                ("Utilities 水电", 96.40), ("Dining 外食", 310.00), ("Books 书", 64.90), ("Gym 健身", 89.00)]}
BASE["Feb"] = [(c, round(v * f, 2)) for (c, v), f in zip(BASE["Jan"], [1, 0.93, 1.1, 1, 1.21, 0.8, 0, 1])]
BASE["Mar"] = [(c, round(v * f, 2)) for (c, v), f in zip(BASE["Jan"], [1, 1.05, 0.96, 1, 0.88, 1.32, 2.0, 1])]
BUDGET = [1850, 450, 140, 40, 110, 280, 50, 89]
FACT_TABLE_1 = "Groceries 买菜 465.00"


def _sheet(sheet: str, rows, sel: str, theme_zoom: float, edited: str = "") -> tuple[str, str]:
    total = round(sum(v for _, v in rows), 2)
    btot = sum(BUDGET)
    tr = ""
    for i, ((c, v), b) in enumerate(zip(rows, BUDGET), start=2):
        hl = "background:#fff3bf;" if c.startswith(edited) and edited else ""
        diff = v - b
        tr += (f'<tr><th>{i}</th><td>{esc(c)}</td><td style="text-align:right;{hl}">{v:,.2f}</td>'
               f'<td style="text-align:right">{b:,.2f}</td><td style="text-align:right;color:{"#c92a2a" if diff > 0 else "#2b8a3e"}">'
               f'{diff:+,.2f}</td></tr>')
    n = len(rows) + 2
    selcss = "outline:2px solid #1a73e8;" if sel == "total" else ""
    tr += (f'<tr style="font-weight:700"><th>{n}</th><td>Total 合计</td><td style="text-align:right;{selcss}">{total:,.2f}</td>'
           f'<td style="text-align:right">{btot:,.2f}</td><td style="text-align:right">{total - btot:+,.2f}</td></tr>')
    formula = f"=SUM(B2:B{n - 1})" if sel == "total" else f"{rows[1][1]:.2f}"
    tabs = "".join(f'<span style="padding:6px 16px;{"background:#fff;border-top:2px solid #1a73e8" if s == sheet else ""}">{s} 2026</span>'
                   for s in ["Jan", "Feb", "Mar"])
    page = f"""<!doctype html><meta charset=utf-8><title>Budget 2026 预算表 — {sheet}（合成数据）</title>
<style>table{{border-collapse:collapse;font:15px -apple-system}}td,th{{border:1px solid #d0d0d0;padding:6px 12px;min-width:90px}}
th{{background:#f1f3f4;color:#555;font-weight:400;min-width:30px}}</style>
<body style="margin:0;background:#fff;color:#202124;font:14px -apple-system;zoom:{theme_zoom}">
<div style="padding:10px 16px;font-size:18px">Budget 2026 预算表 <span style="font-size:12px;color:#888">synthetic data · personal budget template</span></div>
<div style="padding:6px 16px;border-top:1px solid #ddd;border-bottom:1px solid #ddd;font:14px Menlo"><b>fx</b>&nbsp; {formula}</div>
<div style="padding:16px"><table><tr><th></th><th>A</th><th>B</th><th>C</th><th>D</th></tr>
<tr><th>1</th><td><b>Category 类别</b></td><td><b>Spent 实际</b></td><td><b>Budget 预算</b></td><td><b>Diff 差额</b></td></tr>
{tr}</table></div><div style="position:fixed;bottom:0;left:0;right:0;background:#f1f3f4;padding:4px 16px">{tabs}</div></body>"""
    return page, f"{total:,.2f}"


def table_frames() -> list[dict]:
    jan = list(BASE["Jan"])
    jan2 = [(c, 465.00 if c.startswith("Groceries") else v) for c, v in jan]
    jan3 = [(c, 1920.00 if c.startswith("Rent") else v) for c, v in jan2]
    feb2 = [(c, 52.00 if c.startswith("Books") else v) for c, v in BASE["Feb"]]
    plan = [
        ("Jan", jan, "cell", 1.0, FULL, "", "1 月初始数据"),
        ("Jan", jan2, "cell", 1.0, FULL, "Groceries", "Groceries 420.00 → 465.00，合计重算"),
        ("Jan", jan2, "total", 1.0, FULL, "", "选中合计单元格，公式栏显示 =SUM"),
        ("Jan", jan3, "cell", 1.0, FULL, "Rent", "Rent 1,850 → 1,920"),
        ("Feb", BASE["Feb"], "cell", 1.0, FULL, "", "切到 2 月工作表"),
        ("Feb", feb2, "total", 1.0, HALF, "Books", "2 月 Books 0 → 52.00，半屏"),
        ("Mar", BASE["Mar"], "cell", 1.0, FULL, "", "切到 3 月工作表"),
        ("Mar", BASE["Mar"], "total", 1.35, FULL, "", "3 月放大到 135%，选中合计"),
        ("Jan", jan3, "total", 0.85, FULL, "", "回到 1 月，缩小到 85%"),
        ("Feb", feb2, "cell", 1.2, HALF, "", "2 月放大 120%，半屏"),
    ]
    frames = []
    for sheet, rows, sel, z, win, edited, var in plan:
        page, total = _sheet(sheet, rows, sel, z, edited)
        facts = ["cao-table-01#1"] if sheet == "Jan" and any(c.startswith("Groceries") and v == 465.0 for c, v in rows) else []
        frames.append(dict(html=page, window=win, language="mixed", facts=facts,
                           source_kind="mock_ui",
                           variation=f"{var}；合计 {total}", note="合成数据；合计由脚本计算"))
    return frames


TASKS = {
    "cao-code-01": code_frames(),
    "cao-chat-01": chat_frames(),
    "cao-web-01": web_frames(),
    "cao-table-01": table_frames(),
}

FACTS = [  # 写进 data-dataset/raw/cao/facts.csv
    ("cao-code-01#1", "cao-code-01", "error", FACT_CODE_1, "en", "真实运行 train.py --batch_size 16 --img 224 的报错"),
    ("cao-code-01#2", "cao-code-01", "param", FACT_CODE_2, "en", "train.py 打印的配置行"),
    ("cao-chat-01#1", "cao-chat-01", "link", FACT_CHAT_1 + " — https://docs.example.com/screenrecall/api-v2.3", "en", "合成聊天里分享的文档链接"),
    ("cao-chat-01#2", "cao-chat-01", "date", "Report draft deadline " + FACT_CHAT_2, "en", "合成聊天里的 deadline"),
    ("cao-table-01#1", "cao-table-01", "number", "1 月 Groceries 买菜 465.00（改金额后）", "mixed", "合成预算表"),
]
