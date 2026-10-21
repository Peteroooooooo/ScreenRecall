"""cao 第 4 批：table-06 流程图（diagrams.net 只读查看器）、table-08 arXiv 论文结果表（HTML 版）、
table-09 时刻表（公开网页）、code-03 Jupyter（本机 127.0.0.1 的真实 Jupyter，notebook 已由真实内核执行）。

capture_mac.py --plan 使用。流程图内容是虚构的数据处理流程，整份图编码在 URL 的 #R 片段里，
只在浏览器本地渲染，不上传、不保存到任何账号。
"""
from __future__ import annotations

import base64
import os
import urllib.parse
import zlib
from xml.sax.saxutils import quoteattr


def _a(v: str) -> str:
    """属性值转义（流程图文字里有 < 和 ≥）。"""
    return quoteattr(v)

FULL, HALF = (1400, 860), (900, 860)
NOTE_WEB = "公开网站原样显示，未登录、未点击；画面事实待人工看图后补"


# ---------------------------------------------------------------- table-06 流程图
def _flow_xml(title: str, nodes: list[str], branch: tuple[int, str, str] | None = None) -> str:
    """竖排带编号节点；branch=(在第几个节点后分叉, 判断文字, 否分支节点)。"""
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>',
             f'<mxCell id="t" value={_a(title)} style="text;fontSize=18;fontStyle=1;align=left;" vertex="1" parent="1">'
             '<mxGeometry x="40" y="10" width="520" height="30" as="geometry"/></mxCell>']
    y, prev = 60, None
    for i, n in enumerate(nodes, 1):
        nid = f"n{i}"
        cells.append(f'<mxCell id="{nid}" value={_a(f"{i}. {n}")} style="rounded=1;whiteSpace=wrap;fillColor=#dae8fc;'
                     f'strokeColor=#6c8ebf;fontSize=13;" vertex="1" parent="1">'
                     f'<mxGeometry x="160" y="{y}" width="240" height="50" as="geometry"/></mxCell>')
        if prev:
            cells.append(f'<mxCell id="e{i}" style="endArrow=block;html=1;" edge="1" parent="1" source="{prev}" '
                         f'target="{nid}"><mxGeometry relative="1" as="geometry"/></mxCell>')
        prev = nid
        y += 80
        if branch and branch[0] == i:
            cells.append(f'<mxCell id="d" value={_a(branch[1])} style="rhombus;whiteSpace=wrap;fillColor=#fff2cc;'
                         f'strokeColor=#d6b656;fontSize=12;" vertex="1" parent="1">'
                         f'<mxGeometry x="190" y="{y}" width="180" height="70" as="geometry"/></mxCell>')
            cells.append(f'<mxCell id="ed" style="endArrow=block;html=1;" edge="1" parent="1" source="{prev}" '
                         f'target="d"><mxGeometry relative="1" as="geometry"/></mxCell>')
            cells.append(f'<mxCell id="nb" value={_a(branch[2])} style="rounded=1;whiteSpace=wrap;fillColor=#f8cecc;'
                         f'strokeColor=#b85450;fontSize=13;" vertex="1" parent="1">'
                         f'<mxGeometry x="460" y="{y + 10}" width="200" height="50" as="geometry"/></mxCell>')
            cells.append('<mxCell id="eb" value="否" style="endArrow=block;html=1;" edge="1" parent="1" source="d" '
                         'target="nb"><mxGeometry relative="1" as="geometry"/></mxCell>')
            prev = "d"
            y += 100
    body = "".join(cells)
    return (f'<mxfile><diagram name={_a(title)}><mxGraphModel dx="800" dy="600" grid="0"><root>{body}'
            '</root></mxGraphModel></diagram></mxfile>')


def _viewer_url(xml: str, dark: bool) -> str:
    raw = urllib.parse.quote(xml, safe="")
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    data = base64.b64encode(comp.compress(raw.encode()) + comp.flush()).decode()
    q = "lightbox=1&nav=1&layers=0&edit=_blank" + ("&dark=1" if dark else "")
    return f"https://viewer.diagrams.net/?{q}#R{urllib.parse.quote(data, safe='')}"


FLOWS = [
    ("ScreenRecall 截图入库流程（虚构示例）",
     ["窗口等稳检测", "保存 WebP 原图", "登记 OCR 待办", "RapidOCR 识别", "切文本块", "MiniLM 文字向量"],
     (2, "变化 < 1%？", "记 unsaved_change")),
    ("订单退款审批流程（虚构）",
     ["提交退款申请 RF-2093", "客服初审", "仓库确认退货", "财务复核", "原路退款", "通知用户"],
     (3, "商品完好？", "转人工仲裁")),
    ("实验数据清洗流程（虚构）",
     ["读取 raw_logs_v3.csv", "去掉空行与重复", "时区统一为 SGT", "异常值 3σ 过滤", "按任务分组", "导出 clean_v3.parquet"],
     (4, "剩余 ≥ 95%？", "回查采集脚本")),
    ("论文复现训练流水线（虚构）",
     ["下载 CIFAR-10", "随机裁剪 + 翻转", "ResNet-18 训练 90 轮", "验证集早停", "测试集评估", "写 results_seed42.json"],
     (5, "acc ≥ 93.0%？", "调学习率 0.05")),
    ("宿舍报修处理流程（虚构）",
     ["填写报修单 HM-5521", "宿管分派", "维修工上门", "用户确认修好", "结单归档"],
     (3, "当天能修？", "预约次日 10:00")),
]


def _flow_frames():
    out = []
    for k, (title, nodes, br) in enumerate(FLOWS):
        xml = _flow_xml(title, nodes, br)
        for j, (dark, window, zoom, desc) in enumerate([
                (False, FULL, 1.0, "浅色画布，全窗口"), (True, FULL, 1.0, "深色画布，全窗口"),
                (False, HALF, 1.0, "浅色画布，半屏窗口"), (False, FULL, 1.25, "浅色画布，放大到 125%"),
                (True, (1120, 780), 0.8, "深色画布，缩小到 80%")]):
            d = dict(url=_viewer_url(xml, dark), scroll=0, window=window, language="zh", settle=4.0,
                     source_kind="real_web",
                     source=f"diagrams.net 只读查看器（viewer.diagrams.net）显示虚构流程图「{title}」，图数据只在 URL 片段里",
                     note="虚构流程图；diagrams.net 官方查看器在本机浏览器渲染；未登录、未保存",
                     variation=f"流程图 {k + 1}/5「{title}」：{desc}", facts=[])
            if zoom != 1.0:
                d["zoom"] = zoom
            out.append(d)
    return out


# ---------------------------------------------------------------- table-08 arXiv 结果表
PAPERS = [
    ("2302.13971", "LLaMA", ["S2.T1", "S3.T4", "S3.T5", "S3.T7", "S3.T8"]),
    ("2106.09685", "LoRA", ["S5.T2", "S5.T3", "S5.T4", "S7.T5", "S7.T6"]),
    ("2205.01068", "OPT", ["S2.T1", "S3.T2", "S4.T3", "S4.T4", "S4.T5"]),
    ("1910.10683", "T5", ["S3.T1", "S3.T2", "S3.T4", "S3.T6", "S3.T8"]),
    ("2112.10752", "Latent Diffusion", ["S4.T1", "S4.T2", "S4.T3", "S4.T5", "S4.T7"]),
]
_VAR = [(FULL, 1.0, "全窗口"), (FULL, 1.0, "全窗口"), (HALF, 1.0, "半屏窗口"), (FULL, 0.9, "缩小到 90%"),
        ((1120, 780), 1.0, "中等窗口")]


def _paper_frames():
    out = []
    for pid, short, tabs in PAPERS:
        for (window, zoom, desc), tab in zip(_VAR, tabs):
            d = dict(url=f"https://arxiv.org/html/{pid}#{tab}", scroll=0, window=window, language="en", settle=4.0,
                     source_kind="real_web", note=NOTE_WEB,
                     variation=f"arXiv HTML 版论文 {short}（{pid}）表 {tab}：{desc}")
            if zoom != 1.0:
                d["zoom"] = zoom
            out.append(d)
    return out


# ---------------------------------------------------------------- table-09 时刻表
def _five(urls, what):
    out = []
    for url, lang in urls:
        site = url.split("/")[2].removeprefix("www.")
        for scroll, window, zoom, desc in [(0, FULL, 1.0, "顶部，全窗口"), (700, FULL, 1.0, "滚动到中部"),
                                           (1400, FULL, 1.0, "滚动到下部"), (0, HALF, 1.0, "半屏窗口顶部"),
                                           (500, FULL, 0.8, "缩小到 80% 的中上部")]:
            d = dict(url=url, scroll=scroll, window=window, language=lang, settle=4.0, source_kind="real_web",
                     note=NOTE_WEB, variation=f"{what} {url.split(site, 1)[-1] or site}（{site}）：{desc}")
            if zoom != 1.0:
                d["zoom"] = zoom
            out.append(d)
    return out


TIMETABLES = [("https://www.flightaware.com/live/airport/WSSS", "en"),
              ("https://www.flightaware.com/live/airport/RJTT", "en"),
              ("https://www.flightaware.com/live/airport/LSZH", "en"),
              ("https://www.sbstransit.com.sg/first-and-last-train", "en"),
              ("https://www.ktmb.com.my/ETS.html", "en")]


# ---------------------------------------------------------------- code-03 Jupyter（本机）
JUPYTER = os.environ.get("CAO_JUPYTER_BASE", "http://127.0.0.1:18888")
JUPYTER_TOKEN = os.environ.get("CAO_JUPYTER_TOKEN", "")
NOTEBOOKS = ["orders_keyerror.ipynb", "sensor_valueerror.ipynb", "merge_mismatch.ipynb",
             "dtype_parse.ipynb", "groupby_report.ipynb"]


def _jupyter_frames():
    out = []
    for nb in NOTEBOOKS:
        url = f"{JUPYTER}/notebooks/{nb}" + (f"?token={JUPYTER_TOKEN}" if JUPYTER_TOKEN else "")
        for scroll, window, zoom, desc in [(0, FULL, 1.0, "顶部单元格，全窗口"), (600, FULL, 1.0, "滚动到报错单元格"),
                                           (1300, FULL, 1.0, "滚动到下方输出"), (0, HALF, 1.0, "半屏窗口"),
                                           (500, FULL, 1.25, "放大到 125%")]:
            d = dict(url=url, scroll=scroll, window=window, language="en", settle=4.0, source_kind="real_web",
                     source=f"本机 Jupyter Notebook（127.0.0.1）打开 {nb}；单元格由真实 Python 内核执行，报错为真实 traceback",
                     note="受控测试 notebook，数据虚构；Jupyter 只监听 127.0.0.1",
                     variation=f"notebook {nb}：{desc}")
            if zoom != 1.0:
                d["zoom"] = zoom
            out.append(d)
    return out


TASKS = {
    "cao-table-06": _flow_frames(),
    "cao-table-08": _paper_frames(),
    "cao-table-09": _five(TIMETABLES, "时刻表"),
    "cao-code-03": _jupyter_frames(),
}
FACTS: list[tuple] = []
