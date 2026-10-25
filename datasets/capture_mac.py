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
import hashlib
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
            "variation", "note", "source_kind"]
APP_NAME = "Chromium"   # 只写实际进程名；界面性质写 source_kind，避免 app 名带出类别答案

# 网页里常见的遮挡：cookie 同意条、促销 / 订阅弹窗。只点"拒绝 / 关闭"类按钮，不登录、不提交任何内容。
COOKIE_REJECT = ["#onetrust-reject-all-handler", "button:has-text('Reject All')", "button:has-text('Reject all')",
                 "button:has-text('Decline')", "button:has-text('Only necessary')",
                 "button:has-text('Necessary only')"]
# 促销弹窗常放在 iframe 里（如 Braze 的 srcdoc iframe），所以每个 frame 都要找
CLOSE_BUTTONS = ["button[aria-label^='Close' i]:visible", "button[aria-label^='Schließen' i]:visible",
                 ".bz-close-btn:visible", ".ab-close-button:visible",
                 "[role='dialog'] button[class*='close' i]:visible", "[role='dialog'] button:has-text('×'):visible",
                 # 地区跳转 / 订阅弹窗：选"留在当前站点"、"不用了"，不填写任何内容
                 "button:has-text('Stay on'):visible", "a:has-text('Stay on'):visible",
                 "button:has-text('No thanks'):visible", "button:has-text('Not now'):visible",
                 "[class*='modal' i] [class*='close' i]:visible", "[class*='popup' i] [class*='close' i]:visible"]
# 页面上还有遮挡层 / 未加载的图片时返回说明；没有问题返回空串
PAGE_ISSUES_JS = """() => {
  const vw = innerWidth, vh = innerHeight, out = [];
  const inView = r => r.width > 40 && r.height > 40 && r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
  for (const im of document.images) {
    const r = im.getBoundingClientRect();
    if (inView(r) && getComputedStyle(im).visibility !== 'hidden' && (!im.complete || im.naturalWidth === 0))
      out.push('图片未加载 ' + (im.currentSrc || im.src || '').slice(0, 60));
  }
  for (const el of document.querySelectorAll('body *')) {   // 盖住大半个视口的固定定位层（遮罩、全屏弹窗）
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity <= 0.1) continue;
    const r = el.getBoundingClientRect();
    if (inView(r) && r.width * r.height > vw * vh * 0.5 && (+cs.zIndex || 0) >= 100)
      out.push('全屏遮罩 ' + (el.id || el.className || el.tagName).toString().slice(0, 40));
  }
  for (const el of document.querySelectorAll('[role=dialog],[aria-modal=true],#onetrust-banner-sdk,.modal,[class*=iam-root]')) {
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (inView(r) && cs.display !== 'none' && cs.visibility !== 'hidden' && +cs.opacity > 0.1 &&
        r.width * r.height > vw * vh * 0.05)
      out.push('遮挡层 ' + (el.id || el.className || el.tagName).toString().slice(0, 40));
  }
  return out.slice(0, 5).join('；');
}"""


def clear_overlays(page, wait_s: float = 10.0) -> str:
    """在 wait_s 秒内持续守着，出现弹窗就关（cookie 条、拒绝 cookie 后几秒才弹出的促销窗）。返回做了什么。"""
    done, deadline = [], time.time() + wait_s
    while time.time() < deadline:
        hit = False
        for fr in page.frames:
            for sel in COOKIE_REJECT + CLOSE_BUTTONS:
                try:
                    loc = fr.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click(timeout=2000)
                        done.append(sel)
                        hit = True
                        time.sleep(1.0)
                        break
                except Exception:  # noqa: BLE001
                    pass
            if hit:
                break
        if not hit:
            if page.evaluate(PAGE_ISSUES_JS) and "Escape" not in done:
                page.keyboard.press("Escape")   # 多数弹窗支持 Esc 关闭；只按一次
                done.append("Escape")
                time.sleep(1.0)
                continue
            time.sleep(0.5)
    return "；点了弹窗的 " + " / ".join(done) if done else ""


def wait_loaded(page, timeout_s: float = 20.0) -> str:
    """等网络空闲、视口内图片加载完、没有遮挡层。超时返回剩余问题，交给人工判断。"""
    try:
        page.wait_for_load_state("networkidle", timeout=int(timeout_s * 1000))
    except Exception:  # noqa: BLE001
        pass
    deadline, issues = time.time() + timeout_s, ""
    while time.time() < deadline:
        issues = page.evaluate(PAGE_ISSUES_JS)
        if not issues:
            return ""
        time.sleep(1.0)
    return issues


def neutral_page_path(html_dir: Path, task_id: str, i: int) -> Path:
    """受控页面的文件名不带任务编号 / 场景名：地址栏会被截进画面、被 OCR 读到，变成类别线索。"""
    tok = hashlib.blake2b(f"{task_id}/{i}".encode(), digest_size=5).hexdigest()
    return html_dir / "p" / f"{tok}.html"


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


def run(plan: dict, out: Path, only: list[str] | None, html_dir: Path, skip_existing: bool = False) -> int:
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
                seq = fr.get("seq", i)
                if skip_existing and any(task_dir.glob(f"{task_id}_*_{seq:03d}.png")):
                    continue   # 续跑：这个编号已经截过
                w, h = fr.get("window", (1400, 860))
                cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
                cdp.send("Browser.setWindowBounds",
                         {"windowId": wid, "bounds": {"left": 20, "top": 40, "width": w, "height": h}})
                if "html" in fr:
                    hp = neutral_page_path(html_dir, task_id, i)
                    hp.parent.mkdir(parents=True, exist_ok=True)
                    hp.write_text(fr["html"], encoding="utf-8")
                    page.goto(hp.as_uri())
                    source = f"受控页面 {hp.relative_to(html_dir.parent)}"
                else:
                    try:
                        page.goto(fr["url"], wait_until=fr.get("wait_until", "load"), timeout=45000)
                    except Exception as exc:  # noqa: BLE001
                        # 没加载完的页面不截：跳过这一帧，整批继续（之前在这里接着 evaluate 会让整批崩掉）
                        print(f"  !! {task_id} #{i} 打不开 {fr['url']}，跳过这一帧: {str(exc).splitlines()[0]}",
                              file=sys.stderr)
                        failures += 1
                        continue
                    source = fr.get("source") or f"公开网站 {fr['url']}"
                zoom = fr.get("zoom", 1.0)
                if zoom != 1.0:
                    page.evaluate(f"document.documentElement.style.zoom='{zoom}'")
                dismissed = clear_overlays(page) if "url" in fr else ""
                scrolled = ""
                if fr.get("scroll"):
                    page.mouse.move(w // 2, h // 2)
                    page.mouse.wheel(0, fr["scroll"])  # 滚轮对自定义滚动容器也有效
                    time.sleep(1.5)
                    y = page.evaluate("Math.max(window.scrollY, ...[...document.querySelectorAll('*')].map(e => e.scrollTop))")
                    scrolled = f"；滚动 {int(y)}px"
                    if y < fr["scroll"] * 0.3:
                        print(f"  !! {task_id} #{i}: 滚动没生效（{y}px）", file=sys.stderr)
                    if "url" in fr:
                        dismissed += clear_overlays(page, 3.0)   # 滚动后才弹出的促销窗
                page.mouse.move(2, 2)   # 鼠标移出内容区：悬停会弹出链接状态栏、改变按钮样式
                issues = wait_loaded(page) if "url" in fr else ""
                if issues:
                    print(f"  !! {task_id} #{i}: {issues}（照截，记进 note，交人工判断）", file=sys.stderr)
                page.bring_to_front()
                time.sleep(fr.get("settle", 1.5))
                if "url" in fr:   # 截图前最后再看一次：等待期间又弹出的窗口
                    dismissed += clear_overlays(page, 2.0)
                    issues = page.evaluate(PAGE_ISSUES_JS) or issues
                win = chromium_window(page.title())
                if not win:
                    print(f"  !! {task_id} #{i}: 找不到唯一的 Chromium 窗口，跳过", file=sys.stderr)
                    failures += 1
                    continue
                now = datetime.now()
                name = f"{task_id}_{now:%Y%m%d-%H%M%S}_{fr.get('seq', i):03d}.png"   # 补采时 seq 接着已有编号
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
                    "note": "；".join(x for x in (
                        fr.get("note", "").replace("未点击", "只点过弹窗的拒绝 / 关闭按钮" if dismissed else "未点击"),
                        bad, f"截图时页面检查：{issues}" if issues else "") if x),
                    "source_kind": fr.get("source_kind", ""),
                })
                print(f"  ok {task_id} #{i:02d} {name}  [{win[1][:50]}]")
        browser.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", required=True, help="采集者根目录，如 data-dataset/raw/cao")
    ap.add_argument("--task", nargs="*", help="只跑这些任务")
    ap.add_argument("--skip-existing", action="store_true", help="续跑：跳过任务目录里已有同编号截图的帧")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    html_dir = out.parent.parent / "scenes-html"   # data-dataset/scenes-html：受控页面原样留档
    plan = load_plan(Path(a.plan))
    fails = run(plan, out, a.task, html_dir, a.skip_existing)
    print(f"完成，问题 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
