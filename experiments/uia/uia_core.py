"""UIA 文字提取 + 与 OCR 对照的公共函数（实验用，不进入正式代码）。

只读取调用方传入的 hwnd（由本实验自己启动的窗口），不做桌面级遍历。
坐标：进程先切到 Per-Monitor V2，UIA 矩形与 PrintWindow 截图都是屏幕物理像素。
"""

from __future__ import annotations

import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from screenrecall import winapi  # noqa: E402

DPI_MODE = winapi.enable_dpi_awareness()  # 必须早于任何 UIA / 窗口调用

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402
from PIL import Image  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

UIA = comtypes.client.GetModule("UIAutomationCore.dll")
try:
    AUTO = comtypes.client.CreateObject(UIA.CUIAutomation8, interface=UIA.IUIAutomation2)
    # 对卡死的提供者设上限，避免一次提取拖住采集循环
    # 实验里放宽（Word 一次整树缓存要 ~3 s）；正式集成应更紧并放在独立线程
    AUTO.ConnectionTimeout = 10000
    AUTO.TransactionTimeout = 20000
except Exception:  # pragma: no cover
    AUTO = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)

P_RECT, P_CTYPE, P_NAME, P_AID, P_CLASS = 30001, 30003, 30005, 30011, 30012
P_PASSWORD, P_OFFSCREEN, P_FRAMEWORK = 30019, 30022, 30024
P_HAS_TEXT, P_HAS_VALUE, P_VALUE = 30040, 30043, 30045
TEXT_PATTERN = 10014

CTYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}


def _cache_request():
    cr = AUTO.CreateCacheRequest()
    for pid in (P_RECT, P_CTYPE, P_NAME, P_AID, P_CLASS, P_PASSWORD, P_OFFSCREEN, P_FRAMEWORK,
                P_HAS_TEXT, P_HAS_VALUE, P_VALUE):
        cr.AddProperty(pid)
    cr.TreeScope = 7  # TreeScope_Subtree：一次跨进程调用取回整棵子树
    cr.TreeFilter = AUTO.ControlViewCondition
    return cr


_CR = _cache_request()


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _walk(el, depth: int, parent: int, out: list[dict]) -> None:
    r = el.CachedBoundingRectangle
    rec = {
        "parent": parent, "depth": depth,
        "ct": CTYPES.get(el.CachedControlType, str(el.CachedControlType)),
        "name": _str(el.CachedName),
        "value": "",
        "rect": [r.left, r.top, r.right, r.bottom],
        "off": bool(el.CachedIsOffscreen),
        "pwd": bool(el.CachedIsPassword),
        "cls": _str(el.CachedClassName),
        "aid": _str(el.CachedAutomationId),
        "fw": _str(el.CachedFrameworkId),
        "has_tp": bool(el.GetCachedPropertyValue(P_HAS_TEXT)),
    }
    if el.GetCachedPropertyValue(P_HAS_VALUE):
        rec["value"] = _str(el.GetCachedPropertyValue(P_VALUE))
    idx = len(out)
    out.append(rec)
    rec["_el"] = el
    kids = el.GetCachedChildren()
    if kids:
        for i in range(kids.Length):
            _walk(kids.GetElement(i), depth + 1, idx, out)


def _rects(flat) -> list[list[float]]:
    flat = list(flat or [])
    return [[flat[i], flat[i + 1], flat[i] + flat[i + 2], flat[i + 1] + flat[i + 3]] for i in range(0, len(flat) - 3, 4)]


def _tp_lines(vis, cap: int = 400) -> list[dict]:
    """把可见范围按"行"单位逐行切开，取每行文字与矩形（高亮需要行级坐标）。"""
    cur = vis.Clone()
    cur.MoveEndpointByRange(1, cur, 0)  # End -> Start，折叠到可见范围起点
    out = []
    for _ in range(cap):
        line = cur.Clone()
        line.ExpandToEnclosingUnit(3)  # TextUnit_Line
        if line.CompareEndpoints(0, vis, 1) >= 0:  # 行首已越过可见范围末尾
            break
        out.append({"text": line.GetText(-1), "rects": _rects(line.GetBoundingRectangles())})
        prev = cur.Clone()
        if cur.Move(3, 1) == 0 or cur.CompareEndpoints(0, prev, 0) <= 0:
            break
    return out


def extract(hwnd: int, *, lines: bool = True, doc_text: bool = True) -> dict:
    """一次完整提取：缓存整棵控件树 + 顶层 TextPattern 的可见文字（可选逐行矩形、全文长度）。"""
    t0 = time.perf_counter()
    root = AUTO.ElementFromHandle(hwnd)
    cached = root.BuildUpdatedCache(_CR)
    t1 = time.perf_counter()
    recs: list[dict] = []
    _walk(cached, 0, -1, recs)
    t2 = time.perf_counter()

    # 只取"最外层"的 TextPattern 元素，避免嵌套重复
    tp_idx = []
    for i, r in enumerate(recs):
        if not r["has_tp"]:
            continue
        p, nested = r["parent"], False
        while p >= 0:
            if recs[p]["has_tp"]:
                nested = True
                break
            p = recs[p]["parent"]
        if not nested:
            tp_idx.append(i)
    tps = []
    t_lines = 0.0
    t_doc = 0.0
    for i in tp_idx[:8]:
        r = recs[i]
        item = {"elem": i, "ct": r["ct"], "name": r["name"][:80], "cls": r["cls"], "visible_text": "",
                "lines": [], "doc_len": None, "doc_text": None, "error": None}
        try:
            pat = r["_el"].GetCurrentPattern(TEXT_PATTERN).QueryInterface(UIA.IUIAutomationTextPattern)
            vr = pat.GetVisibleRanges()
            texts = []
            for k in range(vr.Length if vr else 0):
                rng = vr.GetElement(k)
                texts.append(rng.GetText(-1))
                if lines:
                    ta = time.perf_counter()
                    item["lines"].extend(_tp_lines(rng))
                    t_lines += time.perf_counter() - ta
            item["visible_text"] = "\n".join(texts)
            if doc_text:
                ta = time.perf_counter()
                d = pat.DocumentRange.GetText(2_000_000)
                t_doc += time.perf_counter() - ta
                item["doc_len"] = len(d)
                item["doc_text"] = d
        except Exception as exc:  # 记录而不中断：不同程序的 TextPattern 实现差异很大
            item["error"] = f"{type(exc).__name__}: {exc}"[:200]
        tps.append(item)
    t3 = time.perf_counter()
    for r in recs:
        r.pop("_el", None)
    return {
        "recs": recs, "tps": tps,
        "ms": {
            "build_cache": (t1 - t0) * 1000, "walk": (t2 - t1) * 1000,
            "textpattern_total": (t3 - t2) * 1000, "tp_lines": t_lines * 1000, "tp_doc": t_doc * 1000,
            "total": (t3 - t0) * 1000,
        },
    }


# ---------- 文字整理 ----------

def norm(s: str) -> str:
    """NFKC + casefold + 去掉全部空白（OCR 对中文空格的增删不算错）。"""
    s = unicodedata.normalize("NFKC", s).casefold()
    # 去掉格式控制字符：资源管理器的日期值里夹着 U+200E/U+200F（方向标记），不去掉就和 OCR 对不上
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", "", s)


def _intersects(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def visible_items(ext: dict, win_rect, password_dummy: str | None = None) -> dict:
    """控件树里"屏幕上可见"的文字项：未标 IsOffscreen、矩形与窗口相交、非密码框值。"""
    items_vis, items_all, pwd_fields = [], [], []
    for r in ext["recs"]:
        texts = []
        if r["name"].strip():
            texts.append(r["name"])
        if r["pwd"]:
            # 密码框：只记录"有没有把明文交出来"，值一律不收
            pwd_fields.append({"name": r["name"], "ct": r["ct"], "value_len": len(r["value"]),
                               "value_is_plaintext": bool(password_dummy and password_dummy in r["value"])})
        elif r["value"].strip() and r["value"] != r["name"]:
            texts.append(r["value"])
        rect = r["rect"]
        vis = (not r["off"]) and rect[2] > rect[0] and rect[3] > rect[1] and _intersects(rect, win_rect)
        for k, t in enumerate(texts):
            # 带 TextPattern 的元素（文档、编辑框）其 Value 往往是整篇全文（含滚出屏幕的部分），
            # "可见"集合里不收，改用 TextPattern 的可见范围；"全部"集合照收，用来统计屏外泄露
            # （单行短值如地址栏、Excel 单元格照常算可见）
            is_tp_value = (r["has_tp"] and t == r["value"] and t != r["name"]
                           and ("\n" in t or "\r" in t or len(t) > 500))
            item = {"text": t, "ct": r["ct"], "rect": rect, "tp_value": is_tp_value}
            items_all.append(item)
            if vis and not is_tp_value:
                items_vis.append(item)
    return {"vis": items_vis, "all": items_all, "pwd": pwd_fields}


# 正文类角色：名字通常就是屏幕上画出来的字。按钮/菜单/窗口等的 Name 多是图标的提示文字，屏幕上未必有
CONTENT_CT = {"Text", "Document", "Edit", "Hyperlink", "ListItem", "DataItem", "TreeItem", "TabItem",
              "HeaderItem", "Header", "ComboBox", "CheckBox", "RadioButton", "Table", "Custom"}


def tp_visible_lines(ext: dict) -> list[dict]:
    out = []
    for tp in ext["tps"]:
        if tp["lines"]:
            for ln in tp["lines"]:
                if ln["text"].strip():
                    out.append({"text": ln["text"], "rects": ln["rects"], "src": tp["ct"]})
        elif tp["visible_text"]:
            for t in tp["visible_text"].splitlines():
                if t.strip():
                    out.append({"text": t, "rects": [], "src": tp["ct"]})
    return out


def coverage(src: list[str], target: list[str], *, threshold: float = 85.0) -> dict:
    """src 中每行在 target 里能否找到（整行子串，或 partial_ratio ≥ 阈值）。返回行/字符加权覆盖率与明细。"""
    tgt = "".join(norm(t) for t in target)
    rows = []
    for s in src:
        n = norm(s)
        if len(n) < 2:
            continue
        if not tgt:
            score, aligned = 0.0, ""
        elif n in tgt:
            score, aligned = 100.0, n
        else:
            al = fuzz.partial_ratio_alignment(n, tgt) if len(n) <= len(tgt) else None
            if al is None:
                score, aligned = fuzz.ratio(n, tgt), tgt
            else:
                score, aligned = al.score, tgt[al.dest_start:al.dest_end]
        rows.append({"src": s, "n": n, "score": score, "aligned": aligned})
    tot = sum(len(r["n"]) for r in rows) or 1
    found = [r for r in rows if r["score"] >= threshold]
    return {
        "lines": len(rows),
        "line_cov": round(len(found) / len(rows), 3) if rows else None,
        "char_cov": round(sum(len(r["n"]) for r in found) / tot, 3),
        "exact_frac_of_found": round(sum(1 for r in found if r["score"] == 100.0) / len(found), 3) if found else None,
        "rows": rows,
    }


def ocr_boxes_to_lines(boxes) -> list[dict]:
    out = []
    for b in boxes:
        xs = [p[0] for p in b.quad]
        ys = [p[1] for p in b.quad]
        out.append({"text": b.text, "conf": b.confidence, "bbox": [min(xs), min(ys), max(xs), max(ys)]})
    return out


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def rect_alignment(ocr_lines: list[dict], uia_items: list[dict], origin, *, key: str = "rect") -> dict:
    """文字完全相同且各自唯一的 OCR 行与 UIA 项，比较位置（UIA 矩形换算到截图坐标）。"""
    from collections import Counter

    oc = Counter(norm(o["text"]) for o in ocr_lines)
    uc = Counter(norm(u["text"]) for u in uia_items)
    ox, oy = origin
    pairs = []
    for o in ocr_lines:
        n = norm(o["text"])
        if len(n) < 4 or oc[n] != 1 or uc.get(n) != 1:
            continue
        u = next(u for u in uia_items if norm(u["text"]) == n)
        if key == "rects":
            rs = u.get("rects") or []
            if not rs:
                continue
            r = [min(x[0] for x in rs), min(x[1] for x in rs), max(x[2] for x in rs), max(x[3] for x in rs)]
        else:
            r = u["rect"]
        ur = [r[0] - ox, r[1] - oy, r[2] - ox, r[3] - oy]
        b = o["bbox"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        ucx, ucy = (ur[0] + ur[2]) / 2, (ur[1] + ur[3]) / 2
        pairs.append({
            "iou": _iou(b, ur), "inside": ur[0] - 4 <= cx <= ur[2] + 4 and ur[1] - 4 <= cy <= ur[3] + 4,
            "dcx": cx - ucx, "dcy": cy - ucy, "dleft": b[0] - ur[0], "dtop": b[1] - ur[1],
        })
    if not pairs:
        return {"pairs": 0}
    med = lambda k: round(statistics.median(p[k] for p in pairs), 1)  # noqa: E731
    return {
        "pairs": len(pairs),
        "center_inside_frac": round(sum(p["inside"] for p in pairs) / len(pairs), 3),
        "median_iou": round(statistics.median(p["iou"] for p in pairs), 3),
        "median_dcx": med("dcx"), "median_dcy": med("dcy"),
        "median_dleft": med("dleft"), "median_dtop": med("dtop"),
    }


# ---------- 截图 + OCR ----------

_OCR = None


def ocr_engine():
    global _OCR
    if _OCR is None:
        from screenrecall.config import OcrConfig
        from screenrecall.ocr import OcrEngine

        _OCR = OcrEngine(OcrConfig(intra_op_threads=2, inter_op_threads=1, max_side_len=2000,
                                   max_attempts=1, retry_backoff_seconds=0))
    return _OCR


def capture_and_ocr(hwnd: int, save_png: Path | None = None) -> dict:
    t0 = time.perf_counter()
    cap = winapi.capture_window(hwnd)
    t1 = time.perf_counter()
    img = Image.frombytes("RGB", (cap.width, cap.height), cap.bgr, "raw", "BGR")
    if save_png:
        img.save(save_png)
    boxes = ocr_engine().recognize(img)
    t2 = time.perf_counter()
    return {"cap": cap, "lines": ocr_boxes_to_lines(boxes), "capture_ms": (t1 - t0) * 1000,
            "ocr_ms": (t2 - t1) * 1000, "size": [cap.width, cap.height], "method": cap.method}


def ms_summary(runs: list[dict]) -> dict:
    """首次调用单列；其余取中位数。"""
    out = {"first": {k: round(v, 1) for k, v in runs[0]["ms"].items()}}
    rest = runs[1:] or runs
    out["median_rest"] = {k: round(statistics.median(r["ms"][k] for r in rest), 1) for k in runs[0]["ms"]}
    out["n"] = len(runs)
    return out
