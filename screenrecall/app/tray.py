"""托盘图标（pystray）：暂停 / 继续、删除最近 5 分钟、打开主页、退出。

托盘不知道数据库和录制进程的细节：所有动作都是注入的回调（TrayActions），状态文字也由回调提供。
菜单先用 menu_model() 生成与 pystray 无关的描述（便于测试），再由 build_menu() 转成 pystray 菜单。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from PIL import Image, ImageDraw

log = logging.getLogger("screenrecall.app.tray")

# 与 web/src/App.tsx 的 Logo 同色：深色圆角底 + 黄 / 蓝 / 灰三条
_BG, _BORDER = (29, 34, 43, 255), (44, 51, 62, 255)
_MARKER, _BLUE, _GREY = (242, 180, 0, 255), (91, 124, 240, 255), (74, 81, 93, 255)
_PAUSED = (120, 126, 136, 255)


@dataclass(frozen=True)
class TrayStatus:
    text: str            # 菜单第一行显示的状态，如"正在录制：chrome.exe"
    paused: bool = False


@dataclass
class TrayActions:
    """托盘的所有动作。pause_until(None) 表示一直暂停到手动继续（目前菜单没有用到）。"""
    pause_until: Callable[[datetime | None], None]
    resume: Callable[[], None]
    delete_recent: Callable[[int], None]   # 参数：分钟数
    open_home: Callable[[], None]
    quit: Callable[[], None]
    status: Callable[[], TrayStatus]


@dataclass(frozen=True)
class MenuEntry:
    key: str
    label: str
    enabled: bool = True
    default: bool = False    # 左键单击图标时执行
    separator: bool = False


def draw_icon(size: int = 64, paused: bool = False) -> Image.Image:
    """按网页 Logo 画托盘图标（32 格坐标按比例放大）。暂停时第一条变灰。"""
    s = size / 32
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([1 * s, 1 * s, 31 * s - 1, 31 * s - 1], radius=8 * s, fill=_BG, outline=_BORDER,
                        width=max(1, round(s)))
    bars = [(7, 10, 13, 3.2, _PAUSED if paused else _MARKER), (7, 16, 18, 2.2, _BLUE), (7, 21, 10, 2.2, _GREY)]
    for x, y, w, h, color in bars:
        d.rounded_rectangle([x * s, y * s, (x + w) * s, (y + h) * s], radius=h * s / 2, fill=color)
    return im


def tomorrow_start(now: datetime | None = None, hour: int = 0) -> datetime:
    """"暂停到明天"的结束时间：明天 hour 点（本地时间）。"""
    now = now or datetime.now()
    return (now + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)


def menu_model(status: TrayStatus) -> list[MenuEntry]:
    """当前状态下的菜单（与 pystray 无关）。"""
    p = status.paused
    return [
        MenuEntry("status", status.text, enabled=False),
        MenuEntry("sep1", "", separator=True),
        MenuEntry("pause_15m", "暂停 15 分钟", enabled=not p),
        MenuEntry("pause_1h", "暂停 1 小时", enabled=not p),
        MenuEntry("pause_tomorrow", "暂停到明天", enabled=not p),
        MenuEntry("resume", "继续录制", enabled=p),
        MenuEntry("sep2", "", separator=True),
        MenuEntry("delete_5m", "删除最近 5 分钟"),
        MenuEntry("open_home", "打开主页", default=True),
        MenuEntry("sep3", "", separator=True),
        MenuEntry("quit", "退出"),
    ]


class Tray:
    """托盘图标。run() 阻塞当前线程直到 stop()；也可以 run_detached() 在后台线程里跑。"""

    def __init__(self, actions: TrayActions, *, title: str = "ScreenRecall",
                 clock: Callable[[], datetime] = datetime.now, day_start_hour: int = 0):
        self.actions, self.title = actions, title
        self._clock, self._day_start_hour = clock, day_start_hour
        self._icon = None
        self._shown_paused: bool | None = None
        self._ready = threading.Event()
        self._cache: tuple[float, TrayStatus | None] = (0.0, None)

    # ---------- 动作分发（测试直接调用） ----------

    def handlers(self) -> dict[str, Callable[[], None]]:
        a, now = self.actions, self._clock
        return {
            "pause_15m": lambda: a.pause_until(now() + timedelta(minutes=15)),
            "pause_1h": lambda: a.pause_until(now() + timedelta(hours=1)),
            "pause_tomorrow": lambda: a.pause_until(tomorrow_start(now(), self._day_start_hour)),
            "resume": a.resume,
            "delete_5m": lambda: a.delete_recent(5),
            "open_home": a.open_home,
            "quit": a.quit,
        }

    def invoke(self, key: str) -> None:
        """执行一个菜单项；回调出错只记日志，不让托盘线程崩掉。执行后刷新菜单和图标。"""
        try:
            self.handlers()[key]()
        except Exception:  # noqa: BLE001
            log.exception("托盘菜单 %s 执行出错", key)
        if key != "quit":
            self.refresh()

    def _status(self, fresh: bool = False) -> TrayStatus:
        """状态回调的结果缓存 0.3 秒：菜单弹出时每一项都会来问一次。"""
        t, cached = self._cache
        if not fresh and cached is not None and time.monotonic() - t < 0.3:
            return cached
        try:
            st = self.actions.status()
        except Exception:  # noqa: BLE001
            log.exception("读取托盘状态出错")
            st = TrayStatus("状态未知")
        self._cache = (time.monotonic(), st)
        return st

    # ---------- pystray ----------

    def build_menu(self):
        import pystray

        def entry(i: int):
            # 文字 / 可用状态在菜单每次弹出时重新计算（pystray 支持可调用对象）
            first = menu_model(self._status())[i]
            if first.separator:
                return pystray.Menu.SEPARATOR
            return pystray.MenuItem(lambda _item: menu_model(self._status())[i].label,
                                    lambda _icon, _item: self.invoke(first.key),
                                    enabled=lambda _item: menu_model(self._status())[i].enabled,
                                    default=first.default)

        return pystray.Menu(*(entry(i) for i in range(len(menu_model(TrayStatus(""))))))

    def refresh(self) -> None:
        """状态变化后调用：更新图标（暂停时变灰）、提示文字和菜单。"""
        icon = self._icon
        if icon is None:
            return
        st = self._status(fresh=True)
        if st.paused != self._shown_paused:
            icon.icon = draw_icon(64, st.paused)
            self._shown_paused = st.paused
        icon.title = f"{self.title} · {st.text}"[:127]  # Windows 托盘提示最长 128 个字符
        icon.update_menu()

    def run(self, setup: Callable[[], None] | None = None) -> None:
        import pystray

        st = self._status()
        self._shown_paused = st.paused
        self._icon = pystray.Icon("screenrecall", draw_icon(64, st.paused), f"{self.title} · {st.text}"[:127],
                                  menu=self.build_menu())

        def _setup(icon):
            icon.visible = True
            self._ready.set()
            if setup:
                setup()

        self._icon.run(setup=_setup)

    def run_detached(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="screenrecall-tray", daemon=True)
        t.start()
        self._ready.wait(5)
        return t

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
