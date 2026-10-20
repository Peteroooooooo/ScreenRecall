"""聊天场景：在真实 Element Web 客户端里展示合成对话并按窗口截图（cao-chat-01..10）。

服务端是本机 Matrix 测试服务器（Synapse，只监听 127.0.0.1:18008，不联邦），客户端是官方 Element Web
发行包（127.0.0.1:18080 静态服务）。所有账号、对话都是 chat_content*.py 里的合成内容，只发在本机服务器，
不会发给任何真人。每个任务用一套独立账号，侧栏只出现本任务的房间。

截图节奏（每任务 25 张，每张都有内容变化）：
  主房间 14 张：消息逐条 / 两条地发出，第 8 张起切到深色主题；
  第二个房间（私聊或另一频道）若干张：消息逐条增加；
  其余：主房间往上翻看更早的消息（窄窗口、浅色），每次多翻一段。

用法（先启动 Synapse 与 Element，见 COLLECTION.md 第 9 节）：
  PLAYWRIGHT_BROWSERS_PATH=data-dataset/.tools/pw data-dataset/.tools/venv/bin/python \\
      datasets/capture_chat_element.py --out data-dataset/raw/cao --task cao-chat-01
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scenes"))

from capture_mac import append_log, chromium_window, looks_blank  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

import chat_content_b  # noqa: E402,F401  （把 06..10 合进 TASKS）
from chat_content import TASKS  # noqa: E402

ROOT = HERE.parent
MX = ROOT / "data-dataset" / ".tools" / "matrix"
HS = "http://127.0.0.1:18008"
EL = "http://127.0.0.1:18080"
SERVER = "course.test"
ELEMENT_VER = "1.12.32"
FRAMES = 25

# 事实：(fact_id, task, kind, 正文, 语言, 说明) —— 与其他计划文件同格式，capture_finalize --plan 读取
FACTS = [
    ("cao-chat-01#1", "cao-chat-01", "link", "API Reference v2.3 — https://docs.example.com/screenrecall/api/v2.3", "en", "合成对话"),
    ("cao-chat-01#2", "cao-chat-01", "date", "Deadline: Friday 14 November 2026, 23:59 SGT", "en", "合成对话"),
    ("cao-chat-02#1", "cao-chat-02", "place", "川味小馆，示例路 88 号 2 楼（虚构），人均 25 新币", "zh", "合成对话"),
    ("cao-chat-03#1", "cao-chat-03", "paper", "Sigmoid Loss for Language Image Pre-Training, arXiv:2303.15343", "en", "合成对话"),
    ("cao-chat-04#1", "cao-chat-04", "bug", "BUG-1042, error code E_DECODE_EMPTY", "en", "合成对话"),
    ("cao-chat-05#1", "cao-chat-05", "meeting", "组会 10月23日（周四）14:00，会议室 E4-04-02（由 E4-03-06 改）", "zh", "合成对话"),
    ("cao-chat-06#1", "cao-chat-06", "flight", "SQ 346 Singapore → Zurich 23:55 12 Dec; LX 1308", "en", "合成对话"),
    ("cao-chat-06#2", "cao-chat-06", "hotel", "Hotel Northern Lantern (fictional), 4 nights", "en", "合成对话"),
    ("cao-chat-07#1", "cao-chat-07", "file", "screens-dev-manifest.csv；提取码 m9x4（示例）", "mixed", "合成对话"),
    ("cao-chat-08#1", "cao-chat-08", "conclusion", "Conclusion: RRF k=60 after the quote filter; no weighted sum.", "en", "合成对话"),
    ("cao-chat-09#1", "cao-chat-09", "deadline", "作业二截止 11月7日（周五）23:59；Cao 负责建表 SQL", "zh", "合成对话"),
    ("cao-chat-10#1", "cao-chat-10", "qa", "Q: retention period for raw screenshots? A: 30 days for images, 90 days for text", "en", "合成对话"),
]
# 判断事实是否在画面里：页面上出现这些字样且在视口内
FACT_KEYS = {
    "cao-chat-01#1": "API Reference v2.3", "cao-chat-01#2": "Deadline: Friday 14 November",
    "cao-chat-02#1": "川味小馆", "cao-chat-03#1": "2303.15343", "cao-chat-04#1": "E_DECODE_EMPTY",
    "cao-chat-05#1": "E4-04-02", "cao-chat-06#1": "SQ 346", "cao-chat-06#2": "Northern Lantern",
    "cao-chat-07#1": "screens-dev-manifest.csv", "cao-chat-08#1": "Conclusion: RRF k=60",
    "cao-chat-09#1": "11月7日", "cao-chat-10#1": "30 days for images",
}
SIZES = [(1400, 860), (1240, 820), (1120, 780)]


# ---------------------------------------------------------------- Matrix 客户端接口（只连本机）
def api(method: str, path: str, token: str | None = None, body=None, raw: bytes | None = None,
        ctype: str = "application/json") -> dict:
    assert HS.startswith("http://127.0.0.1")
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(HS + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(1.5)
                continue
            raise RuntimeError(f"{method} {path}: {e.code} {e.read()[:200]!r}") from None
    raise RuntimeError("unreachable")


def register(local: str, password: str) -> None:
    r = subprocess.run([str(MX / "venv/bin/register_new_matrix_user"), "-c", str(MX / "homeserver.yaml"),
                        "-u", local, "-p", password, "--no-admin", HS], capture_output=True, text=True)
    if r.returncode != 0 and "User ID already taken" not in (r.stdout + r.stderr):
        raise RuntimeError(r.stdout + r.stderr)


def login(local: str, password: str) -> str:
    return api("POST", "/_matrix/client/v3/login", body={
        "type": "m.login.password", "identifier": {"type": "m.id.user", "user": local},
        "password": password})["access_token"]


class Sender:
    """按房间记住已发到第几条消息。"""

    def __init__(self, task: str, spec: dict):
        self.tag = "c" + task.split("-")[-1]                 # cao-chat-03 -> c03
        self.run = datetime.now().strftime("%H%M%S")          # 每次运行一套新账号，重跑不会串房间
        self.members = spec["members"]
        self.tokens, self.uids = [], []
        for local, disp in self.members:
            name = f"{self.tag}{self.run}-{local}"
            pw = f"course-test-{name}"
            register(name, pw)
            tok = login(name, pw)
            api("PUT", f"/_matrix/client/v3/profile/@{name}:{SERVER}/displayname", tok, {"displayname": disp})
            self.tokens.append(tok)
            self.uids.append(f"@{name}:{SERVER}")
        self.me_local, self.me_pw = f"{self.tag}{self.run}-me", f"course-test-{self.tag}{self.run}-me"
        self.rooms = []      # [(room_id, msgs, sent_event_ids)]
        direct: dict[str, list[str]] = {}
        for r in spec["rooms"]:
            msgs = r["msgs"]
            if r.get("dm"):
                other = next(m[0] for m in msgs if isinstance(m[0], int) and m[0] != 0)
                body = {"is_direct": True, "preset": "trusted_private_chat", "invite": [self.uids[0]]}
                rid = api("POST", "/_matrix/client/v3/createRoom", self.tokens[other], body)["room_id"]
                api("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(rid)}", self.tokens[0], {})
                direct.setdefault(self.uids[other], []).append(rid)
            else:
                body = {"name": r["name"], "topic": r.get("topic", ""), "preset": "private_chat",
                        "invite": self.uids[:1] + self.uids[2:]}
                rid = api("POST", "/_matrix/client/v3/createRoom", self.tokens[1], body)["room_id"]
                for i in range(len(self.members)):
                    if i != 1:
                        api("POST", f"/_matrix/client/v3/join/{urllib.parse.quote(rid)}", self.tokens[i], {})
            self.rooms.append([rid, msgs, []])
        if direct:
            api("PUT", f"/_matrix/client/v3/user/{urllib.parse.quote(self.uids[0])}/account_data/m.direct",
                self.tokens[0], direct)
        self.txn = 0

    def send_next(self, room: int, n: int = 1) -> int:
        rid, msgs, sent = self.rooms[room]
        k = 0
        while k < n and len(sent) < len(msgs):
            m = msgs[len(sent)]
            self.txn += 1
            path = f"/_matrix/client/v3/rooms/{urllib.parse.quote(rid)}/send"
            if m[0] == "react":
                _, n_back, emoji = m
                target = [e for e in sent if e][-n_back]
                api("PUT", f"{path}/m.reaction/t{self.txn}", self.tokens[(self.txn % (len(self.tokens) - 1)) + 1],
                    {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": emoji}})
                sent.append(None)   # 反应不能再被反应
            else:
                who, what = m
                ev = api("PUT", f"{path}/m.room.message/t{self.txn}", self.tokens[who], self._content(who, what))
                sent.append(ev["event_id"])
            k += 1
        return k

    def _content(self, who: int, what) -> dict:
        if isinstance(what, str):
            return {"msgtype": "m.text", "body": what}
        kind = what[0]
        if kind == "code":
            _, lang, code = what
            esc = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return {"msgtype": "m.text", "body": f"```{lang}\n{code}\n```", "format": "org.matrix.custom.html",
                    "formatted_body": f'<pre><code class="language-{lang}">{esc}</code></pre>'}
        if kind == "html":
            return {"msgtype": "m.text", "body": what[1], "format": "org.matrix.custom.html", "formatted_body": what[2]}
        if kind == "file":
            _, name, text = what
            data = text.encode()
            up = api("POST", f"/_matrix/media/v3/upload?filename={urllib.parse.quote(name)}", self.tokens[who],
                     raw=data, ctype="text/plain")
            return {"msgtype": "m.file", "body": name, "url": up["content_uri"],
                    "info": {"mimetype": "text/plain", "size": len(data)}}
        raise ValueError(what)


# ---------------------------------------------------------------- 截图计划
def schedule(spec: dict) -> list[dict]:
    """返回 25 步：每步 {room, send, theme, size, scroll}。"""
    main_n = len(spec["rooms"][0]["msgs"])
    other_n = len(spec["rooms"][1]["msgs"]) if len(spec["rooms"]) > 1 else 0
    steps = []
    pre = 4
    rest = main_n - pre
    per = [rest // 14 + (1 if i < rest % 14 else 0) for i in range(14)]
    for i in range(14):
        steps.append({"room": 0, "send": per[i] + (pre if i == 0 else 0), "theme": "dark" if i >= 7 else "light",
                      "size": SIZES[i % 3], "scroll": 0, "what": "主房间新消息"})
    if other_n:
        dm_frames = min(other_n - 1, FRAMES - 14 - 3)
        for j in range(dm_frames):
            steps.append({"room": 1, "send": 2 if j == 0 else 1, "theme": "dark" if j % 2 else "light",
                          "size": SIZES[(j + 1) % 3], "scroll": 0, "what": "第二个房间新消息"})
        if dm_frames < other_n - 1:   # 没截到的余下消息最后一次补发
            steps[-1]["send"] += other_n - 1 - dm_frames
    k, n_scroll = 1, FRAMES - len(steps)
    while len(steps) < FRAMES:
        # 把“从底往上翻到顶”平均分成 n 段，每张多翻一段，最后一张正好到顶（避免翻到顶后重复截同一画面）
        steps.append({"room": 0, "send": 0, "theme": "light", "size": (1000, 760), "scroll": (k, n_scroll),
                      "what": f"主房间往上翻 {k}/{n_scroll}"})
        k += 1
    return steps


# ---------------------------------------------------------------- 浏览器
DISMISS = ["Dismiss", "Later", "Not now", "Close", "OK", "Skip", "No, thanks", "Got it", "Cancel"]


def dismiss(page) -> str:
    did = []
    for _ in range(3):
        hit = False
        for sel in [".mx_Toast_toast button", ".mx_Dialog button", ".mx_AnalyticsToast button"]:
            for b in page.locator(sel).all():
                try:
                    t = (b.inner_text(timeout=500) or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if t in DISMISS or t.lower() in ("dismiss", "later", "not now", "close", "skip"):
                    try:
                        b.click(timeout=1500)
                        did.append(t)
                        hit = True
                        time.sleep(0.5)
                    except Exception:  # noqa: BLE001
                        pass
        if not hit:
            break
    return "；关掉提示：" + "/".join(did) if did else ""


def set_theme(page, theme: str) -> None:
    page.evaluate("""t => { const k='mx_local_settings'; const s=JSON.parse(localStorage.getItem(k)||'{}');
        s.theme=t; s.use_system_theme=false; localStorage.setItem(k, JSON.stringify(s)); }""", theme)
    page.reload(wait_until="load")
    page.wait_for_selector(".mx_RoomView, .mx_HomePage", timeout=60000)


def visible_facts(page, task: str) -> list[str]:
    out = []
    for fid, key in FACT_KEYS.items():
        if not fid.startswith(task + "#"):
            continue
        ok = page.evaluate("""k => [...document.querySelectorAll('.mx_EventTile_body, .mx_MFileBody, .mx_MTextBody, .mx_EventTile_line')]
            .some(e => e.innerText.includes(k) && (r => r.bottom > 0 && r.top < innerHeight && r.height > 0)(e.getBoundingClientRect()))""", key)
        if ok:
            out.append(fid)
    return out


def next_seq(task_dir: Path) -> int:
    nums = [int(p.stem.rsplit("_", 1)[-1]) for p in task_dir.glob("*.png") if p.stem.rsplit("_", 1)[-1].isdigit()]
    return max(nums, default=0) + 1


def run_task(browser, task: str, out: Path) -> int:
    spec = TASKS[task]
    task_dir = out / task
    task_dir.mkdir(parents=True, exist_ok=True)
    log = task_dir / "capture_log.csv"
    if log.exists() and "Element" in log.read_text(encoding="utf-8-sig"):
        print(f"  {task}: 已有 Element 截图，跳过（要重采请先人工处理）")
        return 0
    s = Sender(task, spec)
    ctx = browser.new_context(no_viewport=True, locale="en-US")
    page = ctx.new_page()
    cdp = ctx.new_cdp_session(page)
    wid = cdp.send("Browser.getWindowForTarget")["windowId"]
    page.goto(f"{EL}/#/login", wait_until="load")
    page.wait_for_selector("#mx_LoginForm_username", timeout=60000)
    page.fill("#mx_LoginForm_username", s.me_local)
    page.fill("#mx_LoginForm_password", s.me_pw)
    page.keyboard.press("Enter")
    page.wait_for_selector(".mx_HomePage, .mx_RoomView, .mx_LeftPanel", timeout=90000)
    time.sleep(3)
    dismiss(page)
    theme, fails = None, 0
    for i, st in enumerate(schedule(spec), 1):
        w, h = st["size"]
        cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": 20, "top": 40, "width": w, "height": h}})
        if st["theme"] != theme:
            set_theme(page, st["theme"])
            theme = st["theme"]
        rid = s.rooms[st["room"]][0]
        if f"/room/{rid}" not in urllib.parse.unquote(page.url):
            page.goto(f"{EL}/#/room/{rid}")
            page.wait_for_selector(".mx_RoomView_timeline", timeout=60000)
        sent = s.send_next(st["room"], st["send"])
        time.sleep(2.5)
        panel = page.locator(".mx_RoomView_messagePanel")
        if st["scroll"]:
            k, n = st["scroll"]
            # Element 往上翻会分页加载更早的消息：多翻几次直到高度稳定，再按比例定位
            for _ in range(4):
                panel.evaluate("e => e.scrollTop = 0")
                time.sleep(1.0)
            panel.evaluate("e => e.scrollTop = e.scrollHeight")
            time.sleep(0.8)
            panel.evaluate("(e, [k, n]) => { const span = e.scrollHeight - e.clientHeight;"
                           " e.scrollTop = Math.round(span * (1 - k / n)); }", [k, n])
            time.sleep(1.2)
        else:
            panel.evaluate("e => e.scrollTop = e.scrollHeight")
        dis = dismiss(page)
        page.mouse.move(w - 6, 8)   # 鼠标停在窗口右上角空白处，避免悬停气泡
        page.bring_to_front()
        time.sleep(1.2)
        win = chromium_window(page.title())
        if not win:
            print(f"  !! {task} #{i}: 找不到唯一的 Chromium 窗口，跳过", file=sys.stderr)
            fails += 1
            continue
        now = datetime.now()
        seq = next_seq(task_dir)
        name = f"{task}_{now:%Y%m%d-%H%M%S}_{seq:03d}.png"
        dest = task_dir / name
        subprocess.run(["screencapture", "-x", "-o", f"-l{win[0]}", str(dest)], check=True)
        bad = looks_blank(dest)
        if bad:
            fails += 1
            print(f"  !! {name}: {bad}", file=sys.stderr)
        facts = visible_facts(page, task)
        room_name = spec["rooms"][st["room"]]["name"]
        append_log(task_dir, {
            "file": name, "captured_local": now.strftime("%Y-%m-%d %H:%M:%S"), "time_source": "manual",
            "app": "Chromium", "window_title": win[1],
            "source": f"真实 Element Web {ELEMENT_VER} 客户端（本机 Matrix 测试服务器 127.0.0.1）显示合成对话 {room_name}",
            "language": spec["lang"], "facts": ";".join(facts),
            "variation": f"窗口 {w}x{h}pt；{st['what']}（本次新发 {sent} 条）；{'深色' if theme == 'dark' else '浅色'}主题{dis}",
            "note": "合成对话，账号与人物虚构，只发在本机测试服务器；未发给任何真人" + (f"；{bad}" if bad else ""),
            "source_kind": "real_web",
        })
        print(f"  ok {task} #{i:02d} {name} [{room_name}] facts={facts}")
    ctx.close()
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--task", nargs="+", required=True)
    a = ap.parse_args()
    out = Path(a.out).resolve()
    api("GET", "/_matrix/client/versions")   # 服务器没起来就直接报错
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--window-position=20,40", "--window-size=1400,860",
                                                          "--lang=en-US", "--hide-crash-restore-bubble"])
        for t in a.task:
            try:
                fails += run_task(browser, t, out)
            except Exception as exc:  # noqa: BLE001  一个任务出错不影响后面的任务
                fails += 1
                print(f"  !! {t}: {exc}", file=sys.stderr)
        browser.close()
    print(f"完成，问题 {fails} 个")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
