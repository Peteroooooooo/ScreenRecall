"""Mac 上的受控场景截图（数据集采集辅助，不是产品功能）。

做法：用 Playwright 打开一个**独立的、有界面的** Chromium 窗口（不碰用户自己的浏览器和登录），
把受控页面或公开网站真实显示出来，再用 macOS 自带 `screencapture -l <窗口号>` 截这个窗口。
每张图在截图那一刻记下本机时间、窗口标题、来源和画面变化，写进任务目录的 capture_log.csv，
之后由 capture_finalize.py 合进 collect manifest 生成的 manifest.csv。

不会覆盖已有文件；截出来的图做非空白检查（防黑屏 / 权限提示 / 错窗口）。

运行环境：独立的工具虚拟环境（data-dataset/.tools/venv，含 playwright + pyobjc-framework-Quartz），
与项目 .venv 分开，因为这两个包只是采集辅助，不进 requirements。

    PLAYWRIGHT_BROWSERS_PATH=data-dataset/.tools/pw data-dataset/.tools/venv/bin/python \
        datasets/capture_mac.py --plan datasets/scenes/pilot_plan.py --out data-dataset/raw/cao
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import Quartz
from PIL import Image, ImageStat
from playwright.sync_api import sync_playwright

LOG_COLS = ["file", "captured_local", "time_source", "app", "window_title", "source", "language", "facts",
            "variation", "note"]
APP_NAME = "Chromium (Playwright 独立窗口)"


def chromium_window(title: str = "", pid_hint: str = "Chromium") -> tuple[int, str, tuple[int, int, int, int]] | None:
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    found = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowLayer") == 0 and w.get("kCGWindowOwnerName") == pid_hint:
            b = w["kCGWindowBounds"]
            found.append((int(w["kCGWindowNumber"]), w.get("kCGWindowName", "") or "",
                          (int(b["X"]), int(b["Y"]), int(b["Width"]), int(b["Height"]))))
    if title:  # 优先按页面标题认窗口；网页偶尔会多出一个小的覆盖窗口
        same = [f for f in found if f[1] and (title.startswith(f[1]) or f[1].startswith(title[:30]))]
        if len(same) == 1:
            return same[0]
    big = [f for f in found if f[2][2] >= 600 and f[2][3] >= 500]
    if len(big) != 1:
        print(f"    窗口候选：{found}", file=sys.stderr)
    return big[0] if len(big) == 1 else None


def looks_blank(path: Path) -> str | None:
    with Image.open(path) as im:
        g = im.convert("L").resize((200, 120))
        st = ImageStat.Stat(g)
        if st.stddev[0] < 4:
            return f"画面几乎单色（std={st.stddev[0]:.1f}），疑似黑屏 / 空白"
        if min(im.size) < 500:
            return f"尺寸 {im.size} 太小"
    return None


def load_plan(path: Path):
    spec = importlib.util.spec_from_file_location("plan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TASKS  # {task_id: [Frame dict, ...]}


def append_log(task_dir: Path, row: dict) -> None:
    log = task_dir / "capture_log.csv"
    new = not log.exists()
    with open(log, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=LOG_COLS)
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in LOG_COLS})


def run(plan: dict, out: Path, only: list[str] | None, html_dir: Path) -> int:
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--window-position=20,40", "--window-size=1400,860",
                                                          "--lang=en-US", "--hide-crash-restore-bubble"])
        ctx = browser.new_context(no_viewport=True, locale="en-US")  # 视口跟随窗口大小
        page = ctx.new_page()
        cdp = ctx.new_cdp_session(page)
        wid = cdp.send("Browser.getWindowForTarget")["windowId"]
        for task_id, frames in plan.items():
            if only and task_id not in only:
                continue
            task_dir = out / task_id
            task_dir.mkdir(parents=True, exist_ok=True)
            for i, fr in enumerate(frames, 1):
                w, h = fr.get("window", (1400, 860))
                cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
                cdp.send("Browser.setWindowBounds",
                         {"windowId": wid, "bounds": {"left": 20, "top": 40, "width": w, "height": h}})
                if "html" in fr:
                    hp = html_dir / task_id / f"{i:03d}.html"
                    hp.parent.mkdir(parents=True, exist_ok=True)
                    hp.write_text(fr["html"], encoding="utf-8")
                    page.goto(hp.as_uri())
                    source = f"受控页面 {hp.relative_to(html_dir.parent)}"
                else:
                    try:
                        page.goto(fr["url"], wait_until="load", timeout=45000)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  !! {task_id} #{i} 打不开 {fr['url']}: {exc}", file=sys.stderr)
                    source = f"公开网站 {fr['url']}"
                zoom = fr.get("zoom", 1.0)
                if zoom != 1.0:
                    page.evaluate(f"document.documentElement.style.zoom='{zoom}'")
                dismissed = ""
                for label in fr.get("dismiss", []):   # 只点 cookie 弹窗的"拒绝"类按钮，不登录、不提交任何内容
                    btn = (page.locator(label) if label.startswith("#")
                           else page.get_by_role("button", name=label, exact=True))
                    try:
                        if btn.count() and btn.first.is_visible():
                            btn.first.click(timeout=3000)
                            dismissed = f"；点了 cookie 弹窗的 {label}"
                            time.sleep(1.0)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                scrolled = ""
                if fr.get("scroll"):
                    page.mouse.move(w // 2, h // 2)
                    page.mouse.wheel(0, fr["scroll"])  # 滚轮对自定义滚动容器也有效
                    time.sleep(1.5)
                    y = page.evaluate("Math.max(window.scrollY, ...[...document.querySelectorAll('*')].map(e => e.scrollTop))")
                    scrolled = f"；滚动 {int(y)}px"
                    if y < fr["scroll"] * 0.3:
                        print(f"  !! {task_id} #{i}: 滚动没生效（{y}px）", file=sys.stderr)
                page.bring_to_front()
                time.sleep(fr.get("settle", 1.5))
                win = chromium_window(page.title())
                if not win:
                    print(f"  !! {task_id} #{i}: 找不到唯一的 Chromium 窗口，跳过", file=sys.stderr)
                    failures += 1
                    continue
                now = datetime.now()
                name = f"{task_id}_{now:%Y%m%d-%H%M%S}_{i:03d}.png"
                dest = task_dir / name
                if dest.exists():
                    print(f"  !! {dest} 已存在，不覆盖", file=sys.stderr)
                    failures += 1
                    continue
                subprocess.run(["screencapture", "-x", "-o", f"-l{win[0]}", str(dest)], check=True)
                bad = looks_blank(dest)
                if bad:
                    print(f"  !! {name}: {bad}（文件保留，记为待剔除）", file=sys.stderr)
                    failures += 1
                variation = fr.get("variation", "") + scrolled + dismissed
                if zoom != 1.0:
                    variation += f"；CSS 缩放 {int(zoom * 100)}%"
                append_log(task_dir, {
                    "file": name, "captured_local": now.strftime("%Y-%m-%d %H:%M:%S"), "time_source": "manual",
                    "app": fr.get("app", APP_NAME), "window_title": win[1], "source": source,
                    "language": fr["language"], "facts": ";".join(fr.get("facts", [])),
                    "variation": f"窗口 {w}x{h}pt；{variation}".rstrip("；"),
                    "note": (fr.get("note", "").replace("未点击", "只点过 cookie 弹窗的拒绝按钮" if dismissed else "未点击") + ("；" + bad if bad else "")).strip("；"),
                })
                print(f"  ok {task_id} #{i:02d} {name}  [{win[1][:50]}]")
        browser.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", required=True, help="采集者根目录，如 data-dataset/raw/cao")
    ap.add_argument("--task", nargs="*", help="只跑这些任务")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    html_dir = out.parent.parent / "scenes-html"   # data-dataset/scenes-html：受控页面原样留档
    plan = load_plan(Path(a.plan))
    fails = run(plan, out, a.task, html_dir)
    print(f"完成，问题 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
