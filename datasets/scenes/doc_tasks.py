"""真实 Numbers / Excel / 预览窗口里展示的受控表格与单据（cao 正式采集，capture_real_batch.py docs 使用）。

每个文件内容都不同（不同月份 / 数据集 / 仓库 / 订单），数据由固定种子生成，全部虚构。文件名中性，
不含任务编号或场景名；任何一个文件名都不是另一个的子串（窗口按标题里的文件名识别）。

listing(task, work) 只算出文件名和说明（attach 用，不改文件）；build 才写文件（prepare 用）。
"""
from __future__ import annotations

import random
from pathlib import Path

OWNER = {"Numbers Creator Studio": "Numbers", "Microsoft Excel": "Microsoft Excel", "Preview": "Preview"}
APP = {"cao-table-01": "Numbers Creator Studio", "cao-table-02": "Microsoft Excel",
       "cao-table-07": "Microsoft Excel", "cao-table-04": "Preview", "cao-table-05": "Preview"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- 规格（不写文件）

def _budget_specs():
    cats = ["Rent 房租", "Groceries 买菜", "Transport 交通", "Phone 话费", "Utilities 水电", "Dining 外食",
            "Books 书", "Gym 健身", "Insurance 保险", "Gifts 礼物"]
    plan = [1850, 450, 140, 40, 110, 280, 50, 89, 120, 60]
    out = []
    for m in range(12):   # 12 个月份的家庭预算
        rng = random.Random(100 + m)
        spent = [round(p * rng.uniform(0.7, 1.3), 2) if i else 1850.0 for i, p in enumerate(plan)]
        out.append(dict(stem=f"household-2026-{m + 1:02d}m", sheet=f"{MONTHS[m]} 2026", cats=cats, plan=plan,
                        spent=spent, lang="mixed", var=f"{MONTHS[m]} 2026 家庭预算（10 个类别）"))
    trips = [("trip-japan-osaka", "Osaka 大阪 5 days", ["Flight 机票", "Hotel 酒店", "JR Pass", "Food 餐饮", "Shopping 购物",
                                                     "Museum 门票"], [620, 540, 280, 300, 400, 60]),
             ("trip-swiss-alps", "Swiss Alps 7 days", ["Flight 机票", "Hotel 酒店", "Swiss Pass", "Food 餐饮", "Ski 滑雪",
                                                     "Souvenir 纪念品"], [980, 1260, 390, 520, 450, 80]),
             ("trip-bali-retreat", "Bali 4 days", ["Flight 机票", "Villa 别墅", "Driver 包车", "Food 餐饮", "Spa"],
              [310, 420, 120, 180, 95])]
    for k, (stem, sheet, cats_t, plan_t) in enumerate(trips):
        for v in range(2):
            rng = random.Random(500 + k * 10 + v)
            spent = [round(p * rng.uniform(0.8, 1.25), 2) for p in plan_t]
            out.append(dict(stem=f"{stem}-v{v + 1}x", sheet=sheet, cats=cats_t, plan=plan_t, spent=spent, lang="mixed",
                            var=f"旅行预算 {sheet} 第 {v + 1} 版"))
    for s, (who, cats_s, plan_s) in enumerate([
        ("student-sem1", ["Tuition 学费", "Hostel 宿舍", "Meals 饭卡", "Textbooks 教材", "MRT 地铁"], [4200, 1800, 450, 160, 60]),
        ("student-sem2", ["Tuition 学费", "Hostel 宿舍", "Meals 饭卡", "Laptop 电脑", "MRT 地铁"], [4200, 1800, 480, 1299, 64]),
        ("club-event-fund", ["Venue 场地", "Snacks 零食", "Prizes 奖品", "Printing 印刷", "Transport 交通"], [600, 180, 250, 75, 90]),
        ("wedding-gift-pool", ["Gift 礼物", "Card 贺卡", "Flowers 鲜花", "Delivery 配送"], [880, 12, 150, 25]),
        ("pet-care-yearly", ["Food 粮", "Vet 体检", "Grooming 美容", "Insurance 保险", "Toys 玩具"], [720, 380, 300, 260, 90]),
        ("home-office-setup", ["Desk 桌子", "Chair 椅子", "Monitor 显示器", "Lamp 台灯", "Cables 线材"], [389, 529, 459, 79, 45]),
        ("garden-renovation", ["Soil 土", "Plants 植物", "Tools 工具", "Labour 人工"], [120, 640, 210, 900]),
    ]):
        rng = random.Random(900 + s)
        spent = [round(p * rng.uniform(0.75, 1.2), 2) for p in plan_s]
        out.append(dict(stem=f"{who}-plan", sheet=who.replace("-", " ").title(), cats=cats_s, plan=plan_s,
                        spent=spent, lang="mixed", var=f"预算表 {who}"))
    return out


def _results_specs():
    datasets = ["CIFAR-10", "Flowers-102", "Food-101", "ImageNet-1k (subset)", "SST-2", "AG News", "MNLI",
                "SQuAD v1.1", "LibriSpeech test-clean", "ESC-50", "Oxford Pets", "DTD", "EuroSAT", "PCam",
                "GTSRB", "FER2013", "Caltech-101", "SVHN", "STL-10", "Country211", "IMDb", "Yelp-5", "TREC",
                "CoLA", "QNLI"]
    models = ["ResNet-50", "ViT-S/16", "ConvNeXt-T", "EfficientNet-B0", "MobileNetV3-L", "DeiT-T", "Swin-T",
              "RegNetY-4G", "BERT-base", "RoBERTa-base", "DistilBERT", "MiniLM-L12"]
    out = []
    for k, ds in enumerate(datasets):
        rng = random.Random(2000 + k)
        ms = rng.sample(models, 6)
        rows = []
        for m in ms:
            acc = round(rng.uniform(62, 97), 2)
            rows.append([m, acc, round(acc - rng.uniform(0.3, 4.0), 2), rng.choice([16, 32, 64, 128]),
                         round(rng.uniform(0.4, 11.0), 1), rng.choice([10, 20, 30, 50, 90])])
        sort_key = ["acc", "f1", "time"][k % 3]
        idx = {"acc": 1, "f1": 2, "time": 4}[sort_key]
        rows.sort(key=lambda r: -r[idx] if sort_key != "time" else r[idx])
        slug = ds.lower().replace(" ", "-").replace("(", "").replace(")", "").replace("/", "").replace(".", "")
        out.append(dict(stem=f"results-{slug}-r{k + 1:02d}", dataset=ds, rows=rows, sort=sort_key, lang="en",
                        var=f"{ds} 实验结果，按 {sort_key} 排序，条件格式色阶"))
    return out


def _inventory_specs():
    wh = ["North Depot", "East Hub", "Jurong West", "Tampines", "Penang Store", "Johor Bahru", "Online FC-1",
          "Online FC-2", "Changi Airside"]
    items = [("KB-87", "Mechanical keyboard 87-key"), ("MS-02", "Wireless mouse"), ("HD-55", "USB-C hub 5-in-1"),
             ("CB-2M", "USB-C cable 2 m"), ("MN-27", "27\" monitor"), ("ST-01", "Laptop stand"),
             ("WC-HD", "1080p webcam"), ("HS-BT", "Bluetooth headset"), ("SS-1T", "Portable SSD 1 TB"),
             ("PB-20", "Power bank 20000 mAh"), ("CH-65", "65 W charger"), ("DK-TB", "Thunderbolt dock")]
    out = []
    for k in range(25):
        rng = random.Random(3000 + k)
        w = wh[k % len(wh)]
        rows = []
        for sku, name in rng.sample(items, 9):
            stock = rng.randint(0, 240)
            rows.append([sku, name, stock, rng.choice([20, 30, 50]), round(rng.uniform(4.9, 389.0), 2)])
        week = 30 + k
        out.append(dict(stem=f"stock-{w.lower().replace(' ', '-')}-w{week}", warehouse=w, week=week, rows=rows,
                        lang="en", var=f"{w} 第 {week} 周库存 + 按品类汇总页"))
    return out


def _order_specs():
    shops = ["Lumen Electronics", "Kopi & Co. Supplies", "Greenleaf Grocer", "Northwind Books", "Atlas Outdoor",
             "Pixel Print Studio", "Harbour Home"]
    goods = [("Noise-cancelling headphones", 249.0), ("Arabica beans 1 kg", 32.5), ("Oat milk x6", 18.9),
             ("Deep Learning (hardcover)", 88.0), ("Trekking poles", 74.0), ("A3 poster print", 12.0),
             ("Linen bedsheet set", 119.0), ("Phone case", 19.9), ("Desk lamp", 46.0), ("Hiking socks x3", 21.0)]
    out = []
    for k in range(25):
        rng = random.Random(4000 + k)
        lines = [(n, rng.randint(1, 3), p) for n, p in rng.sample(goods, rng.randint(2, 4))]
        no = f"ORD-{rng.randint(100000, 999999)}"
        out.append(dict(stem=f"order-{no.lower()}", shop=shops[k % len(shops)], order_no=no, lines=lines,
                        date=f"2026-{rng.randint(8, 10):02d}-{rng.randint(1, 28):02d}", lang="en" if k % 3 else "mixed",
                        var=f"{shops[k % len(shops)]} 订单确认 {no}"))
    return out


def _bill_specs():
    kinds = [("water-electric", "水电费缴费通知 Utilities Bill", "SP Group (示例)"),
             ("tuition", "学费缴费通知 Tuition Fee Notice", "Example University Bursar"),
             ("hostel", "宿舍费缴费通知 Hostel Fee", "Residential College (示例)"),
             ("broadband", "宽带账单 Broadband Bill", "FibreNet (示例)"),
             ("mobile", "手机话费账单 Mobile Bill", "TeleOne (示例)")]
    out = []
    for k in range(25):
        rng = random.Random(5000 + k)
        slug, title, issuer = kinds[k % len(kinds)]
        acct = f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}"
        amt = round(rng.uniform(28, 4800 if slug == "tuition" else 260), 2)
        due = f"2026-{rng.randint(10, 12):02d}-{rng.randint(1, 28):02d}"
        out.append(dict(stem=f"notice-{slug}-{acct.replace('-', '')}", title=title, issuer=issuer, account=acct,
                        amount=amt, due=due, period=f"{MONTHS[(k + 6) % 12]} 2026", lang="mixed",
                        var=f"{title}，账号 {acct}，金额 {amt:.2f}，截止 {due}"))
    return out


SPECS = {"cao-table-01": _budget_specs, "cao-table-02": _results_specs, "cao-table-07": _inventory_specs,
         "cao-table-04": _order_specs, "cao-table-05": _bill_specs}
EXT = {"cao-table-01": ".xlsx", "cao-table-02": ".xlsx", "cao-table-07": ".xlsx", "cao-table-04": ".pdf",
       "cao-table-05": ".pdf"}


def _check_names(specs) -> None:
    stems = [s["stem"] for s in specs]
    for a in stems:
        for b in stems:
            if a != b and a in b:
                raise ValueError(f"文件名 {a} 是 {b} 的子串，窗口识别会混淆")


def listing(task: str, work: Path):
    specs = SPECS[task]()
    _check_names(specs)
    return APP[task], [(work / (s["stem"] + EXT[task]), {"lang": s["lang"], "var": s["var"], "facts": s.get("facts", "")})
                       for s in specs]


# ---------------------------------------------------------------- 写文件

def _xlsx_budget(path: Path, s: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = s["sheet"][:31]
    ws.append(["Category 类别", "Spent 实际", "Budget 预算", "Diff 差额"])
    for n, (c, v, b) in enumerate(zip(s["cats"], s["spent"], s["plan"]), 2):
        ws.append([c, v, b, f"=B{n}-C{n}"])
    last = len(s["cats"]) + 1
    ws.append(["Total 合计", f"=SUM(B2:B{last})", f"=SUM(C2:C{last})", f"=B{last + 1}-C{last + 1}"])
    for c in ws[1] + ws[last + 1]:
        c.font = Font(bold=True)
    for c in ws[1]:
        c.fill = PatternFill("solid", fgColor="DDEBF7")
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=4):
        for c in row:
            c.number_format = "#,##0.00"
    ws.column_dimensions["A"].width = 22
    wb.save(path)


def _xlsx_results(path: Path, s: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import ColorScaleRule
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "results"
    ws.append([f"Dataset: {s['dataset']}  (synthetic numbers for a course demo)"])
    ws.append(["Model", "Accuracy (%)", "Macro-F1 (%)", "Batch", "Train time (h)", "Epochs"])
    for r in s["rows"]:
        ws.append(r)
    n = len(s["rows"]) + 2
    ws.append(["Best", f"=MAX(B3:B{n})", f"=MAX(C3:C{n})", "", f"=MIN(E3:E{n})", ""])
    ws.conditional_formatting.add(f"B3:C{n}", ColorScaleRule(start_type="min", start_color="F8696B",
                                                              mid_type="percentile", mid_value=50, mid_color="FFEB84",
                                                              end_type="max", end_color="63BE7B"))
    ws.freeze_panes = "B3"
    for c in ws[2]:
        c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 20
    wb.save(path)


def _xlsx_inventory(path: Path, s: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append([f"{s['warehouse']} — week {s['week']} stock count (fictional)"])
    ws.append(["SKU", "Item", "On hand", "Reorder at", "Unit price", "Stock value", "Status"])
    for k, r in enumerate(s["rows"], 3):
        ws.append(r + [f"=C{k}*E{k}", f'=IF(C{k}<D{k},"REORDER","ok")'])
    n = len(s["rows"]) + 2
    ws.append(["", "Total", f"=SUM(C3:C{n})", "", "", f"=SUM(F3:F{n})", f'=COUNTIF(G3:G{n},"REORDER")&" to reorder"'])
    ws.conditional_formatting.add(f"C3:C{n}", CellIsRule(operator="lessThan", formula=["$D3"],
                                                          fill=PatternFill("solid", fgColor="FFC7CE")))
    for c in ws[2]:
        c.font = Font(bold=True)
    ws.column_dimensions["B"].width = 28
    sm = wb.create_sheet("Summary")
    sm.append(["Status", "SKUs", "Units"])
    sm.append(["REORDER", f'=COUNTIF(Stock!G3:G{n},"REORDER")', f'=SUMIF(Stock!G3:G{n},"REORDER",Stock!C3:C{n})'])
    sm.append(["ok", f'=COUNTIF(Stock!G3:G{n},"ok")', f'=SUMIF(Stock!G3:G{n},"ok",Stock!C3:C{n})'])
    wb.save(path)


def _order_html(s: dict) -> str:
    rows = "".join(f"<tr><td>{n}</td><td>{q}</td><td>{p:.2f}</td><td>{q * p:.2f}</td></tr>" for n, q, p in s["lines"])
    sub = sum(q * p for _, q, p in s["lines"])
    gst = round(sub * 0.09, 2)
    zh = "<p>感谢您的订购！以下为订单确认（示例单据，非真实交易）。</p>" if s["lang"] == "mixed" else ""
    return f"""<html><body style="font-family:Helvetica;margin:40px;color:#222">
<h1 style="color:#1f4e79">{s['shop']}</h1><h2>Order Confirmation</h2>{zh}
<p><b>Order No.</b> {s['order_no']} &nbsp; <b>Date</b> {s['date']} &nbsp; <b>Customer</b> Demo Customer</p>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">
<tr style="background:#ddebf7"><th>Item</th><th>Qty</th><th>Unit (SGD)</th><th>Amount (SGD)</th></tr>{rows}
<tr><td colspan="3" align="right">Subtotal</td><td>{sub:.2f}</td></tr>
<tr><td colspan="3" align="right">GST 9%</td><td>{gst:.2f}</td></tr>
<tr><td colspan="3" align="right"><b>Total</b></td><td><b>{sub + gst:.2f}</b></td></tr></table>
<p style="color:#888;margin-top:40px">Sample document generated for a course dataset. Not a real order.</p></body></html>"""


def _bill_html(s: dict) -> str:
    return f"""<html><body style="font-family:'PingFang SC',Helvetica;margin:40px;color:#222">
<h1 style="color:#7a1f1f">{s['title']}</h1><p>{s['issuer']} · 账单周期 Billing period: {s['period']}</p>
<table border="1" cellpadding="10" cellspacing="0" style="border-collapse:collapse;width:80%">
<tr><td>账号 Account No.</td><td>{s['account']}</td></tr>
<tr><td>应缴金额 Amount due</td><td><b>SGD {s['amount']:.2f}</b></td></tr>
<tr><td>缴费截止 Due date</td><td><b>{s['due']}</b></td></tr>
<tr><td>缴费方式 Payment</td><td>PayNow / GIRO（示例）</td></tr></table>
<p style="margin-top:30px">逾期将收取滞纳金 A late fee applies after the due date.</p>
<p style="color:#888">示例单据，仅用于课程数据集，账号与金额均为虚构。</p></body></html>"""


def _pdfs(items: list[tuple[Path, str]]) -> None:
    import os
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[2] / "data-dataset/.tools/pw"))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chromium")  # 只装了完整 Chromium，用它的新无头模式生成 PDF
        pg = b.new_page()
        for path, html in items:
            pg.set_content(html)
            pg.pdf(path=str(path), format="A4", print_background=True)
        b.close()


def build(task: str, work: Path):
    app, files = listing(task, work)
    specs = SPECS[task]()
    if task == "cao-table-01":
        for (p, _), s in zip(files, specs):
            _xlsx_budget(p, s)
    elif task == "cao-table-02":
        for (p, _), s in zip(files, specs):
            _xlsx_results(p, s)
    elif task == "cao-table-07":
        for (p, _), s in zip(files, specs):
            _xlsx_inventory(p, s)
    elif task == "cao-table-04":
        _pdfs([(p, _order_html(s)) for (p, _), s in zip(files, specs)])
    elif task == "cao-table-05":
        _pdfs([(p, _bill_html(s)) for (p, _), s in zip(files, specs)])
    return app, files


FACTS = [
    ("cao-table-01#2", "cao-table-01", "amount", "household-2026-03m：Mar 2026 预算表（具体金额由人看图确认）", "mixed",
     "草稿，需人工确认数值"),
]
