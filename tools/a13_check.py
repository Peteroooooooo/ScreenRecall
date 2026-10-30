"""A13 一键实测：断网可用 + 各缩放下截图边界与 OCR 高亮对齐。在 ScreenRecall 目录下运行：

  .\\.venv\\Scripts\\python.exe tools\\a13_check.py --out results\\windows\\<日期>-<机器名>\\a13 --wifi-off --scales 100,125,150

全程不用动电脑（实测约 2 分钟）。脚本会：
  1. 用全新的数据目录 data-a13\\<时间> 启动后台和页面接口；两个进程都套在 a13_netguard 里（进程内对外连接一律失败并记日志）。
  2. 断网检查（--wifi-off 时先真的断开 Wi-Fi）：录一个测试窗口 → 文字搜索、画面搜索、查看原图、导入截图再搜到它；
     结束后立刻重连 Wi-Fi。
  3. 每个缩放档：切换主显示器缩放 → 依次录三个窗口（DPI 感知的 Tk 窗口、不感知 DPI 的 Tk 窗口、Edge 里的测试页），
     核对：截图尺寸 = 窗口边框尺寸、右 / 下没有黑边、四角标记位置对、每行文字的 OCR 框和文字实际位置的重合度（IoU）；
     再用 Edge 打开 ScreenRecall 详情页，核对页面上画出来的高亮框和 OCR 坐标是否重合。
  4. 无论成功失败都恢复原来的缩放、重连 Wi-Fi、关掉自己开的进程；结果写到 --out（a13.json + README.md）。
     叠加了框的截图只放在数据目录里（不进 git）。

只改主显示器；中途想停按 Ctrl+C，同样会恢复。万一脚本被强行关掉：python tools\\a13_system.py restore data-a13\\<时间>\\state.json
（或者到 设置 → 系统 → 屏幕 → 缩放 手动改回，再点任务栏的 Wi-Fi 重连）。
"""
from __future__ import annotations

import argparse
import ctypes
import difflib
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import warnings
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import a13_system as system  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
warnings.filterwarnings("ignore", message=r"connect\(\) must be used as a context manager")
WORDS = {  # 每个阶段每种窗口一个不会撞车的词，用来搜到对应截图
    "offline": {"aware": "Lavender"},
    100: {"aware": "Marigold", "unaware": "Tamarind", "edge": "Juniper"},
    125: {"aware": "Begonia", "unaware": "Cardamom", "edge": "Sequoia"},
    150: {"aware": "Hibiscus", "unaware": "Saffron", "edge": "Magnolia"},
    175: {"aware": "Primrose", "unaware": "Nutmeg", "edge": "Cypress"},
    200: {"aware": "Camellia", "unaware": "Paprika", "edge": "Redwood"},
}
IMPORT_WORD = "Peppermint"
IOU_PASS = 0.5
EDGE_LINES = [  # (left, top, css 字体, 文字) —— 和 a13_window 的内容对应，单位是 CSS 像素
    (40, 34, "bold 35px 'Segoe UI'", "{word} {tag} edge"),
    (40, 112, "19px 'Segoe UI'", "The quick brown fox jumps over 4718 lazy dogs"),
    (40, 162, "16px Consolas", "def merge(a, b): return sorted(a + b)"),
    (40, 208, "19px 'Microsoft YaHei UI'", "缩放对齐测试：截图与高亮框应该重合"),
    (300, 300, "13px 'Segoe UI'", "Order HZ20261025-0042 at 14:30, total 127.50"),
    (430, 400, "24px 'Segoe UI'", "Bottom right 9931"),
]

_user32 = ctypes.WinDLL("user32")
_dwmapi = ctypes.WinDLL("dwmapi")


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------- 窗口几何（本进程是 PMv2，读到的都是物理像素） ----------------

def window_rect(hwnd: int) -> list[int]:
    r = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def frame_bounds(hwnd: int) -> list[int]:
    r = wintypes.RECT()
    if _dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)):  # EXTENDED_FRAME_BOUNDS
        return window_rect(hwnd)
    return [r.left, r.top, r.right, r.bottom]


def to_front(hwnd: int) -> None:
    _user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE
    _user32.SetForegroundWindow(wintypes.HWND(hwnd))


# ---------------- 页面接口 ----------------

class Api:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.c = httpx.Client(base_url=self.base, headers={"X-ScreenRecall": "1"}, timeout=120, trust_env=False)

    def get(self, path: str, **kw):
        r = self.c.get(path, **kw)
        r.raise_for_status()
        return r

    def post(self, path: str, body: dict | None = None, **kw) -> dict:
        r = self.c.post(path, json=body, **kw) if body is not None else self.c.post(path, **kw)
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"{path}: {data.get('error', r.status_code)}")
        return data

    def status(self) -> dict:
        return self.get("/api/status").json()

    def search(self, q: str, mode: str = "text") -> dict:
        return self.post("/api/search", {"q": q, "mode": mode, "limit": 10})


def wait_for(fn, timeout: float, every: float = 1.0, what: str = ""):
    end = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception as exc:  # 服务刚起来时连接被拒等
            last_exc = exc
        time.sleep(every)
    raise TimeoutError(f"等待超时（{timeout:g} 秒）：{what}" + (f"；最后的错误：{last_exc}" if last_exc else ""))


# ---------------- 进程 ----------------

class Procs:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.items: list[tuple[str, subprocess.Popen]] = []

    def start(self, name: str, args: list[str], group: bool = False) -> subprocess.Popen:
        out = open(self.run_dir / f"{name}.log", "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if group else 0
        env = os.environ | {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        p = subprocess.Popen(args, cwd=REPO, stdout=out, stderr=subprocess.STDOUT, creationflags=flags, env=env)
        self.items.append((name, p))
        return p

    def stop(self, p: subprocess.Popen, graceful: bool = False, timeout: float = 15) -> None:
        if p.poll() is not None:
            return
        try:
            if graceful:
                p.send_signal(signal.CTRL_BREAK_EVENT)
                p.wait(timeout)
                return
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()

    def stop_all(self) -> None:
        for name, p in reversed(self.items):
            self.stop(p, graceful=(name == "daemon"))


# ---------------- 截图分析 ----------------

def find_marks(img: np.ndarray) -> list[list[float]] | None:
    """四个象限里各找品红角标的中心（有损编码后颜色有偏差，按范围判断）。顺序：左上、右上、左下、右下。"""
    r, g, b = img[..., 0].astype(int), img[..., 1].astype(int), img[..., 2].astype(int)
    mask = (r > 190) & (b > 190) & (g < 90)
    h, w = mask.shape
    out = []
    for ys, xs in ((slice(0, h // 2), slice(0, w // 2)), (slice(0, h // 2), slice(w // 2, w)),
                   (slice(h // 2, h), slice(0, w // 2)), (slice(h // 2, h), slice(w // 2, w))):
        yy, xx = np.nonzero(mask[ys, xs])
        if len(yy) < 20:
            return None
        out.append([float(xx.mean() + xs.start), float(yy.mean() + ys.start)])
    return out


def fit_axis(src: list[float], dst: list[float]) -> tuple[float, float, float]:
    """dst ≈ s·src + t，返回 (s, t, 最大残差)。"""
    a = np.vstack([src, np.ones(len(src))]).T
    (s, t), *_ = np.linalg.lstsq(a, np.array(dst), rcond=None)
    resid = float(np.max(np.abs(a @ np.array([s, t]) - np.array(dst))))
    return float(s), float(t), resid


def black_margin(img: np.ndarray) -> list[int]:
    """从右边、下边往里数，连续有几列 / 几行几乎全黑（截错区域时多出来的部分是黑的；
    Windows 11 窗口自带 1–2 像素深色边框，所以判定时允许几个像素）。"""
    dark = img.max(axis=2) < 16
    cols = next((i for i, c in enumerate(dark.mean(axis=0)[::-1]) if c < 0.9), dark.shape[1])
    rows = next((i for i, r in enumerate(dark.mean(axis=1)[::-1]) if r < 0.9), dark.shape[0])
    return [int(cols), int(rows)]


def norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def bbox_of(quad) -> list[float]:
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    return [min(xs), min(ys), max(xs), max(ys)]


def iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match_lines(expected: list[dict], ocr: list[dict]) -> list[dict]:
    """每行已知文字：找文字最像的 OCR 行；OCR 把一行拆成几段时，把中心落在这行范围里的几段合起来算。"""
    out = []
    for e in expected:
        rect = e["rect"]
        best, best_r = None, 0.0
        for ln in ocr:
            r = difflib.SequenceMatcher(None, norm(e["text"]), norm(ln["text"])).ratio()
            if r > best_r:
                best, best_r = ln, r
        pad = 0.25 * (rect[3] - rect[1])
        inside = [ln for ln in ocr if rect[0] - pad <= (bbox_of(ln["quad"])[0] + bbox_of(ln["quad"])[2]) / 2 <= rect[2] + pad
                  and rect[1] - pad <= (bbox_of(ln["quad"])[1] + bbox_of(ln["quad"])[3]) / 2 <= rect[3] + pad]
        cands = []
        if best is not None and best_r >= 0.5:
            cands.append(([best], bbox_of(best["quad"])))
        if inside:
            bs = [bbox_of(ln["quad"]) for ln in inside]
            cands.append((inside, [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]))
        if not cands:
            out.append({"text": e["text"], "found": False, "iou": 0.0, "ok": False})
            continue
        lines, box = max(cands, key=lambda c: iou(c[1], rect))
        v = iou(box, rect)
        dx = (box[0] + box[2]) / 2 - (rect[0] + rect[2]) / 2
        dy = (box[1] + box[3]) / 2 - (rect[1] + rect[3]) / 2
        out.append({"text": e["text"], "found": True, "ocr_text": " | ".join(ln["text"] for ln in lines),
                    "iou": round(v, 3), "center_dx": round(dx, 1), "center_dy": round(dy, 1),
                    "ocr_box": [round(x, 1) for x in box], "expected_box": [round(x, 1) for x in rect], "ok": v >= IOU_PASS})
    return out


def overlay(img: Image.Image, checks: list[dict], marks, path: Path) -> None:
    im = img.convert("RGB").copy()
    d = ImageDraw.Draw(im)
    for c in checks:
        if "expected_box" in c:
            d.rectangle(c["expected_box"], outline=(0, 170, 0), width=2)
        if "ocr_box" in c:
            d.rectangle(c["ocr_box"], outline=(230, 0, 0), width=2)
    for x, y in marks or []:
        d.ellipse([x - 6, y - 6, x + 6, y + 6], outline=(0, 90, 255), width=2)
    im.save(path)


# ---------------- Edge（临时配置，不碰用户自己的浏览器数据） ----------------

def edge_exe() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge")


class Edge:
    def __init__(self, procs: Procs, run_dir: Path, port: int, url: str):
        self.port = port
        self.profile = Path(tempfile.mkdtemp(prefix="a13-edge-"))
        exe = edge_exe()
        if not exe:
            raise RuntimeError("没有找到 Edge")
        self.proc = procs.start("edge", [
            exe, f"--user-data-dir={self.profile}", f"--remote-debugging-port={port}", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-features=CalculateNativeWinOcclusion",
            "--window-position=80,60", "--window-size=1250,860", f"--app={url}"])
        self.ws_url = wait_for(self._page_ws, 40, 0.5, "Edge 调试端口")
        from websockets.sync.client import ClientConnection, connect
        self.ws: ClientConnection = connect(self.ws_url, max_size=2 ** 24, open_timeout=20).__enter__()
        self.n = 0

    def _page_ws(self):
        targets = httpx.get(f"http://127.0.0.1:{self.port}/json", timeout=3, trust_env=False).json()
        page = next((t for t in targets if t.get("type") == "page"), None)
        return page and page["webSocketDebuggerUrl"]

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=60))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(f"页面脚本出错：{r['exceptionDetails'].get('text')}")
        return r.get("result", {}).get("value")

    def goto(self, url: str) -> None:
        self.call("Page.navigate", url=url)

    def close(self, procs: Procs) -> None:
        try:
            self.ws.close()
        except Exception:
            pass
        procs.stop(self.proc)
        time.sleep(1)
        shutil.rmtree(self.profile, ignore_errors=True)


def edge_page(word: str, tag: str) -> str:
    marks = "".join(f'<div class="m" style="{p}"></div>' for p in (
        "left:10px;top:10px", "right:10px;top:10px", "left:10px;bottom:10px", "right:10px;bottom:10px"))
    lines = "".join(f'<span class="t" style="left:{x}px;top:{y}px;font:{f}">{t.format(word=word, tag=tag)}</span>'
                    for x, y, f, t in EDGE_LINES)
    return (f"<!doctype html><meta charset=utf-8><title>ScreenRecall A13 {word} (edge)</title>"
            "<style>html,body{margin:0;height:100%;background:#fff;color:#111;overflow:hidden}"
            ".m{position:fixed;width:14px;height:14px;background:#f0f}.t{position:absolute;white-space:nowrap;line-height:1.15}"
            f"</style>{marks}{lines}")


# ---------------- 一次录制 ----------------

class Run:
    def __init__(self, a, out: Path, run_dir: Path):
        self.a, self.out, self.run_dir = a, out, run_dir
        self.shots = run_dir / "overlays"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.procs = Procs(run_dir)
        self.api = Api(a.port)
        self.report: dict = {"started": datetime.now().isoformat(timespec="seconds"), "machine": platform.node(),
                             "windows": platform.platform(), "phases": [], "notes": []}

    # ---- 服务 ----
    def start_services(self) -> None:
        guard = str(self.run_dir / "netguard.jsonl")
        data = str(self.run_dir / "data")
        self.daemon = self.procs.start("daemon", [PY, "tools/a13_netguard.py", guard, "-m", "screenrecall.daemon",
                                                  "--data-dir", data], group=True)
        self.web = self.procs.start("webapi", [PY, "tools/a13_netguard.py", guard, "-m", "screenrecall.webapi",
                                               "--data-dir", data, "--port", str(self.a.port)])
        wait_for(lambda: self.api.status()["alive"], 240, 1, "后台心跳")
        log("后台和页面接口已启动（都在断网壳里）")

    def guard_attempts(self) -> list[dict]:
        p = self.run_dir / "netguard.jsonl"
        if not p.exists():
            return []
        rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return [r for r in rows if r["kind"] != "start"]

    # ---- 录一张 ----
    def record(self, title_part: str, word: str, timeout: float = 180) -> dict:
        wins = wait_for(lambda: [w for w in self.api.get("/api/windows").json()["windows"] if title_part in w["title"]],
                        30, 1, f"窗口列表里出现“{title_part}”")
        hwnd = wins[0]["hwnd"]
        self.api.post("/api/control", {"action": "start", "hwnd": hwnd})
        wait_for(lambda: (s := self.api.status())["control"]["actual_state"] == "recording"
                 and not s["pending"], 30, 0.5, "开始录制")
        time.sleep(2.0)
        self.api.post("/api/control", {"action": "snapshot"})

        def hit():  # 只认字面命中：文字搜索里也混有语义匹配，别的花名会被当成“相似”
            res = self.api.search(word)["results"]
            return next((r for r in res if any(b["kind"] == "lex" for b in r.get("boxes", []))), None)
        r = wait_for(hit, timeout, 2, f"搜到“{word}”（截图 + OCR 完成）")
        self.api.post("/api/control", {"action": "pause"})
        fid = r["frame_id"]
        detail = self.api.post(f"/api/frames/{fid}/detail", {"q": word})
        raw = self.api.get(f"/api/frames/{fid}/image").content
        return {"hwnd": hwnd, "fid": fid, "detail": detail, "image": raw, "frame_bounds": frame_bounds(hwnd),
                "window_rect": window_rect(hwnd)}

    # ---- 一个窗口的核对 ----
    def check_capture(self, kind: str, scale, rec: dict, expected_lines: list[dict], canvas_marks, canvas_size,
                      expect_offset, expect_scale: float) -> dict:
        d = rec["detail"]
        img = Image.open(io.BytesIO(rec["image"]))
        arr = np.asarray(img.convert("RGB"))
        fb = rec["frame_bounds"]
        dpi = system.effective_dpi()
        meta = d.get("capture_meta") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        fbw, fbh = fb[2] - fb[0], fb[3] - fb[1]
        if kind == "unaware" and dpi != 96:
            exp_w, exp_h, tol = fbw * 96 / dpi, fbh * 96 / dpi, 2
        else:
            exp_w, exp_h, tol = fbw, fbh, 0
        res = {"kind": kind, "frame_id": rec["fid"], "image_size": [img.width, img.height],
               "detail_size": [d["width"], d["height"]], "frame_bounds_size": [fbw, fbh],
               "expected_size": [round(exp_w, 1), round(exp_h, 1)], "method": meta.get("method"),
               "window_dpi": meta.get("dpi"), "monitor_dpi": dpi}
        res["size_ok"] = (abs(img.width - exp_w) <= tol + 0.5 and abs(img.height - exp_h) <= tol + 0.5
                          and [img.width, img.height] == [d["width"], d["height"]])
        res["black_margin"] = black_margin(arr)
        res["black_ok"] = max(res["black_margin"]) <= 4
        marks = find_marks(arr)
        res["marks_found"] = marks is not None
        if marks:
            sx, tx, rx = fit_axis([m[0] for m in canvas_marks], [m[0] for m in marks])
            sy, ty, ry = fit_axis([m[1] for m in canvas_marks], [m[1] for m in marks])
            res["map"] = {"sx": round(sx, 4), "sy": round(sy, 4), "tx": round(tx, 1), "ty": round(ty, 1),
                          "residual": round(max(rx, ry), 2)}
            res["scale_ok"] = abs(sx - expect_scale) < 0.02 * expect_scale and abs(sy - expect_scale) < 0.02 * expect_scale
            res["marks_ok"] = res["scale_ok"] and max(rx, ry) < 2.0
            if expect_offset is not None:
                res["offset_error"] = [round(tx - expect_offset[0], 1), round(ty - expect_offset[1], 1)]
                res["offset_ok"] = max(abs(v) for v in res["offset_error"]) <= 2.0
            # 内容区右 / 下边缘应贴着截图右 / 下边（只差窗口边框）
            gap_r = img.width - (tx + sx * canvas_size[0])
            gap_b = img.height - (ty + sy * canvas_size[1])
            res["content_edge_gap"] = [round(gap_r, 1), round(gap_b, 1)]
            if kind != "edge":  # Edge 新界面在网页四周自带约 5 CSS px 的圆角边距，这项只对 Tk 窗口判定
                res["edge_ok"] = -1.5 <= gap_r <= 3.5 and -1.5 <= gap_b <= 3.5
            mapped = [{"text": e["text"], "rect": [sx * e["bbox"][0] + tx, sy * e["bbox"][1] + ty,
                                                   sx * e["bbox"][2] + tx, sy * e["bbox"][3] + ty]} for e in expected_lines]
            res["lines"] = match_lines(mapped, d["lines"])
            res["lines_ok"] = all(x["ok"] for x in res["lines"])
        else:
            res["lines"], res["lines_ok"], res["marks_ok"] = [], False, False
        name = f"{scale}-{kind}.png"
        overlay(img, res.get("lines", []), marks, self.shots / name)
        res["overlay"] = str(self.shots / name)
        keys = ["size_ok", "black_ok", "marks_ok", "lines_ok"] + [k for k in ("offset_ok", "edge_ok") if k in res]
        res["ok"] = all(res.get(k) for k in keys)
        return res

    def tk_window(self, kind: str, word: str, tag: str) -> tuple[subprocess.Popen, dict]:
        info_path = self.run_dir / f"win-{tag}-{kind}.json"
        p = self.procs.start(f"win-{tag}-{kind}", [PY, "tools/a13_window.py", "--dpi", kind, "--word", word,
                                                   "--tag", str(tag), "--out", str(info_path)])
        info = wait_for(lambda: info_path.exists() and json.loads(info_path.read_text(encoding="utf-8")), 30, 0.3,
                        f"{kind} 测试窗口")
        to_front(info["hwnd"])
        return p, info

    def run_tk(self, kind: str, scale, word: str) -> dict:
        p, info = self.tk_window(kind, word, str(scale))
        try:
            rec = self.record(info["title"], word)
            fb, wr = rec["frame_bounds"], rec["window_rect"]
            if kind == "aware":  # 画布原点（物理）− 边框左上角
                off = [info["canvas_root"][0] - fb[0], info["canvas_root"][1] - fb[1]]
            else:  # 逻辑坐标里画布相对窗口矩形的位置，再减去后台按比例裁掉的边框偏移
                k = 96 / system.effective_dpi()
                off = [info["canvas_root"][0] - info["window_rect"][0] - round((fb[0] - wr[0]) * k),
                       info["canvas_root"][1] - info["window_rect"][1] - round((fb[1] - wr[1]) * k)]
            res = self.check_capture(kind, scale, rec, info["lines"], info["marks"], info["canvas_size"], off, 1.0)
            res["awareness"] = info["awareness"]
            return res
        finally:
            self.procs.stop(p)

    def run_edge(self, scale, word: str, edge: Edge) -> dict:
        page = self.run_dir / f"page-{scale}.html"
        page.write_text(edge_page(word, str(scale)), encoding="utf-8")
        edge.goto(page.as_uri())
        wait_for(lambda: edge.eval("document.readyState === 'complete' && document.fonts.status === 'loaded'"), 20, 0.3,
                 "测试页加载")
        time.sleep(0.8)
        geo = edge.eval("""(() => {
            const r = (e) => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom]; };
            return { dpr: devicePixelRatio, size: [innerWidth, innerHeight],
                     marks: [...document.querySelectorAll('.m')].map(e => { const b = r(e); return [(b[0]+b[2])/2, (b[1]+b[3])/2]; }),
                     lines: [...document.querySelectorAll('.t')].map(e => ({ text: e.textContent, bbox: r(e) })) };
        })()""")
        hwnd_title = f"ScreenRecall A13 {word}"
        wins = wait_for(lambda: [w for w in self.api.get("/api/windows").json()["windows"] if hwnd_title in w["title"]],
                        20, 1, "Edge 测试页窗口")
        to_front(wins[0]["hwnd"])
        rec = self.record(hwnd_title, word)
        res = self.check_capture("edge", scale, rec, geo["lines"], geo["marks"], geo["size"], None, geo["dpr"])
        res["dpr"] = geo["dpr"]
        return res

    def ui_check(self, edge: Edge, fid: str, word: str) -> dict:
        """在 ScreenRecall 详情页里量：SVG 高亮多边形在屏幕上的位置 = 图片位置 + OCR 坐标 × 显示比例？"""
        edge.goto("about:blank")  # 先离开上一张，免得量到上一张详情页残留的框
        time.sleep(0.5)
        edge.goto(f"{self.api.base}/#/frame/{fid}?q={word}")
        js = """(() => {
          const poly = [...document.querySelectorAll('svg polygon')];
          if (!poly.length) return null;
          const img = poly[0].closest('div').querySelector('img');
          if (!img || !img.complete || !img.naturalWidth) return null;
          const ir = img.getBoundingClientRect();
          return { dpr: devicePixelRatio, img: [ir.left, ir.top, ir.width, ir.height], natural: [img.naturalWidth, img.naturalHeight],
                   polys: poly.map(p => { const b = p.getBoundingClientRect();
                     return { rect: [b.left, b.top, b.right, b.bottom],
                              pts: p.getAttribute('points').split(' ').map(s => s.split(',').map(Number)) }; }) };
        })()"""
        g = wait_for(lambda: edge.eval(js), 30, 0.5, "详情页高亮框")
        time.sleep(1.2)  # 高亮框有入场动画（最长约 0.85 秒），结束后再量
        g = edge.eval(js) or g
        ix, iy, iw, ih = g["img"]
        kx, ky = iw / g["natural"][0], ih / g["natural"][1]
        worst = 0.0
        for p in g["polys"]:
            xs, ys = [q[0] for q in p["pts"]], [q[1] for q in p["pts"]]
            want = [ix + min(xs) * kx, iy + min(ys) * ky, ix + max(xs) * kx, iy + max(ys) * ky]
            worst = max(worst, *(abs(a - b) for a, b in zip(want, p["rect"])))
        return {"frame_id": fid, "dpr": g["dpr"], "boxes": len(g["polys"]), "max_error_css_px": round(worst, 2),
                "max_error_device_px": round(worst * g["dpr"], 2), "ok": worst <= 1.0}

    # ---- 阶段 ----
    def offline_phase(self) -> dict:
        ph = {"phase": "offline", "wifi_off_requested": self.a.wifi_off, "checks": {}}
        st = system.wifi_status()
        self.wifi_profile = st["profile"] if self.a.wifi_off and st["connected"] else ""
        if self.a.wifi_off:
            if st["connected"]:
                system.wifi_off()
                wait_for(lambda: not system.wifi_status()["connected"], 20, 0.5, "Wi-Fi 断开")
                log("Wi-Fi 已断开")
            ph["wifi_disconnected"] = not system.wifi_status()["connected"]
        ph["internet_reachable_during"] = system.internet_reachable()
        log(f"外网可达：{ph['internet_reachable_during']}")
        word = WORDS["offline"]["aware"]
        c = ph["checks"]
        p, info = self.tk_window("aware", word, "offline")
        try:
            rec = self.record(info["title"], word)
            c["text_search"] = {"ok": True, "frame_id": rec["fid"]}
        except Exception as exc:
            c["text_search"] = {"ok": False, "error": str(exc)}
            rec = None
        finally:
            self.procs.stop(p)
        try:
            q = "white window with black text and pink squares in the corners"
            want = rec["fid"] if rec else None
            # 全图向量在 OCR 之后才算，先等测试截图进入画面索引
            res = wait_for(lambda: (r := self.api.search(q, mode="image"))["rank_basis"] == "image"
                           and (want is None or want in [x["frame_id"] for x in r["results"]]) and r, 180, 2,
                           "画面搜索出结果")
            fids = [r["frame_id"] for r in res["results"]]
            c["image_search"] = {"ok": bool(fids), "results": len(fids), "message": res.get("message"),
                                 "test_frame_rank": fids.index(rec["fid"]) + 1 if rec and rec["fid"] in fids else None}
        except Exception as exc:
            c["image_search"] = {"ok": False, "error": str(exc)}
        if rec:
            img = Image.open(io.BytesIO(rec["image"]))
            c["view_original"] = {"ok": [img.width, img.height] == [rec["detail"]["width"], rec["detail"]["height"]],
                                  "size": [img.width, img.height], "bytes": len(rec["image"])}
        else:
            c["view_original"] = {"ok": False, "error": "没有可查看的截图"}
        try:
            c["import"] = self.import_check()
        except Exception as exc:
            c["import"] = {"ok": False, "error": str(exc)}
        ph["internet_reachable_after_checks"] = system.internet_reachable()
        if self.a.wifi_off and self.wifi_profile:
            system.wifi_on(self.wifi_profile)
            try:
                ph["wifi_reconnected"] = bool(wait_for(lambda: system.internet_reachable(), 60, 2, "Wi-Fi 重连"))
                log("Wi-Fi 已重连")
            except TimeoutError:
                ph["wifi_reconnected"] = False
                log("Wi-Fi 一分钟内没连上外网，继续缩放测试（不需要网络），结束时会再试一次")
        ph["ok"] = all(v.get("ok") for v in c.values())
        return ph

    def import_check(self) -> dict:
        im = Image.new("RGB", (1280, 720), "white")
        d = ImageDraw.Draw(im)
        d.text((80, 120), f"{IMPORT_WORD} import check 5820", fill="black", font=ImageFont.truetype("segoeui.ttf", 56))
        d.text((80, 240), "This screenshot was imported while the network was off.", fill="black",
               font=ImageFont.truetype("segoeui.ttf", 32))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        r = self.api.post("/api/import", files={"files": ("a13-import.png", buf.getvalue(), "image/png")},
                          data={"label": "A13 offline import"})
        hit = wait_for(lambda: next((x for x in self.api.search(IMPORT_WORD)["results"]
                                     if any(b["kind"] == "lex" for b in x.get("boxes", []))), None), 180, 2,
                       f"搜到导入的“{IMPORT_WORD}”")
        return {"ok": r.get("ok") == 1 and bool(hit), "imported": r.get("ok"), "frame_id": hit["frame_id"]}

    def scale_phase(self, scale: int) -> dict:
        ph = {"phase": f"scale-{scale}", "scale": scale, "windows": [], "ui": []}
        if system.primary()["current"] != scale:
            log(f"切换缩放到 {scale}% …")
            try:
                system.set_scale(scale)
            except Exception as exc:
                log(f"切换失败：{exc}")
                return ph | {"ok": False, "error": f"切换缩放失败：{exc}"}
            time.sleep(3)  # 让各窗口处理完 DPI 变化
        ph["effective_dpi"] = system.effective_dpi()
        log(f"当前缩放 {system.primary()['current']}%（有效 DPI {ph['effective_dpi']}）")
        words = WORDS.get(scale) or {k: f"{v}{scale}" for k, v in WORDS[100].items()}
        for kind in ("aware", "unaware"):
            try:
                r = self.run_tk(kind, scale, words[kind])
            except Exception as exc:
                r = {"kind": kind, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log(f"  {kind}: {'通过' if r.get('ok') else '未通过'}")
            ph["windows"].append(r)
        edge = None
        try:
            edge = Edge(self.procs, self.run_dir, self.a.edge_port, "about:blank")
            try:
                r = self.run_edge(scale, words["edge"], edge)
            except Exception as exc:
                r = {"kind": "edge", "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            log(f"  edge: {'通过' if r.get('ok') else '未通过'}")
            ph["windows"].append(r)
            for w in ph["windows"]:
                if w.get("frame_id"):
                    try:
                        u = self.ui_check(edge, w["frame_id"], words[w["kind"]])
                    except Exception as exc:
                        u = {"frame_id": w["frame_id"], "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    u["kind"] = w["kind"]
                    ph["ui"].append(u)
            log(f"  页面高亮框：{'通过' if ph['ui'] and all(u['ok'] for u in ph['ui']) else '未通过'}")
        except Exception as exc:
            ph["windows"].append({"kind": "edge", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            if edge:
                edge.close(self.procs)
        ph["ok"] = all(w.get("ok") for w in ph["windows"]) and bool(ph["ui"]) and all(u.get("ok") for u in ph["ui"])
        return ph


# ---------------- 报告 ----------------

def write_report(out: Path, rep: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "a13.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    yes = lambda v: "通过" if v else "**未通过**"  # noqa: E731
    L = [f"# A13 实测：断网可用 + 缩放对齐（{rep['started'][:10]}，{rep['machine']}）", "",
         f"- 系统：{rep['windows']}；主显示器原缩放 {rep.get('original_scale')}%，推荐 {rep.get('recommended_scale')}%",
         f"- 脚本：`tools/a13_check.py {rep.get('args', '')}`；结束时缩放已恢复为 {rep.get('restored_scale')}%",
         f"- 总结论：{yes(rep.get('ok'))}", ""]
    for ph in rep["phases"]:
        if ph["phase"] == "offline":
            L += ["## 断网", "",
                  f"- 真断 Wi-Fi：{'是' if ph.get('wifi_disconnected') else '否（只用进程内断网）'}；"
                  f"检查期间外网可达：{ph.get('internet_reachable_during')}；后台和页面接口都在进程内断网壳里运行",
                  f"- 两个进程尝试对外连接 {rep.get('net_attempts', 0)} 次" +
                  (f"：{'；'.join(rep.get('net_attempt_targets', []))}" if rep.get("net_attempts") else ""),
                  *(["- 说明：`_detect_agent` 是 huggingface_hub 1.33 拼请求头时取“AI 代理名单”（每天一次、失败就当空名单），"
                     "不受 local_files_only 控制；HF_HUB_OFFLINE 要在 huggingface_hub 被导入前设好才能关掉它。断网时立即失败、"
                     "不影响功能（调用栈见数据目录 netguard.jsonl）。"]
                    if any("_detect_agent" in t for t in rep.get("net_attempt_targets", [])) else []), "",
                  "| 功能 | 结果 | 说明 |", "|---|---|---|"]
            for k, label in (("text_search", "文字搜索"), ("image_search", "画面搜索"), ("view_original", "查看原图"),
                             ("import", "导入截图并搜到")):
                v = ph["checks"].get(k, {})
                extra = v.get("error") or ", ".join(f"{a}={b}" for a, b in v.items() if a not in ("ok",))
                L.append(f"| {label} | {yes(v.get('ok'))} | {extra} |")
            L.append("")
            continue
        if ph.get("error"):
            L += [f"## 缩放 {ph['scale']}%", "", f"**没有测成**：{ph['error']}", ""]
            continue
        L += [f"## 缩放 {ph['scale']}%（有效 DPI {ph.get('effective_dpi')}）", "",
              "| 窗口 | 捕获方式 | 截图 / 期望尺寸 | 右下黑边 | 角标比例 · 残差 | 边界偏差 | 文字行 IoU（最小） | 结论 |",
              "|---|---|---|---|---|---|---|---|"]
        for w in ph["windows"]:
            if w.get("error"):
                L.append(f"| {w['kind']} | — | — | — | — | — | — | **出错**：{w['error']} |")
                continue
            m = w.get("map") or {}
            ious = [x["iou"] for x in w.get("lines", [])]
            off = w.get("offset_error") or w.get("content_edge_gap")
            L.append(f"| {w['kind']} | {w.get('method')} | {w['image_size'][0]}×{w['image_size'][1]} / "
                     f"{w['expected_size'][0]:g}×{w['expected_size'][1]:g} | {w['black_margin'][0]} / {w['black_margin'][1]} px | "
                     f"{m.get('sx', '—')} · {m.get('residual', '—')} px | {off} | "
                     f"{min(ious) if ious else '—'}（{sum(x['ok'] for x in w.get('lines', []))}/{len(ious)} 行） | {yes(w['ok'])} |")
        L += ["", "页面高亮框（Edge 打开详情页，量 SVG 多边形和图片的相对位置）：", ""]
        for u in ph["ui"]:
            L.append(f"- {u['kind']}：" + (f"出错 {u['error']}" if u.get("error") else
                     f"{u['boxes']} 个框，最大偏差 {u['max_error_css_px']} CSS px（{u['max_error_device_px']} 物理 px），"
                     f"devicePixelRatio {u['dpr']}，{yes(u['ok'])}"))
        L.append("")
    L += ["## 判定口径", "",
          "- 截图尺寸：DPI 感知窗口 = 窗口可见边框的物理尺寸；不感知 DPI 的窗口 = 物理尺寸 × 96 / 显示器 DPI（±2 px），并且和库里记录的宽高一致。",
          "- 右下黑边：从右边 / 下边往里连续几乎全黑的列数 / 行数 ≤ 4（区域算错时多出来的部分是黑的；Windows 11 窗口自带 1–2 像素深色边框）。",
          "- 角标：四角品红方块的位置拟合出的比例 = 1（Tk 窗口）或 devicePixelRatio（Edge），残差 < 2 px；"
          "Tk 窗口另外用窗口自己报告的画布位置独立算一遍偏移，误差 ≤ 2 px，画布右、下边缘贴着截图边缘（≤ 3.5 px，窗口边框）；"
          "Edge 新界面在网页四周自带约 5 CSS px 圆角边距，这一列只记录不判定。",
          f"- 文字行：每行已知文字的实际位置（由角标换算）和 OCR 框的 IoU ≥ {IOU_PASS}。",
          "- 页面高亮框：详情页里 SVG 多边形的屏幕位置和“图片位置 + OCR 坐标 × 显示比例”最大相差 ≤ 1 CSS px。",
          "- 断网：后台和页面接口在进程内断网壳里运行（对外连接和域名解析一律失败并记录）；加 --wifi-off 时同时真断开 Wi-Fi。",
          "", "叠加了框的截图（绿 = 文字实际位置，红 = OCR 框，蓝圈 = 角标）在数据目录 `overlays/` 里，不提交。", ""]
    (out / "README.md").write_text("\n".join(L), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="A13 一键实测")
    ap.add_argument("--out", help="结果目录，默认 results\\windows\\<今天>-<机器名>\\a13")
    ap.add_argument("--scales", default="100,125,150", help="要测的缩放，逗号分隔；current = 只测当前缩放、不切换")
    ap.add_argument("--wifi-off", action="store_true", help="断网检查时真的断开 Wi-Fi（结束后自动重连）")
    ap.add_argument("--skip-offline", action="store_true")
    ap.add_argument("--port", type=int, default=8613)
    ap.add_argument("--edge-port", type=int, default=9343)
    ap.add_argument("--yes", action="store_true", help="不询问，直接开始")
    a = ap.parse_args()

    system.set_dpi_aware()
    if not (REPO / "web" / "dist" / "index.html").exists():
        print("没有找到网页前端构建产物 web\\dist（页面高亮框检查要用）：先在 web\\ 下运行 npm run build")
        return 2
    if not edge_exe():
        print("没有找到 Microsoft Edge（Edge 测试页和页面高亮框检查要用）")
        return 2
    a.out = a.out or str(Path("results") / "windows" / f"{datetime.now():%Y-%m-%d}-{platform.node()}" / "a13")
    out = (REPO / a.out).resolve() if not Path(a.out).is_absolute() else Path(a.out)
    run_dir = REPO / "data-a13" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    prim = system.primary()
    original = prim["current"]
    scales = [original] if a.scales == "current" else [int(x) for x in a.scales.split(",")]
    bad = [s for s in scales if s not in prim["allowed"]]
    if bad:
        print(f"主显示器只支持 {prim['allowed']}，不能测 {bad}")
        return 2
    # 先测当前缩放，再从大到小，最后恢复：切换次数最少
    scales = sorted(set(scales), key=lambda s: (s != original, -s))
    wifi = system.wifi_status()
    state = {"scale": original, "wifi_profile": wifi["profile"] if a.wifi_off and wifi["connected"] else ""}
    # 含 Wi-Fi 名称，放数据目录（不进 git），不放结果目录
    (run_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    plan = [f"缩放 {s}%" for s in scales]
    print("A13 实测将要做：")
    if not a.skip_offline:
        print("  · 断网检查" + ("（会断开 Wi-Fi，检查完立刻重连）" if a.wifi_off else "（只在进程内断网，不动 Wi-Fi）"))
    print(f"  · 依次在 {', '.join(plan)} 下录测试窗口并核对（会切换主显示器缩放，结束后恢复为 {original}%）")
    print(f"  · 数据目录 {run_dir}，结果写到 {out}")
    print("  期间请不要操作电脑；中途按 Ctrl+C 可以停止，同样会恢复设置。")
    if not a.yes:
        input("按回车开始…")

    run = Run(a, out, run_dir)
    rep = run.report
    rep.update({"original_scale": original, "recommended_scale": prim["recommended"], "scales": scales,
                "args": " ".join(sys.argv[1:]), "data_dir": str(run_dir)})
    t0 = time.monotonic()
    try:
        run.start_services()
        if not a.skip_offline:
            log("断网检查 …")
            rep["phases"].append(run.offline_phase())
        for s in scales:
            rep["phases"].append(run.scale_phase(s))
    except KeyboardInterrupt:
        rep["notes"].append("被 Ctrl+C 中止")
        log("已中止，正在恢复设置 …")
    except Exception:
        rep["notes"].append("出错中止：" + traceback.format_exc())
        log("出错：\n" + traceback.format_exc())
    finally:
        try:
            if system.primary()["current"] != original:
                system.set_scale(original)
                log(f"缩放已恢复为 {original}%")
        except Exception as exc:
            rep["notes"].append(f"恢复缩放失败：{exc}（请到 设置 → 屏幕 → 缩放 手动改回 {original}%）")
            log(f"恢复缩放失败：{exc}；请手动改回 {original}%")
        if state["wifi_profile"] and not system.wifi_status()["connected"]:
            system.wifi_on(state["wifi_profile"])
            log("已请求重连 Wi-Fi")
        run.procs.stop_all()
        rep["restored_scale"] = system.primary()["current"]
        attempts = run.guard_attempts()
        rep["net_attempts"] = len(attempts)
        rep["net_attempt_targets"] = sorted({f"{x['module']} {x['kind']} {x['target']}（{(x.get('stack') or ['?'])[-1]}）"
                                             for x in attempts})[:20]
        rep["minutes"] = round((time.monotonic() - t0) / 60, 1)
        rep["ok"] = bool(rep["phases"]) and all(p.get("ok") for p in rep["phases"]) and not rep["notes"]
        write_report(out, rep)
        log(f"完成（{rep['minutes']} 分钟）：{'全部通过' if rep['ok'] else '有未通过项'}。报告：{out / 'README.md'}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
