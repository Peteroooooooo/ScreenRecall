"""用 Chromium DevTools 协议滚动 Edge / VS Code（Electron）页面：python tools/cdp_scroll.py <端口> <命令> ...

Chromium 系程序不处理投递的滚轮 / 按键消息（tools/scroll_window.py 对它们无效），所以实测时用单独的实例：
  msedge.exe --user-data-dir=<临时目录> --remote-debugging-port=9333 --disable-features=CalculateNativeWinOcclusion ...
  Code.exe   --user-data-dir=<临时目录> --extensions-dir=<临时目录> --remote-debugging-port=9334 ...
（临时配置，不碰用户自己的浏览器数据；关闭遮挡检测，被其他窗口盖住时也继续重绘，录得到最新画面。）

  eval  <JS 表达式>                         在页面里求值并打印结果
  run   --minutes M --every S --pixels P     按节奏在页面中央投递滚轮事件（Input.dispatchMouseEvent）：连续向下几次再向上
"""
import argparse
import asyncio
import json
import random
import sys
import time

import httpx
import websockets


def page_ws(port: int) -> str:
    targets = httpx.get(f"http://127.0.0.1:{port}/json", timeout=5, trust_env=False).json()
    page = next(t for t in targets if t["type"] == "page")
    return page["webSocketDebuggerUrl"]


class Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method: str, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                return msg.get("result", msg.get("error"))

    async def eval(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True)
        return r.get("result", {}).get("value", r)

    async def wheel(self, x: float, y: float, dy: float):
        await self.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=dy)


async def main_async(a) -> None:
    async with websockets.connect(page_ws(a.port), max_size=2**24) as ws:
        cdp = Cdp(ws)
        if a.cmd == "eval":
            print(json.dumps(await cdp.eval(a.expr), ensure_ascii=False))
            return
        size = await cdp.eval("[window.innerWidth, window.innerHeight]")
        x, y = size[0] * a.x, size[1] * a.y
        rng = random.Random(a.seed)
        end = time.monotonic() + a.minutes * 60
        direction, run_len, n = 1, rng.randint(4, 8), 0
        print(f"每约 {a.every} 秒在 ({x:.0f}, {y:.0f}) 滚动 {a.pixels}px，共 {a.minutes} 分钟", flush=True)
        while time.monotonic() < end:
            await cdp.wheel(x, y, direction * a.pixels)
            n += 1
            if n >= run_len:
                direction, run_len, n = -direction, rng.randint(3, 8), 0
            await asyncio.sleep(max(0.5, a.every * rng.uniform(0.7, 1.3)))
        print("done", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("port", type=int)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eval")
    e.add_argument("expr")
    r = sub.add_parser("run")
    r.add_argument("--minutes", type=float, default=7)
    r.add_argument("--every", type=float, default=10)
    r.add_argument("--pixels", type=float, default=600)
    r.add_argument("--x", type=float, default=0.6, help="滚轮位置（占视口宽度的比例）")
    r.add_argument("--y", type=float, default=0.5)
    r.add_argument("--seed", type=int, default=0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
