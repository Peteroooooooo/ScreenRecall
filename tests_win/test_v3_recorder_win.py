"""v3 录制子进程的真实窗口检查：把 python -m screenrecall.v3.recorder 当子进程跑，只录本测试自己开的 Tk 窗口。

- 临时 home / 数据目录；先用 Store 建库，把 python.exe 设成"一直录"（Tk 窗口在后台也录，不用抢前台）；
- 录制进程带 --only-pids <Tk 的进程号>（测试隔离：别的窗口一律当成没有）、--no-ocr、--exit-after-s；
- Tk 窗口每 3 秒换一屏字；跑完看库里的次数，只报数量；关掉自己开的 Tk 窗口，删临时目录。
单独运行：python -m pytest tests_win\\test_v3_recorder_win.py -q -p no:cacheprovider
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("仅 Windows", allow_module_level=True)

ROOT = Path(__file__).resolve().parent.parent
RUN_S = 14.0

_TK = r"""
import ctypes, sys, tkinter as tk
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
root = tk.Tk(); root.title(sys.argv[1]); root.geometry("700x500+60+60")
cv = tk.Canvas(root, bg="white", highlightthickness=0); cv.pack(fill="both", expand=True)
def page(n=[0]):
    cv.delete("all")
    for i in range(12):
        cv.create_text(20, 16 + i * 36, anchor="nw", font=("Microsoft YaHei UI", 14),
                       text=f"SRV3TK 第 {n[0]} 屏 第 {i} 行 screen recall test line")
    n[0] += 1
    if n[0] < 4:
        root.after(3000, page)
page()
root.after(int(float(sys.argv[2]) * 1000), root.destroy)
root.mainloop()
"""


def _find_window(title: str, timeout: float = 20.0):
    from screenrecall import winapi

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for w in winapi.list_windows():
            if w.title == title:
                return w
        time.sleep(0.2)
    raise AssertionError("没找到自己开的 Tk 窗口")


def _rmtree(path: Path) -> None:
    for _ in range(40):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.5)


def test_recorder_records_only_own_tk_window():
    from screenrecall.v3.config import build
    from screenrecall.v3.store import Store

    base = Path(tempfile.mkdtemp(prefix="sr-v3rec-win-"))
    tk_proc = rec = win = None
    try:
        data, home = base / "data", base / "home"
        data.mkdir()
        home.mkdir()
        cfg = build({}, data_dir=data, home=home)
        st = Store(cfg)
        st.init_defaults(cfg.default_record_apps)
        st.set_app_tier(Path(sys.executable).name, "always")
        st.close()

        script = base / "tk_page.py"
        script.write_text(_TK, encoding="utf-8")
        title = f"SRV3 录制测试窗口 {base.name}"
        tk_proc = subprocess.Popen([sys.executable, str(script), title, str(RUN_S + 6)])
        # venv 的 python.exe 只是启动器，窗口在它起的真解释器进程里：按自己起的唯一标题找到窗口的进程号
        win = _find_window(title)
        cmd = [sys.executable, "-m", "screenrecall.v3.recorder", "--data-dir", str(data), "--home", str(home),
               "--only-pids", str(win.pid), "--no-ocr", "--exit-after-s", str(RUN_S)]
        t0 = time.monotonic()
        errf = base / "recorder.err"
        with open(errf, "wb") as err:
            # stdin 管道一直开着（关掉 = 让它退出）；到 --exit-after-s 自己收尾
            rec = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            rec.wait(timeout=RUN_S + 30)
            rec.stdin.close()
        took = time.monotonic() - t0
        assert rec.returncode == 0, errf.read_text(encoding="utf-8", errors="replace")[-2000:]
        log = (home / "logs" / "recorder.log").read_text(encoding="utf-8", errors="replace")
        assert "recorder stats final" in log

        c = sqlite3.connect(cfg.db_path)
        try:
            n = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("sources", "visits", "snaps", "shots", "lines", "grey_spans", "clips")}
            bg = c.execute("SELECT COUNT(*) FROM visits WHERE background = 1").fetchone()[0]
            apps = {r[0].lower() for r in c.execute("SELECT DISTINCT app FROM sources")}
            state = c.execute("SELECT recorder_state FROM control WHERE id = 1").fetchone()[0]
            open_visits = c.execute("SELECT COUNT(*) FROM visits WHERE open_since_ms IS NOT NULL").fetchone()[0]
        finally:
            c.close()
        print(f"\n录制 {took:.1f} 秒：{n}，后台条目 {bg}")
        assert n["snaps"] >= 1 and bg >= 1, log[-3000:]
        assert apps <= {Path(sys.executable).name.lower()}  # 只录到了自己开的窗口
        assert n["clips"] == 0
        assert state == "stopped" and open_visits == 0
    finally:
        if rec is not None and rec.poll() is None:
            rec.kill()
        if win is not None:  # 真正开窗口的解释器进程（自己起的）
            import psutil

            try:
                psutil.Process(win.pid).kill()
            except psutil.NoSuchProcess:
                pass
        if tk_proc is not None and tk_proc.poll() is None:
            tk_proc.kill()
            tk_proc.wait(5)
        _rmtree(base)
