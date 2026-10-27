"""录制质量的真实窗口检查：只用本测试自己开的窗口（tkinter、临时配置目录的 Edge 打开本地合成页面）。

- Edge 的界面文字（标签页、地址栏、网址、悬停卡片）在页面加载、切换标签页、刷新的每一次读字里都不当正文；
- 刚钉住、画面一直不变的 Edge 窗口：第一屏的字马上存下（录制子进程 + --only-hwnds 隔离）；
- 文本光标那样的一条细竖线不被 OCR 成一行，表格里一个字的格子照收（真实截图 + 真实 OCR 模型）；
- 平滑翻页 / 滚轮下的位移：模板化的内容（日志、表格、头像相同的聊天、重复行很多的代码）在 tkinter 和 Edge 里
  按滚轮和翻页滚动，统计接上的比例和偏移错的比例（必须为 0），对比翻页 / 平移时提高帧率前后；
  提高帧率之后接上的至少三分之一。
只输出计数、比例和耗时；不打印屏幕文字和窗口标题。临时目录、窗口、进程都在结束时清掉。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import numpy as np
import psutil
import pytest

from screenrecall import winapi

winapi.enable_dpi_awareness()

from screenrecall.rec.core import RecorderConfig, RecorderCore  # noqa: E402
from screenrecall.rec.frames import FrameMap  # noqa: E402
from screenrecall.rec.inputmon import FakeInput  # noqa: E402
from screenrecall.rec.tracker import ListSink  # noqa: E402
from screenrecall.rec.windows import RealWindows  # noqa: E402
from screenrecall.text import uia  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
MODELS = ROOT / "models" / "ocr"
u32 = ctypes.windll.user32


# ---------------- 小工具 ----------------

def _title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _find_hwnd(sub: str, timeout: float = 30.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in winapi.list_windows():
            if sub in w.title:
                return w.hwnd
        time.sleep(0.05)
    raise AssertionError("没找到测试窗口")


def _kill_tree(pid: int) -> None:
    try:
        p = psutil.Process(pid)
        procs = p.children(recursive=True) + [p]
    except psutil.NoSuchProcess:
        return
    for q in procs:
        try:
            q.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)


def _close_edge(proc: subprocess.Popen, profile: Path) -> None:
    killed = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "msedge.exe" and any(str(profile) in (c or "")
                                                                       for c in p.info["cmdline"] or []):
                p.kill()
                killed.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(killed, timeout=10)
    _kill_tree(proc.pid)


def _rmtree(path: Path) -> None:
    for _ in range(40):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)


def _edge(profile: Path, url: str, *extra: str) -> subprocess.Popen:
    return subprocess.Popen([
        str(EDGE), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--new-window", "--window-size=900,760", "--window-position=960,40",
        "--disable-features=CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding", "--disable-popup-blocking", *extra, url])


@pytest.fixture
def work():
    base = Path(tempfile.mkdtemp(prefix="srq-win-"))
    procs: list = []
    yield base, procs
    for kind, p, extra in reversed(procs):
        try:
            if kind == "close":
                p.close()
            elif kind == "edge":
                _close_edge(p, extra)
            elif kind == "tk":
                p.close()
            else:
                _kill_tree(p.pid)
        except Exception:  # noqa: BLE001 - 收尾尽量做完
            pass
    _rmtree(base)


def _reader(base: Path) -> uia.UiaReader:
    return uia.UiaReader(uia.UiaConfig(comtypes_cache_dir=str(base / "comtypes")))


# ---------------- 1. Edge 界面文字 ----------------

def _doc_page(k: int, title: str, script: str) -> str:
    paras = "\n".join(f"<p>SRQDOC-{k}-{i:03d} 合成正文 alpha beta gamma {i * 7}</p>" for i in range(60))
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head>"
            f"<body style='font:18px sans-serif'>{paras}<script>{script}</script></body></html>")


@pytest.mark.skipif(not EDGE.exists(), reason="本机没有 Edge")
def test_edge_chrome_is_never_stored_across_loads_tab_switches_and_reloads(work):
    from screenrecall.v2.rec_text import stored_lines

    base, procs = work
    marker = uuid.uuid4().hex[:6]
    # p0 → 跳到 p1 → p1 用 window.open 开新标签页 p2（切到新标签页）→ p2 刷新一次后 window.close（切回 p1）→ p1 刷新
    (base / "p0.html").write_text(_doc_page(0, f"srq-{marker}-p0", "setTimeout(()=>location.href='p1.html', 2500);"),
                                  encoding="utf-8")
    (base / "p1.html").write_text(_doc_page(1, f"srq-{marker}-p1", (
        "if (!sessionStorage.opened) { sessionStorage.opened = 1; setTimeout(()=>window.open('p2.html'), 2500); }"
        " else if (!sessionStorage.reloaded) { sessionStorage.reloaded = 1; setTimeout(()=>location.reload(), 2500); }"
    )), encoding="utf-8")
    (base / "p2.html").write_text(_doc_page(2, f"srq-{marker}-p2", (
        "if (!sessionStorage.r) { sessionStorage.r = 1; setTimeout(()=>location.reload(), 2000); }"
        " else { setTimeout(()=>{ window.close(); }, 2000); }"
    )), encoding="utf-8")
    profile = base / "profile"
    edge = _edge(profile, (base / "p0.html").as_uri())
    procs.append(("edge", edge, profile))
    hwnd = _find_hwnd(f"srq-{marker}")
    reader = _reader(base)
    procs.append(("close", reader, None))
    w32 = uia._win32()
    reads = chrome = doc = raw_doc = ui_doc = 0
    hover_reads = hover_chrome = 0
    pages_seen: set[str] = set()
    hover = {"on": False}

    def do_hover():
        # 光标移到自己窗口的标签页上停一会儿（出悬停卡片），之后放回原处；窗口临时置顶，保证悬停的是自己的窗口
        time.sleep(4.0)
        old = ctypes.wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(old))
        u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0013)
        try:
            fr = w32.frame_rect(hwnd)
            u32.SetCursorPos(fr[0] + 200, fr[1] + 30)
            hover["on"] = True
            time.sleep(2.5)
        finally:
            hover["on"] = False
            u32.SetCursorPos(old.x, old.y)
            u32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0013)

    import ctypes.wintypes  # noqa: F401

    th = threading.Thread(target=do_hover, daemon=True)
    th.start()
    end = time.monotonic() + 16.0
    while time.monotonic() < end:
        res = reader.read(hwnd)
        if res.status in ("ok", "empty") and res.window_rect is not None:
            reads += 1
            wr = res.window_rect
            fmap = FrameMap.from_bounds(wr, wr[2] - wr[0], wr[3] - wr[1])
            conv, _ = stored_lines(res, fmap)
            bad = sum(1 for text, _, _ in conv if "SRQDOC" not in text)
            chrome += bad
            doc += sum(1 for text, _, _ in conv if "SRQDOC" in text)
            raw_doc += sum(1 for ln in res.lines if "SRQDOC" in ln.text and ln.source_detail.startswith("textpattern"))
            ui_doc += sum(1 for ln in res.lines if "SRQDOC" in ln.text and ln.role != "text")
            pages_seen |= {text.split("-")[1] for text, _, _ in conv if text.startswith("SRQDOC")}
            if hover["on"]:
                hover_reads += 1
                hover_chrome += bad
        time.sleep(0.05)
    th.join(5)
    print(f"\nEdge 读字 {reads} 次（悬停时 {hover_reads} 次），存成正文的界面文字 {chrome} 行（悬停时 {hover_chrome}），"
          f"正文行 {doc}（TextPattern 给的 {raw_doc}，被当成界面的 {ui_doc}），看到的页面 {sorted(pages_seen)}")
    assert reads >= 40 and hover_reads >= 3
    assert chrome == 0
    assert ui_doc == 0 and doc >= raw_doc > 0  # 正文行一行都没少
    assert pages_seen == {"0", "1", "2"}


# ---------------- 2. 刚钉住、一直不变的 Edge 窗口 ----------------

def _recorder(home: Path, data: Path, hwnds: list[int], out) -> subprocess.Popen:
    env = dict(os.environ)
    env["SCREENRECALL_HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    env.pop("SCREENRECALL_DATA_DIR", None)
    env.pop("SCREENRECALL_TEST_ONLY_HWNDS", None)
    return subprocess.Popen([sys.executable, "-m", "screenrecall.v2.recorder", "--data-dir", str(data), "--home",
                             str(home), "--only-hwnds", ",".join(str(h) for h in hwnds)],
                            stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT), env=env)


@pytest.mark.skipif(not EDGE.exists(), reason="本机没有 Edge")
def test_pinned_static_edge_window_first_screen_is_stored(work):
    base, procs = work
    marker = uuid.uuid4().hex[:6]
    (base / "s.html").write_text(_doc_page(7, f"srq-{marker}-static", ""), encoding="utf-8")
    profile = base / "profile"
    edge = _edge(profile, (base / "s.html").as_uri())
    procs.append(("edge", edge, profile))
    hwnd = _find_hwnd(f"srq-{marker}")
    time.sleep(4.0)  # 页面早就加载完了；之前没有任何程序用 UIA 碰过它
    home, data = base / "home", base / "data"
    from screenrecall.store.writer import Writer

    proc_name = winapi.describe_window(hwnd).process
    with Writer(data) as w:  # 录制进程一起来就钉住它（它刚打开，也可能正在前台：两条路都要马上读第一屏）
        w.control_set(desired_state="recording",
                      pinned_windows=[{"hwnd": hwnd, "process": proc_name, "title": _title(hwnd)}]).result(10)
    out = open(base / "recorder.out", "wb")
    procs.append(("close", out, None))
    t_pin = time.monotonic()
    rec = _recorder(home, data, [hwnd], out)
    procs.append(("rec", rec, None))
    db = data / "recall.sqlite3"
    got_at = None
    n_doc = 0
    while time.monotonic() - t_pin < 10.0:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            n_doc = c.execute("SELECT COUNT(*) FROM lines WHERE text LIKE 'SRQDOC-7-%'").fetchone()[0]
        except sqlite3.Error:
            n_doc = 0
        finally:
            c.close()
        if n_doc >= 10 and got_at is None:
            got_at = time.monotonic() - t_pin
        time.sleep(0.25)
    rec.stdin.close()
    assert rec.wait(10) == 0
    out.close()
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        other = c.execute("SELECT COUNT(*) FROM lines WHERE text NOT LIKE 'SRQDOC-%'").fetchone()[0]
        segs = [r[0] for r in c.execute("SELECT title FROM segments")]
    finally:
        c.close()
    print(f"\n录制进程启动（已钉住）后 {got_at if got_at is None else round(got_at, 2)} 秒存下第一屏的正文"
          f"（{n_doc} 行），界面文字 {other} 行")
    assert all(marker in t for t in segs)
    assert got_at is not None and got_at < 6.0  # 含录制进程启动约 1.5 秒
    assert other == 0


# ---------------- 3. 文本光标与一个字的格子（真实截图 + 真实 OCR） ----------------

@pytest.mark.skipif(not (MODELS / "PP-OCRv6_det_tiny.onnx").exists(), reason="没有 OCR 模型")
def test_caret_bar_is_not_an_ocr_line_but_single_digit_cells_are(work):
    import tkinter as tk

    from screenrecall.rec.wgc import CaptureSession
    from screenrecall.text.ocr2 import OcrEngine
    from screenrecall.v2.work_ocr import glyph_noise

    root = tk.Tk()
    root.title("srq-caret-check")
    root.geometry("900x420+40+40")
    cv = tk.Canvas(root, bg="white", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    font = ("Microsoft YaHei UI", 14)
    cv.create_text(30, 20, anchor="nw", font=font, text="合成的一行字 synthetic line")
    cv.create_rectangle(32, 70, 33, 100, fill="black", outline="")  # 空行上的光标（2 像素宽）
    cv.create_text(30, 120, anchor="nw", font=font, text="abc")
    cv.create_rectangle(400, 120, 401, 150, fill="black", outline="")  # 行尾后面的光标
    digits = ["1", "7", "0", "9", "4", "中"]
    for i, d in enumerate(digits):
        x = 40 + i * 90
        cv.create_rectangle(x - 10, 200, x + 50, 250, outline="#b0b0b0")
        cv.create_text(x + 10, 208, anchor="nw", font=font, text=d)
    root.update()
    time.sleep(0.3)
    root.update()
    hwnd = int(u32.GetAncestor(cv.winfo_id(), 2))

    class _Tk:
        def close(self):
            root.destroy()

    procs = work[1]
    procs.append(("tk", _Tk(), None))
    src = CaptureSession(hwnd, max_fps=10)
    src.start()
    try:
        f = None
        end = time.monotonic() + 3
        while f is None and time.monotonic() < end:
            root.update()
            f = src.take()
            time.sleep(0.02)
    finally:
        src.stop()
    assert f is not None
    img = f.bgr
    cap = uia.caption_height(hwnd)
    crop = img[cap:]
    carets = [(33, 85), (401, 135)]  # 画布坐标 = 客户区物理像素
    kept, dropped = {}, {}
    for model in ("tiny", "small"):
        eng = OcrEngine(model, models_dir=MODELS)
        for ln in eng.recognize(crop, crop=True):
            text = ln.text.strip()
            box = (ln.x0, ln.y0, ln.x1, ln.y1)
            cx, cy = (ln.x0 + ln.x1) / 2, (ln.y0 + ln.y1) / 2
            at_caret = len(text) == 1 and any(abs(cx - x) < 20 and abs(cy - y) < 20 for x, y in carets)
            target = dropped if glyph_noise(text, ln.confidence, crop, box) else kept
            target.setdefault(model, []).append((text, at_caret))
    one = {m: sorted(t for t, _ in v if len(t) == 1) for m, v in kept.items()}
    print(f"\n保留的单字行 {one}；丢掉的噪声行 { {m: len(v) for m, v in dropped.items()} }（其中光标 "
          f"{ {m: sum(1 for _, c in v if c) for m, v in dropped.items()} }）")
    for model in ("tiny", "small"):
        assert not [t for t, c in kept.get(model, []) if c], model  # 光标没有变成一行
        assert not {t for t, _ in dropped.get(model, [])} & set(digits), model  # 识别出来的数字一个都没丢
    assert {"1", "7", "0", "9", "4"} <= set(one.get("small", []))
    assert len({"1", "7", "0", "9", "4"} & set(one.get("tiny", []))) >= 3


# ---------------- 4. 模板化内容的平滑翻页 / 滚轮 ----------------

class _OnlyMine(RealWindows):
    """前台永远是测试窗口（绝不去截用户的窗口），滚轮都落在它上面。"""

    def __init__(self, hwnd: int):
        super().__init__()
        self.mine = hwnd

    def foreground(self) -> int:
        return self.mine

    def root_at(self, x: int, y: int) -> int:
        return self.mine


def _templated_html(kind: str, n: int) -> str:
    if kind == "log":
        rows = [f"2026-10-27 10:{(i // 60) % 60:02d}:{i % 60:02d}.{(i * 37) % 1000:03d} INFO  [worker-{i % 4}] "
                f"GET /api/items?page={i % 9} 200 {12 + i % 7} ms" for i in range(n)]
        return "<pre style='font:15px Consolas,monospace;line-height:22px;margin:12px'>" + "\n".join(rows) + "</pre>"
    if kind == "table":
        tr = "".join(f"<tr><td>{i % 50}</td><td>合成条目 Item</td><td>OK</td><td>2026-10-27</td><td>{(i * 7) % 10}</td></tr>"
                     for i in range(n))
        return ("<table style='border-collapse:collapse;font:15px sans-serif;margin:12px' border=1 cellpadding=6>"
                + tr + "</table>")
    if kind == "chat":
        msgs = ["ok", "收到", "好的，我看一下", "明天再说", "ok"]
        out = []
        for i in range(n):
            side = "left" if i % 2 == 0 else "right"
            out.append(f"<div style='display:flex;flex-direction:{'row' if side == 'left' else 'row-reverse'};"
                       f"margin:10px'><div style='width:36px;height:36px;border-radius:18px;background:#4a8;color:#fff;"
                       f"text-align:center;line-height:36px'>A</div><div style='background:#eee;border-radius:8px;"
                       f"padding:8px 12px;margin:0 10px;font:15px sans-serif'>{msgs[i % len(msgs)]}</div></div>")
        return "".join(out)
    # code：重复行很多（每个函数都一样，只有名字里的数字不同）
    lines = []
    for i in range(n // 6):
        lines += [f"function handler{i % 7}(req) {{", "  if (!req) {", "    return null;", "  }",
                  "  return process(req);", "}"]
    body = "\n".join(f"{k + 1:5d}  {t}" for k, t in enumerate(lines))
    return "<pre style='font:15px Consolas,monospace;line-height:21px;margin:12px'>" + body + "</pre>"


_SCROLL_JS = """
const T = "%s"; const steps = %s; let k = 0;
function settle(cb){ let last = -1, same = 0; const iv = setInterval(()=>{
  if (scrollY === last) { if (++same >= 8) { clearInterval(iv); cb(); } } else { same = 0; last = scrollY; } }, 30); }
function next(){
  if (k >= steps.length) { document.title = T + " done"; return; }
  const kind = steps[k];
  document.title = T + " s" + k + " " + kind;
  setTimeout(()=>{
    if (kind === "pd") scrollBy({top: Math.round(innerHeight * 0.875), behavior: "smooth"});
    else scrollBy({top: 160, behavior: "smooth"});
    settle(()=>{ document.title = T + " e" + k + " y=" + Math.round(scrollY * devicePixelRatio); k++;
                 setTimeout(next, 1800); });
  }, 40);
}
setTimeout(next, 3500);
"""


def _steps(n: int) -> list[str]:
    return ["pd" if i % 3 != 2 else "wheel" for i in range(n)]


def _score(stills: list, steps: list[tuple[str, int, int, int]]) -> dict:
    """steps：[(种类, 真实位置 px, 这一步结束的时间, 下一步开始的时间)]，第 0 项是开始前（种类 ''）。
    每一步取它结束之后、下一步开始之前的最后一次静止，看块和位置。"""
    res = {"pd": [0, 0, 0, 0], "wheel": [0, 0, 0, 0]}  # 接上、偏移错、接不上、没有静止
    prev = None
    for kind, y, t_end, t_next in steps:
        st = [s for s in stills if t_end <= s.t_ms < t_next]
        cur = (st[-1].block, st[-1].pos_y, y) if st else None
        if kind and prev is not None:
            r = res[kind]
            if cur is None:
                r[3] += 1
            elif cur[0] != prev[0]:
                r[2] += 1
            elif abs((cur[1] - prev[1]) - (cur[2] - prev[2])) <= 3:
                r[0] += 1
            else:
                r[1] += 1
        if cur is not None:
            prev = cur
    return res


def _run_edge_page(base: Path, procs: list, kind: str, cfg: RecorderConfig, n_steps: int) -> tuple[dict, float]:
    marker = uuid.uuid4().hex[:6]
    title = f"srq-{marker}"
    steps = _steps(n_steps)
    html = (f"<!doctype html><html><head><meta charset=utf-8><title>{title} ready</title></head>"
            f"<body style='margin:0'>{_templated_html(kind, 900)}"
            f"<script>{_SCROLL_JS % (title, json.dumps(steps))}</script></body></html>")
    page = base / f"{kind}-{marker}.html"
    page.write_text(html, encoding="utf-8")
    profile = base / f"profile-{marker}"
    edge = _edge(profile, page.as_uri(), "--enable-smooth-scrolling")
    procs.append(("edge", edge, profile))
    hwnd = _find_hwnd(title)
    sink, inp = ListSink(), FakeInput()
    core = RecorderCore(sink, cfg, windows=_OnlyMine(hwnd), inputs=inp, watcher=None)
    me = psutil.Process()
    core.start()
    c0 = me.cpu_times()
    w0 = time.perf_counter()
    marks: list[tuple[str, int, int]] = [("", 0, core.clock())]  # (种类, 位置, 结束时间)
    starts: list[int] = []
    last = ""
    fr = uia._win32().frame_rect(hwnd)
    try:
        deadline = time.monotonic() + 8 + n_steps * 4.5
        while time.monotonic() < deadline:
            t = _title(hwnd)
            if t != last:
                last = t
                now = core.clock()
                rest = t[len(title):].split()
                if rest and rest[0].startswith("s") and rest[0][1:].isdigit():
                    starts.append(now)
                    if rest[1] == "pd":
                        inp.key(now, nav=True)
                    else:
                        inp.wheel(now, -120, (fr[0] + fr[2]) // 2, (fr[1] + fr[3]) // 2)
                elif rest and rest[0].startswith("e") and rest[1].startswith("y="):
                    marks.append((steps[int(rest[0][1:])], int(rest[1][2:]), now))
                elif rest and rest[0] == "done":
                    time.sleep(1.5)
                    break
            time.sleep(0.01)
    finally:
        core.stop()
        c1 = me.cpu_times()
        wall = time.perf_counter() - w0
        _close_edge(edge, profile)
    cpu = (c1.user + c1.system - c0.user - c0.system) / wall
    stills = [e["ev"] for e in sink.of("still")]
    nexts = starts[1:] + [10**15]
    # 开始前那一项：结束时间 = 记录开始，下一项开始 = 第一步开始
    steps_t = [(marks[0][0], marks[0][1], marks[0][2], starts[0] if starts else 10**15)]
    for i, (k, y, t_end) in enumerate(marks[1:]):
        steps_t.append((k, y, t_end, nexts[i] if i < len(nexts) else 10**15))
    assert len(marks) - 1 == n_steps, f"{kind}: 页面只走了 {len(marks) - 1} 步"
    return _score(stills, steps_t), cpu


def _merge(acc: dict, res: dict) -> None:
    for k, v in res.items():
        a = acc.setdefault(k, [0, 0, 0, 0])
        for i in range(4):
            a[i] += v[i]


@pytest.mark.skipif(not EDGE.exists(), reason="本机没有 Edge")
def test_templated_content_smooth_page_down_and_wheel_in_edge(work):
    base, procs = work
    before = replace(RecorderConfig(), nav_boost_ms=0, shift_boost_ms=0)
    after = RecorderConfig()
    out = {}
    for name, cfg in (("before", before), ("after", after)):
        acc: dict = {}
        cpus = []
        for kind in ("log", "table", "chat", "code"):
            res, cpu = _run_edge_page(base, procs, kind, cfg, 9)
            _merge(acc, res)
            cpus.append(cpu)
            print(f"\n{name} {kind}: {res} cpu {cpu * 100:.1f}% of one core")
        out[name] = (acc, sum(cpus) / len(cpus))
    for name, (acc, cpu) in out.items():
        for k, (ok, wrong, broke, miss) in acc.items():
            n = ok + wrong + broke + miss
            print(f"{name} {k}: steps {n}, joined {ok / max(n, 1) * 100:.0f}%, wrong offset {wrong}, "
                  f"unjoined {broke}, no still {miss}; cpu {cpu * 100:.1f}% of one core")
    for name, (acc, _) in out.items():
        assert all(v[1] == 0 for v in acc.values()), f"{name}: 有偏移错的步"
    assert out["after"][0]["pd"][0] >= out["before"][0]["pd"][0]
    # 整屏复核之后（2026-10-27 实测）：日志、表格、代码大多接得上；完全周期的聊天页一律接不上（没法知道滚了多少）
    for k, (ok, wrong, broke, miss) in out["after"][0].items():
        assert ok * 3 >= ok + wrong + broke + miss, f"after {k}: 接上的不到三分之一"


class _TkTemplated:
    def __init__(self, kind: str, w: int = 900, h: int = 700):
        import tkinter as tk

        self.root = tk.Tk()
        self.root.title(f"srq-tk-{kind}")
        self.root.geometry(f"{w}x{h}+40+40")
        self.cv = tk.Canvas(self.root, bg="white", highlightthickness=0, yscrollincrement=1)
        self.cv.pack(fill="both", expand=True)
        mono, sans = ("Consolas", 12), ("Microsoft YaHei UI", 11)
        y = 10
        n = 700
        for i in range(n):
            if kind == "log":
                self.cv.create_text(12, y, anchor="nw", font=mono, text=(
                    f"2026-10-27 10:{(i // 60) % 60:02d}:{i % 60:02d}.{(i * 37) % 1000:03d} INFO  [worker-{i % 4}] "
                    f"GET /api/items?page={i % 9} 200 {12 + i % 7} ms"))
                y += 24
            elif kind == "table":
                for j, (x, t) in enumerate(((12, f"{i % 50}"), (80, "合成条目 Item"), (300, "OK"), (380, "2026-10-27"),
                                            (520, f"{(i * 7) % 10}"))):
                    self.cv.create_rectangle(x - 6, y - 4, x + (62 if j == 0 else 214 if j == 1 else 74 if j == 2
                                                               else 134 if j == 3 else 60), y + 26, outline="#999")
                    self.cv.create_text(x, y, anchor="nw", font=sans, text=t)
                y += 30
            elif kind == "chat":
                msgs = ["ok", "收到", "好的，我看一下", "明天再说", "ok"]
                left = i % 2 == 0
                ax = 14 if left else w - 54
                self.cv.create_oval(ax, y, ax + 36, y + 36, fill="#4a8", outline="")
                self.cv.create_text(ax + 18, y + 18, font=sans, text="A", fill="white")
                tx = 62 if left else w - 260
                self.cv.create_rectangle(tx, y + 2, tx + 190, y + 34, fill="#eee", outline="")
                self.cv.create_text(tx + 10, y + 8, anchor="nw", font=sans, text=msgs[i % len(msgs)])
                y += 48
            else:
                rows = [f"function handler{(i // 6) % 7}(req) {{", "  if (!req) {", "    return null;", "  }",
                        "  return process(req);", "}"]
                self.cv.create_text(12, y, anchor="nw", font=mono, text=f"{i + 1:5d}  {rows[i % 6]}")
                y += 22
        self.cv.configure(scrollregion=(0, 0, w, y + 40))
        self.root.update()
        time.sleep(0.3)
        self.root.update()
        self.hwnd = int(u32.GetAncestor(self.cv.winfo_id(), 2))
        self.view_h = self.cv.winfo_height()

    def pump(self, seconds: float) -> None:
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            self.root.update()
            time.sleep(0.005)

    def top(self) -> int:
        return int(self.cv.canvasy(0))

    def scroll(self, px: int) -> None:
        self.cv.yview_scroll(px, "units")
        self.root.update()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _run_tk(kind: str, cfg: RecorderConfig, n_steps: int, procs: list) -> dict:
    d = _TkTemplated(kind)
    procs.append(("tk", d, None))
    sink, inp = ListSink(), FakeInput()
    core = RecorderCore(sink, cfg, windows=_OnlyMine(d.hwnd), inputs=inp, watcher=None)
    core.start()
    marks = []
    try:
        t_start = core.clock()
        d.pump(2.0)
        marks.append(("", d.top(), t_start))
        starts = []
        for i, kind_s in enumerate(_steps(n_steps)):
            t = core.clock()
            starts.append(t)
            if kind_s == "pd":
                inp.key(t, nav=True)
                total = int(d.view_h * 0.875)
                # 平滑翻页：先快后慢，约 300 ms 走完
                done = 0
                for f in (0.25, 0.5, 0.7, 0.85, 0.94, 1.0):
                    step = int(total * f) - done
                    done += step
                    d.scroll(step)
                    d.pump(0.05)
            else:
                for _ in range(3):
                    inp.wheel(core.clock(), -120)
                    d.scroll(40)
                    d.pump(0.04)
            t_end = core.clock()
            d.pump(2.2)
            marks.append((kind_s, d.top(), t_end))
        d.pump(0.5)
    finally:
        core.stop()
        d.close()
    stills = [e["ev"] for e in sink.of("still")]
    nexts = starts + [10**15]
    steps_t = [(k, y, t_end, nexts[i]) for i, (k, y, t_end) in enumerate(marks)]
    return _score(stills, steps_t)


def test_templated_content_smooth_page_down_and_wheel_in_tk(work):
    _, procs = work
    before = replace(RecorderConfig(), nav_boost_ms=0, shift_boost_ms=0)
    after = RecorderConfig()
    out = {}
    for name, cfg in (("before", before), ("after", after)):
        acc: dict = {}
        for kind in ("log", "table", "chat", "code"):
            res = _run_tk(kind, cfg, 9, procs)
            _merge(acc, res)
            print(f"\n{name} tk {kind}: {res}")
        out[name] = acc
    for name, acc in out.items():
        for k, (ok, wrong, broke, miss) in acc.items():
            n = ok + wrong + broke + miss
            print(f"{name} tk {k}: steps {n}, joined {ok / max(n, 1) * 100:.0f}%, wrong offset {wrong}, "
                  f"unjoined {broke}, no still {miss}")
    for name, acc in out.items():
        assert all(v[1] == 0 for v in acc.values()), f"{name}: 有偏移错的步"
    assert out["after"]["pd"][0] >= out["before"]["pd"][0]
    for k, (ok, wrong, broke, miss) in out["after"].items():
        assert ok * 3 >= ok + wrong + broke + miss, f"after tk {k}: 接上的不到三分之一"
