"""对一个应用跑完整探针：启动 → 摆放窗口 → 截图+OCR → 多次 UIA 提取 → 遮挡/最小化 → 关闭。

用法：python probe.py <app> --tmp <临时目录> --out <结果目录>
app: notepad | conhost | wt | explorer | edge | chrome | vscode | word | excel

隐私：只读取本脚本自己启动的窗口（按启动得到的 PID / 启动前后新增的 hwnd 认定），
不枚举、不读取用户已有窗口的 UI 树；全部内容来自 fixtures.py 的合成数据。
原始文字转储只写到 --tmp（运行结束删除），--out 里只有统计指标与合成内容的示例。
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uia_core as U  # noqa: E402  (先导入：设置 DPI 感知)
import fixtures as F  # noqa: E402
import psutil  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsWindow.argtypes = [wintypes.HWND]
_ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_ENUM, wintypes.LPARAM]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND

WIN_RECT = (120, 120, 1920, 1520)  # 物理像素：1800×1400（150% 下约 1200×933 逻辑像素）
SW_MINIMIZE, SW_RESTORE = 6, 9
WM_CLOSE = 0x0010

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CODE = str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Microsoft VS Code" / "Code.exe")


# ---------- 窗口与进程 ----------

def top_level_hwnds() -> list[int]:
    """只拿句柄（不读标题、不读 UI 树），用于"启动前后新增了哪些窗口"的差集。"""
    out: list[int] = []
    user32.EnumWindows(_ENUM(lambda h, _l: out.append(int(h)) or True), 0)
    return out


def own_windows(pids: set[int], cls: str | None = None, exclude: set[int] = frozenset()) -> list[int]:
    res = []
    for h in top_level_hwnds():
        if h in exclude or U.winapi.window_pid(h) not in pids:
            continue
        if not U.winapi._IsWindowVisible(h) or user32.GetWindow(h, 4):  # 可见、无 owner
            continue
        if cls and U.winapi.class_name(h) != cls:
            continue
        res.append(h)
    return res


def wait_for(fn, timeout=25.0, step=0.4):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def tree_pids(pid: int) -> set[int]:
    try:
        p = psutil.Process(pid)
        return {pid} | {c.pid for c in p.children(recursive=True)}
    except psutil.NoSuchProcess:
        return set()


def new_procs(name: str, since: float) -> set[int]:
    out = set()
    for p in psutil.process_iter(["name", "create_time"]):
        if (p.info["name"] or "").lower() == name.lower() and (p.info["create_time"] or 0) >= since - 1:
            out.add(p.pid)
    return out


def kill_tree(pids: set[int]) -> None:
    procs = []
    for pid in pids:
        try:
            p = psutil.Process(pid)
            procs.append(p)
            procs.extend(p.children(recursive=True))
        except psutil.NoSuchProcess:
            pass
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=8)


def place(hwnd: int, rect=WIN_RECT) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.3)
    l, t, r, b = rect
    user32.SetWindowPos(hwnd, None, l, t, r - l, b - t, 0x0004 | 0x0010)  # NOZORDER | NOACTIVATE


def tree_stats(pids: set[int]) -> dict:
    mem = cpu = 0.0
    n = 0
    for pid in pids:
        try:
            p = psutil.Process(pid)
            mem += p.memory_info().private
            ct = p.cpu_times()
            cpu += ct.user + ct.system
            n += 1
        except psutil.Error:
            pass
    return {"procs": n, "private_mib": round(mem / 2**20, 1), "cpu_s": round(cpu, 2)}


def idle_cpu(pids_fn, seconds=10.0) -> dict:
    a = tree_stats(pids_fn())
    time.sleep(seconds)
    b = tree_stats(pids_fn())
    return {"cpu_pct_of_one_core": round((b["cpu_s"] - a["cpu_s"]) / seconds * 100, 1),
            "private_mib": b["private_mib"], "procs": b["procs"]}


OCCLUDER = r'''
import ctypes, sys, tkinter as tk
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
x, y, w, h = map(int, sys.argv[1:5])
r = tk.Tk(); r.overrideredirect(True); r.attributes("-topmost", True)
r.geometry(f"{w}x{h}+{x}+{y}"); r.configure(bg="#334")
tk.Label(r, text="OCCLUDER 遮挡窗口", fg="white", bg="#334", font=("Segoe UI", 28)).pack(expand=True)
r.mainloop()
'''


# ---------- 各应用启动 ----------

class App:
    def __init__(self, name, hwnd, pids_fn, close_fn, markers, notes=None, stats_pids_fn=None):
        self.name, self.hwnd, self.pids_fn, self.close_fn = name, hwnd, pids_fn, close_fn
        self.markers = markers
        self.notes = notes or {}
        self.stats_pids_fn = stats_pids_fn


def launch_notepad(paths, tmp):
    t0 = time.time()
    if any((p.info["name"] or "").lower() == "notepad.exe" for p in psutil.process_iter(["name"])):
        raise SystemExit("用户已有记事本在运行：Win11 记事本会把文件开成已有窗口的标签页，跳过以免读到用户窗口")
    subprocess.Popen(["notepad.exe", str(paths["note"])])
    hwnd = wait_for(lambda: (own_windows(new_procs("Notepad.exe", t0), "Notepad") or [None])[0])
    if not hwnd:
        raise SystemExit("记事本窗口未出现")
    pids = new_procs("Notepad.exe", t0)

    def close():
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if not wait_for(lambda: not user32.IsWindow(hwnd), 8):
            kill_tree(pids)

    return App("notepad", hwnd, lambda: pids, close,
               ["NOTE-L001", "NOTE-L020", "NOTE-L150", "NOTE-L300"])


def launch_conhost(paths, tmp):
    p = subprocess.Popen(["conhost.exe", "powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-Command",
                          F.console_command()])
    hwnd = wait_for(lambda: (own_windows(tree_pids(p.pid), "ConsoleWindowClass") or [None])[0])
    if not hwnd:
        raise SystemExit("控制台窗口未出现")
    pids = tree_pids(p.pid)
    time.sleep(2)
    return App("conhost", hwnd, lambda: tree_pids(p.pid) or pids, lambda: kill_tree({p.pid}),
               ["CON-L001", "CON-L150", "CON-L200"])


def launch_wt(paths, tmp):
    t0 = time.time()
    if any((p.info["name"] or "").lower() == "windowsterminal.exe" for p in psutil.process_iter(["name"])):
        raise SystemExit("用户已有 Windows Terminal 在运行，跳过")
    # wt 会把命令行里的 ";" 当成"新标签页"分隔符，改用脚本文件
    script = tmp / "fixtures" / "wt-print.ps1"
    script.write_text(F.console_command().replace("; ", "\n"), encoding="utf-8-sig")
    subprocess.Popen(["wt.exe", "-w", "new", "powershell.exe", "-NoLogo", "-NoProfile", "-NoExit",
                      "-ExecutionPolicy", "Bypass", "-File", str(script)])
    hwnd = wait_for(lambda: (own_windows(new_procs("WindowsTerminal.exe", t0), "CASCADIA_HOSTING_WINDOW_CLASS")
                             or [None])[0])
    if not hwnd:
        raise SystemExit("Windows Terminal 窗口未出现")
    pids = new_procs("WindowsTerminal.exe", t0)
    time.sleep(3)

    def close():
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        wait_for(lambda: not user32.IsWindow(hwnd), 6)
        kill_tree(pids)

    return App("wt", hwnd, lambda: pids, close, ["CON-L001", "CON-L150", "CON-L200"])


def launch_explorer(paths, tmp):
    before = set(top_level_hwnds())
    subprocess.Popen(["explorer.exe", str(paths["explorer"])])
    folder = paths["explorer"].name

    def find():
        for h in top_level_hwnds():
            if h in before or U.winapi.class_name(h) != "CabinetWClass":
                continue
            if folder in U.winapi.window_title(h):  # 只读新出现窗口的标题
                return h
        return None

    hwnd = wait_for(find, 15)
    if not hwnd:
        raise SystemExit("资源管理器没有新开窗口（可能开成了已有窗口的标签页），为免读到用户窗口而跳过")
    time.sleep(2)
    pid = U.winapi.window_pid(hwnd)

    def close():
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        wait_for(lambda: not user32.IsWindow(hwnd), 6)

    # 导航窗格里有用户自己的固定文件夹 / OneDrive 名称：提交结果不留文字示例
    return App("explorer", hwnd, lambda: {pid}, close, ["EXP-F01", "EXP-F10", "EXP-F40", folder],
               notes={"private_ui": True})


def _launch_chromium(name, exe, flags, paths, tmp):
    ud = tmp / f"profile-{name}"
    urls = [paths["page"].as_uri(), paths["page2"].as_uri()]
    p = subprocess.Popen([exe, f"--user-data-dir={ud}", "--no-first-run", "--no-default-browser-check",
                          "--disable-sync", "--new-window", *flags, *urls])

    def find():
        for h in own_windows(tree_pids(p.pid) | {p.pid}, "Chrome_WidgetWin_1"):
            if "SR-UIA" in U.winapi.window_title(h):
                return h
        return None

    hwnd = wait_for(find, 30)
    if not hwnd:
        kill_tree({p.pid})
        raise SystemExit(f"{name} 窗口未出现")
    pids_fn = lambda: tree_pids(p.pid)  # noqa: E731
    return App(name, hwnd, pids_fn, lambda: kill_tree({p.pid}),
               ["探针页面", "TICK", "probe_user_01", F.PASSWORD_DUMMY, "CANVAS-ONLY", "ARIAHIDDEN-VISIBLE",
                "SRONLY-INVISIBLE", "DISPLAYNONE-SECRET", "INV-20261026-0731", "ORD-2026-0003",
                "CODE-BLOCK-MARKER", "ARTICLE-P01", "ARTICLE-P20", "ARTICLE-P40", "HIDDENTAB-MARKER"],
               stats_pids_fn=pids_fn)


def launch_edge(paths, tmp):
    return _launch_chromium("edge", EDGE, ["--inprivate"], paths, tmp)


def launch_chrome(paths, tmp):
    if not Path(CHROME).exists():
        raise SystemExit("未安装 Chrome")
    return _launch_chromium("chrome", CHROME, ["--incognito"], paths, tmp)


def launch_vscode_sr(paths, tmp):
    """同上，但临时配置里打开"读屏优化模式"，看编辑器正文能否经 UIA 取到。"""
    ud = tmp / "vscode-user-sr"
    (ud / "User").mkdir(parents=True, exist_ok=True)
    (ud / "User" / "settings.json").write_text('{"editor.accessibilitySupport": "on"}', encoding="utf-8")
    app = launch_vscode(paths, tmp, ud=ud)
    app.name = "vscode_sr"
    return app


def launch_vscode(paths, tmp, ud=None):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("VSCODE_", "ELECTRON_"))}
    ud, ext = ud or tmp / "vscode-user", tmp / "vscode-ext"
    p = subprocess.Popen([CODE, "--user-data-dir", str(ud), "--extensions-dir", str(ext), "--disable-extensions",
                          "--new-window", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
                          str(paths["code_dir"]), str(paths["code"])], env=env)

    def find():
        for h in own_windows(tree_pids(p.pid) | {p.pid}, "Chrome_WidgetWin_1"):
            if "sample.py" in U.winapi.window_title(h):
                return h
        return None

    hwnd = wait_for(find, 40)
    if not hwnd:
        kill_tree({p.pid})
        raise SystemExit("VS Code 窗口未出现")
    time.sleep(4)
    pids_fn = lambda: tree_pids(p.pid)  # noqa: E731
    return App("vscode", hwnd, pids_fn, lambda: kill_tree({p.pid}),
               ["CODE-L001", "CODE-L010", "CODE-L080", "CODE-L150", "sample.py"], stats_pids_fn=pids_fn)


# Office 用自动化接口新建文档：不落盘、不进"最近使用"列表。
# 自动化放在单独的辅助进程里做完就退出，探针进程不持有 Office 的 COM 连接
OFFICE_HELPER = r'''
import sys, time, json, comtypes.client
sys.path.insert(0, sys.argv[2])
import fixtures as F
kind = sys.argv[1]
if kind == "word":
    app = comtypes.client.CreateObject("Word.Application", dynamic=True)
    app.Visible = True
    doc = app.Documents.Add()
    doc.Content.Text = "\r".join(F.word_paragraphs())
    doc.Saved = True
    time.sleep(1)
    hwnd = int(app.ActiveWindow.Hwnd)
else:
    app = comtypes.client.CreateObject("Excel.Application", dynamic=True)
    app.Visible = True
    wb = app.Workbooks.Add()
    ws = wb.Worksheets(1)
    for r, row in enumerate(F.excel_rows(), 1):
        for c, v in enumerate(row, 1):
            ws.Cells(r, c).Value2 = v
    wb.Saved = True
    app.UserControl = True
    time.sleep(1)
    hwnd = int(app.Hwnd)
print(json.dumps({"hwnd": hwnd}), flush=True)
'''


def _launch_office(kind, exe_name, markers):
    if any((p.info["name"] or "").lower() == exe_name for p in psutil.process_iter(["name"])):
        raise SystemExit(f"用户已有 {exe_name} 在运行，跳过")
    t0 = time.time()
    out = subprocess.run([sys.executable, "-c", OFFICE_HELPER, kind, str(Path(__file__).resolve().parent)],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        kill_tree(new_procs(exe_name, t0))
        raise SystemExit(f"{kind} 辅助进程失败：{out.stderr[-500:]}")
    hwnd = json.loads(out.stdout.strip().splitlines()[-1])["hwnd"]
    pid = U.winapi.window_pid(hwnd)
    time.sleep(2)

    def close():
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # 文档已标记"已保存"，不会弹保存框
        if not wait_for(lambda: not user32.IsWindow(hwnd), 10):
            kill_tree({pid})
        wait_for(lambda: not psutil.pid_exists(pid), 10)
        if psutil.pid_exists(pid):
            kill_tree({pid})

    return App(kind, hwnd, lambda: {pid}, close, markers, notes={"private_ui": True, "slow": True})


def launch_word(paths, tmp):
    return _launch_office("word", "winword.exe", ["WORD-P01", "WORD-P10", "WORD-P40", "WORD-P60"])


def launch_excel(paths, tmp):
    return _launch_office("excel", "excel.exe", ["XL-R01", "XL-R20", "XL-R60", "张三"])


LAUNCHERS = {
    "notepad": launch_notepad, "conhost": launch_conhost, "wt": launch_wt, "explorer": launch_explorer,
    "edge": launch_edge, "chrome": launch_chrome, "vscode": launch_vscode, "vscode_sr": launch_vscode_sr,
    "word": launch_word,
    "excel": launch_excel,
}


# ---------- 测量 ----------

def _texts(items):
    return [i["text"] for i in items]


def _uniq(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def analyse(app: App, ocr: dict, ext: dict) -> dict:
    cap = ocr["cap"]
    win = list(cap.rect)
    vis = U.visible_items(ext, win, F.PASSWORD_DUMMY)
    tp_lines = U.tp_visible_lines(ext)
    ocr_texts = _texts(ocr["lines"])
    tree_vis = _uniq(_texts(vis["vis"]))
    tree_all = _uniq(_texts(vis["all"]))
    tp_vis = _texts(tp_lines)
    union = _uniq(tree_vis + tp_vis)
    content_items = [i for i in vis["vis"] if i["ct"] in U.CONTENT_CT]
    content = _uniq(_texts(content_items) + tp_vis)

    res = {
        "elements": len(ext["recs"]),
        "text_items_visible": len(tree_vis), "text_items_all": len(tree_all),
        "chars_visible": sum(len(t) for t in tree_vis), "chars_all": sum(len(t) for t in tree_all),
        "ocr_lines": len(ocr_texts), "ocr_chars": sum(len(t) for t in ocr_texts),
        "frameworks": sorted({r["fw"] for r in ext["recs"] if r["fw"]}),
        "window_name": ext["recs"][0]["name"] if ext["recs"] else "",
    }
    from collections import Counter

    res["ctypes_visible_text"] = dict(Counter(i["ct"] for i in vis["vis"]).most_common(12))
    res["textpatterns"] = [
        {"ct": t["ct"], "cls": t["cls"], "visible_chars": len(t["visible_text"]), "lines": len(t["lines"]),
         "doc_len": t["doc_len"], "error": t["error"]}
        for t in ext["tps"]
    ]
    res["password_fields"] = vis["pwd"]
    # 密码明文是否出现在任何 UIA 输出里（名称、值、TextPattern 可见文字或全文）
    blobs = [r["name"] + "\x00" + r["value"] for r in ext["recs"]]
    blobs += [t["visible_text"] or "" for t in ext["tps"]] + [t["doc_text"] or "" for t in ext["tps"]]
    res["password_plaintext_anywhere"] = any(F.PASSWORD_DUMMY in b for b in blobs)
    res["edits"] = [
        {"name": r["name"][:60], "value": r["value"][:200], "ct": r["ct"]}
        for r in ext["recs"]
        if r["ct"] in ("Edit", "ComboBox") and r["value"] and not r["pwd"]
    ][:8]

    cov = {}
    for label, tgt in (("tree", tree_vis), ("textpattern", tp_vis), ("content", content), ("union", union)):
        a = U.coverage(ocr_texts, tgt)
        b = U.coverage(tgt, ocr_texts)
        cov[label] = {
            "ocr_in_uia_line": a["line_cov"], "ocr_in_uia_char": a["char_cov"],
            "ocr_exact_of_found": a["exact_frac_of_found"],
            "uia_in_ocr_line": b["line_cov"], "uia_in_ocr_char": b["char_cov"], "uia_lines": b["lines"],
        }
        if label == "union":
            cov["_rows_ocr"] = a["rows"]
            cov["_rows_uia"] = b["rows"]
    res["coverage"] = {k: v for k, v in cov.items() if not k.startswith("_")}

    rows_ocr, rows_uia = cov["_rows_ocr"], cov["_rows_uia"]
    res["ex_ocr_differs"] = [
        {"ocr": r["src"], "uia": r["aligned"], "score": round(r["score"], 1)}
        for r in rows_ocr if 85 <= r["score"] < 100
    ][:15]
    res["ex_ocr_not_in_uia"] = [r["src"] for r in rows_ocr if r["score"] < 85][:25]
    res["ex_uia_not_in_ocr"] = [r["src"][:120] for r in rows_uia if r["score"] < 85][:25]

    origin = (cap.rect[0], cap.rect[1])
    res["ex_content_not_in_ocr"] = [
        r["src"][:120] for r in U.coverage(content, ocr_texts)["rows"] if r["score"] < 85
    ][:25]
    res["chars_tp_value_offscreen"] = sum(len(i["text"]) for i in vis["all"] if i["tp_value"])
    res["rect_align_tree"] = U.rect_alignment(ocr["lines"], vis["vis"], origin)
    res["rect_align_textpattern"] = U.rect_alignment(ocr["lines"], tp_lines, origin, key="rects")

    # 标记文字分别出现在哪
    ocr_n = U.norm("".join(ocr_texts))
    def has(texts, m):
        return U.norm(m) in U.norm("".join(texts))
    tp_doc = [t["doc_text"] or "" for t in ext["tps"]]
    res["markers"] = {
        m: {"ocr": U.norm(m) in ocr_n, "tree_visible": has(tree_vis, m), "tree_all": has(tree_all, m),
            "tp_visible": has(tp_vis, m), "tp_document": has(tp_doc, m)}
        for m in app.markers
    }
    return res


_HOME = str(Path.home())
_HOME_VARIANTS = [_HOME, _HOME.replace("\\", "/"), Path.home().as_uri()[len("file:///"):]]


def _scrub(obj):
    """提交前把本机用户目录换成 %USERPROFILE%（地址栏、标题里会带临时文件路径）。"""
    if isinstance(obj, str):
        for v in _HOME_VARIANTS:
            obj = obj.replace(v, "%USERPROFILE%")
        return obj
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    return obj


def run(app_name: str, tmp: Path, out: Path) -> dict:
    paths = F.write_all(tmp / "fixtures")
    app = LAUNCHERS[app_name](paths, tmp)
    hwnd = app.hwnd
    result = {"app": app_name, "dpi_mode": U.DPI_MODE, "notes": app.notes}
    raw = {}
    try:
        place(hwnd)
        time.sleep(3)
        result["window_class"] = U.winapi.class_name(hwnd)
        if app.stats_pids_fn:
            time.sleep(20)  # 等启动期的临时子进程退出、CPU 回落，再量"无障碍未开启"的基线
            result["browser_before"] = idle_cpu(app.stats_pids_fn, 10)

        ocr = U.capture_and_ocr(hwnd, tmp / f"{app_name}-shot.png")
        result["capture"] = {"size": ocr["size"], "method": ocr["method"], "rect": list(ocr["cap"].rect),
                             "dpi": ocr["cap"].dpi, "capture_ms": round(ocr["capture_ms"], 1),
                             "ocr_ms": round(ocr["ocr_ms"], 1)}

        # 冷启动：程序第一次被 UIA 客户端访问时，树可能还没建好。连续轻量提取，看多久才"满"
        cold = []
        tc = time.perf_counter()
        for i in range(25 if not app.notes.get("slow") else 6):
            ta = time.perf_counter()
            try:
                e = U.extract(hwnd, lines=False, doc_text=False)
            except Exception as exc:
                cold.append({"t_ms": round((time.perf_counter() - tc) * 1000),
                             "call_ms": round((time.perf_counter() - ta) * 1000, 1), "error": str(exc)[:120]})
                continue
            cold.append({"t_ms": round((time.perf_counter() - tc) * 1000), "call_ms": round(e["ms"]["total"], 1),
                         "elements": len(e["recs"]),
                         "vis_chars": sum(len(x["text"]) for x in U.visible_items(e, list(ocr["cap"].rect))["vis"])
                         + sum(len(x["visible_text"]) for x in e["tps"])})
            time.sleep(0.1)
        e = None
        result["cold_series"] = cold
        runs = []
        for i in range(7):
            runs.append(U.extract(hwnd))
            time.sleep(0.4)
        result["latency_ms"] = U.ms_summary(runs)
        result["per_run_elements"] = [len(r["recs"]) for r in runs]
        result["per_run_visible_chars"] = [
            sum(len(i["text"]) for i in U.visible_items(r, list(ocr["cap"].rect))["vis"]) for r in runs
        ]
        # 只要树、不要 TextPattern 行/全文时的耗时（最便宜的用法）
        lite = [U.extract(hwnd, lines=False, doc_text=False) for _ in range(5)]
        result["latency_lite_ms"] = {k: round(statistics.median(r["ms"][k] for r in lite), 1)
                                     for k in lite[0]["ms"]}

        final = runs[-1]
        result["analysis"] = analyse(app, ocr, final)
        raw["ocr"] = ocr["lines"]
        raw["uia"] = {"recs": final["recs"], "tps": final["tps"]}

        if app.stats_pids_fn:
            result["browser_after_uia"] = idle_cpu(app.stats_pids_fn, 10)

        # 侧面效果检查：UIA 读完后再截一张，看界面有没有弹出新提示（例如 VS Code 读屏提示）
        ocr_after = U.capture_and_ocr(hwnd, tmp / f"{app_name}-after.png")
        before_set = {U.norm(t["text"]) for t in ocr["lines"]}
        result["new_ocr_lines_after_uia"] = [t["text"] for t in ocr_after["lines"]
                                             if U.norm(t["text"]) not in before_set][:20]

        # 完全遮挡：置顶遮挡窗盖住整个窗口
        l, t, r, b = ocr["cap"].rect
        occ = subprocess.Popen([sys.executable, "-c", OCCLUDER, str(l - 30), str(t - 30), str(r - l + 60),
                                str(b - t + 60)])
        time.sleep(3.5)
        try:
            ext_occ = U.extract(hwnd)
            vis_occ = U.visible_items(ext_occ, list(ocr["cap"].rect))
            vis_final = U.visible_items(final, list(ocr["cap"].rect))
            a = {U.norm(i["text"]) for i in vis_final["vis"]}
            bset = {U.norm(i["text"]) for i in vis_occ["vis"]}
            try:
                ocr_occ = U.capture_and_ocr(hwnd, tmp / f"{app_name}-occluded.png")
                occ_texts = [x["text"] for x in ocr_occ["lines"]]
                ocr_keep = U.coverage([t["text"] for t in ocr["lines"]], occ_texts)["char_cov"]
                occ_has_occluder = any("occluder" in U.norm(x) for x in occ_texts)
            except U.winapi.CaptureError as exc:
                ocr_keep, occ_has_occluder = f"CaptureError {exc.reason}", None
            tp_occ = sum(len(x["visible_text"]) for x in ext_occ["tps"])
            tp_fin = sum(len(x["visible_text"]) for x in final["tps"])
            result["occluded"] = {
                "uia_visible_items": len(bset), "uia_visible_items_unoccluded": len(a),
                "uia_same_items_frac": round(len(a & bset) / max(1, len(a)), 3),
                "tp_visible_chars": tp_occ, "tp_visible_chars_unoccluded": tp_fin,
                "uia_ms": round(ext_occ["ms"]["total"], 1),
                "printwindow_ocr_line_keep": ocr_keep, "printwindow_shows_occluder": occ_has_occluder,
            }
        finally:
            occ.kill()
        time.sleep(1)

        # 最小化
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        time.sleep(2.5)
        try:
            ext_min = U.extract(hwnd)
            vis_min = U.visible_items(ext_min, list(ocr["cap"].rect))
            try:
                U.winapi.capture_window(hwnd)
                cap_min = "ok"
            except U.winapi.CaptureError as exc:
                cap_min = exc.reason
            result["minimized"] = {
                "elements": len(ext_min["recs"]),
                "visible_items": len(vis_min["vis"]), "all_items": len(vis_min["all"]),
                "tp_visible_chars": sum(len(x["visible_text"]) for x in ext_min["tps"]),
                "tp_doc_chars": sum((x["doc_len"] or 0) for x in ext_min["tps"]),
                "uia_ms": round(ext_min["ms"]["total"], 1), "capture": cap_min,
            }
        except Exception as exc:
            result["minimized"] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(1.5)

        if app.stats_pids_fn:
            runs = lite = final = ext_occ = ext_min = None  # 释放元素引用
            gc.collect()
            time.sleep(35)  # 观察客户端停止访问后，浏览器无障碍开销会不会自己降下来
            result["browser_idle_45s_later"] = idle_cpu(app.stats_pids_fn, 10)
    finally:
        try:
            app.close_fn()
        except Exception as exc:
            result["close_error"] = str(exc)
    (tmp / f"{app_name}-raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if app.notes.get("private_ui"):
        # Office 窗口带登录账号信息：提交的结果里不保留任何文字示例
        a = result["analysis"]
        for k in ("ex_ocr_differs", "ex_ocr_not_in_uia", "ex_uia_not_in_ocr", "ex_content_not_in_ocr"):
            if k in a:
                a[k] = f"<已省略 {len(a[k])} 条：窗口含账号信息>"
        result["new_ocr_lines_after_uia"] = f"<已省略 {len(result.get('new_ocr_lines_after_uia', []))} 条>"
        a["window_name"] = "<省略>"
        a["edits"] = "<省略>"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{app_name}.json").write_text(json.dumps(_scrub(result), ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("app", choices=sorted(LAUNCHERS))
    ap.add_argument("--tmp", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.tmp.mkdir(parents=True, exist_ok=True)
    res = run(args.app, args.tmp, args.out)
    a = res["analysis"]
    print(json.dumps({"app": args.app, "latency": res["latency_ms"]["median_rest"]["total"],
                      "first": res["latency_ms"]["first"]["total"], "coverage": a["coverage"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
