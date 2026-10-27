"""UIA 读字的真实窗口检查：只读本测试自己启动的窗口，内容全是合成文字。

- 控制台（conhost）：TextPattern 可见范围逐行读字，滚走的行不出现，矩形在窗口内、从上到下；
- Edge（临时配置目录、本地合成页面）：可见段落读到、屏外段落不出现、密码框的值不出现；
- tkinter：Tk 8.6 不提供文字，判为"给不出可用文字"，has_text_hint 随后返回 False；
- 卡死的窗口：读取在超时内返回；最小化：直接返回 minimized。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
import pytest

from screenrecall import winapi
from screenrecall.text import uia

winapi.enable_dpi_awareness()

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


def _find_hwnd(title_part: str, timeout: float = 15.0) -> int:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in winapi.list_windows():
            if title_part in w.title:
                return w.hwnd
        time.sleep(0.2)
    raise AssertionError(f"没找到窗口：{title_part}")


def _kill_tree(pid: int) -> None:
    try:
        p = psutil.Process(pid)
        procs = p.children(recursive=True) + [p]
    except psutil.NoSuchProcess:  # 已经退出（Edge 启动器把窗口交给主进程后自己就退了）
        return
    for q in procs:
        try:
            q.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(procs, timeout=5)


@pytest.fixture(scope="module", autouse=True)
def comtypes_cache(tmp_path_factory):
    """comtypes 生成的接口代码放进临时目录（每个进程只生成一次），测试不在 %LOCALAPPDATA% 留东西。"""
    cache = tmp_path_factory.mktemp("comtypes_cache")
    uia._import_uia(str(cache))
    return cache


@pytest.fixture(scope="module")
def reader(comtypes_cache):
    r = uia.UiaReader(uia.UiaConfig(timeout_s=1.5, comtypes_cache_dir=str(comtypes_cache)))
    yield r
    r.close()


def _tk_script(title: str, hang: bool) -> str:
    return f"""
import tkinter as tk, time
root = tk.Tk(); root.title({title!r}); root.geometry('700x400+80+80')
t = tk.Text(root); t.pack(fill='both', expand=True)
t.insert('end', 'SRTK-ALPHA 合成测试第一行 tkinter text\\nSRTK-BETA 第二行 second line\\n')
tk.Label(root, text='SRTK-LABEL 标签').pack()
root.update()
if {hang!r}:
    time.sleep(30)
root.mainloop()
"""


def _start_tk(tmp_path: Path, hang: bool = False):
    title = f"sr-uia-tk-{uuid.uuid4().hex[:8]}"
    script = tmp_path / f"{title}.py"
    script.write_text(_tk_script(title, hang), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)])
    return proc, _find_hwnd(title)


def _start_console(tmp_path: Path, commands: list[str]) -> subprocess.Popen:
    """用经典控制台（conhost，不经过 Windows Terminal）跑一个批处理；路径带空格也不用转义。"""
    bat = tmp_path / f"run-{uuid.uuid4().hex[:6]}.cmd"
    bat.write_text("@echo off\r\nchcp 65001>nul\r\n" + "\r\n".join(commands) + "\r\n", encoding="utf-8")
    return subprocess.Popen(["conhost.exe", "cmd.exe", "/k", str(bat)])


def test_console_lines(reader, tmp_path):
    marker = uuid.uuid4().hex[:6]
    body = tmp_path / "con.txt"
    rows = [f"SRCON-{marker}-{i:03d} 合成控制台行 console row {i}" for i in range(300)]
    body.write_text("\r\n".join(rows) + "\r\n", encoding="utf-8")
    title = f"sr-uia-con-{marker}"
    proc = _start_console(tmp_path, [f"title {title}", f'type "{body}"'])
    try:
        hwnd = _find_hwnd(title)
        time.sleep(1.0)
        res = reader.read(hwnd)
        assert res.status == "ok" and res.usable, res
        got = [ln for ln in res.lines if f"SRCON-{marker}-" in ln.text]
        assert len(got) >= 5
        nums = [int(ln.text.split("-")[2][:3]) for ln in got]
        # 最后几行在屏幕上，开头的行早已滚走、不应出现（全文范围里有，但不是可见范围）
        assert max(nums) == 299 and min(nums) > 0
        assert nums == sorted(nums)
        wr = res.window_rect
        for ln in got:
            l, t, r, b = ln.rect
            assert wr[0] <= l < r <= wr[2] and wr[1] <= t < b <= wr[3]
            assert ln.source_detail.startswith("textpattern:")
        tops = [ln.rect[1] for ln in got]
        assert tops == sorted(tops)
        assert any("合成控制台行" in ln.text for ln in got)
        print(f"console: lines={len(res.lines)} matched={len(got)} ms={res.elapsed_ms}")
    finally:
        _kill_tree(proc.pid)


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:18px sans-serif;margin:20px}} .far{{margin-top:4000px}}</style></head><body>
<h1>SRWEB-HEAD 合成网页标题</h1>
<p>SRWEB-P1 第一段可见文字 visible paragraph one</p>
<p>SRWEB-P2 第二段 second visible paragraph</p>
<label>密码 <input id="pw" type="password"></label>
<div class="far"><p>SRWEB-FAR 屏外段落 offscreen paragraph</p></div>
<script>document.getElementById('pw').value = 'SRSECRET{secret}';</script>
</body></html>"""


@pytest.mark.skipif(not EDGE.exists(), reason="本机没有 Edge")
def test_edge_visible_only(reader, tmp_path):
    marker = uuid.uuid4().hex[:6]
    title = f"sr-uia-web-{marker}"
    page = tmp_path / "page.html"
    page.write_text(_PAGE.format(title=title, secret=marker), encoding="utf-8")
    profile = Path(tempfile.mkdtemp(prefix="sr-uia-edge-"))
    proc = subprocess.Popen([
        str(EDGE), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--new-window", "--window-size=1000,800",
        "--window-position=60,60", page.as_uri(),
    ])
    try:
        hwnd = _find_hwnd(title, timeout=30)
        time.sleep(1.5)
        fresh = uia.UiaReader(uia.UiaConfig(timeout_s=1.5, comtypes_cache_dir=reader.cfg.comtypes_cache_dir))
        res = None
        for _ in range(5):  # 第一次访问浏览器只给外框；读取器自己会重试一次，这里再兜底几次
            res = fresh.read(hwnd)
            if any("SRWEB-P1" in ln.text for ln in res.lines):
                break
            time.sleep(0.5)
        fresh.close()
        texts = [ln.text for ln in res.lines]
        joined = "\n".join(texts)
        assert res.status == "ok" and res.usable
        assert "SRWEB-P1" in joined and "SRWEB-P2" in joined and "SRWEB-HEAD" in joined
        assert "SRWEB-FAR" not in joined, "屏外段落不应出现"
        assert f"SRSECRET{marker}" not in joined, "密码框的值不应出现"
        wr = res.window_rect
        p1 = next(ln for ln in res.lines if "SRWEB-P1" in ln.text)
        l, t, r, b = p1.rect
        assert wr[0] <= l < r <= wr[2] and wr[1] <= t < b <= wr[3]
        assert p1.role == "text"
        print(f"edge: lines={len(texts)} elements={res.element_count} ms={res.elapsed_ms}")
    finally:
        # 只关本测试启动、用临时配置目录的 Edge 进程
        tag = str(profile)
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if (p.info["name"] or "").lower() == "msedge.exe" and any(tag in (c or "") for c in p.info["cmdline"] or []):
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _kill_tree(proc.pid)
        for _ in range(20):
            try:
                shutil.rmtree(profile)
                break
            except OSError:
                time.sleep(0.5)


@pytest.mark.skipif(not EDGE.exists(), reason="本机没有 Edge")
def test_edge_first_touch_page_not_lost(comtypes_cache, tmp_path):
    """默认 0.5 s 预算、新浏览器进程第一次被访问：只给外框（标签、地址栏）也算 usable，
    但页面区域必须出现在 uncovered 里，调用方才会登记待 OCR，不会把这一屏的正文丢掉。"""
    marker = uuid.uuid4().hex[:6]
    title = f"sr-uia-ft-{marker}"
    paras = "".join(f"<p>SRFT-{i} 合成段落 synthetic paragraph {i}</p>" for i in range(40))
    page = tmp_path / "ft.html"
    page.write_text(f"<!doctype html><meta charset=utf-8><title>{title}</title><body>{paras}</body>", encoding="utf-8")
    profile = Path(tempfile.mkdtemp(prefix="sr-uia-edge-"))
    proc = subprocess.Popen([
        str(EDGE), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--new-window", "--window-size=1000,800",
        "--window-position=60,60", page.as_uri(),
    ])
    try:
        hwnd = _find_hwnd(title, timeout=30)
        time.sleep(2.0)
        r = uia.UiaReader(uia.UiaConfig(comtypes_cache_dir=str(comtypes_cache)))
        first = r.read(hwnd)
        later = None
        for _ in range(10):
            time.sleep(0.5)
            later = r.read(hwnd)
            if any("SRFT-" in ln.text for ln in later.lines):
                break
        r.close()
        page_lines = [ln for ln in later.lines if "SRFT-" in ln.text]
        assert page_lines, "之后的读取应读到页面正文"
        first_hits = sum(1 for ln in first.lines if "SRFT-" in ln.text)
        if first_hits == 0:
            # 第一次没读到正文：正文所在的位置必须落在 uncovered 横条里
            for ln in page_lines:
                mid = (ln.rect[1] + ln.rect[3]) // 2
                assert any(b[1] <= mid < b[3] for b in first.uncovered), (ln.rect, first.uncovered)
        # 读到正文后，正文区域不再整片算作没读到
        page_top = min(ln.rect[1] for ln in page_lines)
        page_bottom = max(ln.rect[3] for ln in page_lines)
        inside = sum(max(0, min(b[3], page_bottom) - max(b[1], page_top)) for b in later.uncovered)
        assert inside < 0.2 * (page_bottom - page_top)
        print(f"edge first touch: status={first.status} usable={first.usable} page-lines={first_hits} "
              f"uncovered={len(first.uncovered)}; later page-lines={len(page_lines)} uncovered={len(later.uncovered)}")
    finally:
        tag = str(profile)
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if (p.info["name"] or "").lower() == "msedge.exe" and any(tag in (c or "") for c in p.info["cmdline"] or []):
                    p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _kill_tree(proc.pid)
        for _ in range(20):
            try:
                shutil.rmtree(profile)
                break
            except OSError:
                time.sleep(0.5)


def test_tk_no_usable_text_and_hint(tmp_path):
    proc, hwnd = _start_tk(tmp_path)
    try:
        r = uia.UiaReader(uia.UiaConfig(timeout_s=1.5, hint_fail_streak=3))
        assert r.has_text_hint(hwnd)
        res = None
        for _ in range(3):
            res = r.read(hwnd)
        assert res.status in ("ok", "empty")
        assert not res.usable
        assert not any("SRTK-ALPHA" in ln.text for ln in res.lines)
        assert r.has_text_hint(hwnd) is False
        # 最小化：直接返回，不读
        ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        time.sleep(0.5)
        m = r.read(hwnd)
        assert m.status == "minimized" and m.lines == []
        r.close()
        print(f"tk: status={res.status} lines={len(res.lines)} chars={res.content_chars} ms={res.elapsed_ms}")
    finally:
        _kill_tree(proc.pid)


def test_hung_window_returns_within_timeout(tmp_path):
    proc, hwnd = _start_tk(tmp_path, hang=True)
    try:
        time.sleep(0.5)
        r = uia.UiaReader(uia.UiaConfig(timeout_s=0.4, connection_timeout_ms=500, transaction_timeout_ms=800,
                                        first_touch_retry_s=0))
        times, statuses = [], []
        for _ in range(3):
            t0 = time.perf_counter()
            res = r.read(hwnd)
            times.append(time.perf_counter() - t0)
            statuses.append(res.status)
            assert res.status in ("timeout", "ok", "empty", "error", "busy")
        assert max(times) < 0.4 + 0.3, times
        assert "timeout" in statuses
        # 卡死线程最终会因为 IUIAutomation2 超时退出
        end = time.monotonic() + 10
        while r.alive_abandoned() and time.monotonic() < end:
            time.sleep(0.2)
        print(f"hung: statuses={statuses}, max wait {max(times) * 1000:.0f} ms, abandoned left={r.alive_abandoned()}")
        r.close()
    finally:
        _kill_tree(proc.pid)


def test_read_visible_lines_api(tmp_path):
    marker = uuid.uuid4().hex[:6]
    title = f"sr-uia-con2-{marker}"
    proc = _start_console(tmp_path, [f"title {title}", f"echo SRAPI-{marker}-HELLO"])
    try:
        hwnd = _find_hwnd(title)
        time.sleep(0.8)
        lines = uia.read_visible_lines(hwnd, timeout_s=1.5)
        assert any(f"SRAPI-{marker}-HELLO" in ln.text for ln in lines)
    finally:
        _kill_tree(proc.pid)
        uia.default_reader().close()


if os.name != "nt":  # pragma: no cover
    pytest.skip("仅 Windows", allow_module_level=True)
