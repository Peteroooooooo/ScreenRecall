"""前台窗口跟踪、窗口状态和排除规则。

- 前台窗口：SetWinEventHook（EVENT_SYSTEM_FOREGROUND、最小化开始/结束、标题变化）在专用消息线程里收事件，
  回调只记一下、叫醒录制核心；录制核心每一轮仍然直接读 GetForegroundWindow，钩子装不上时就退化成轮询；
- 窗口状态：不存在 / 最小化（IsIconic）/ 被隐藏（DWM cloaked）/ 锁屏。缺口一律从这些状态判断，
  从不根据"没有收到帧"推断（WGC 画面不变时本来就不送帧）；
- 排除规则：直接用 screenrecall.rules.check（进程名不区分大小写，标题关键词不区分大小写的子串）。
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

from ..config import RulesConfig
from ..rules import check as _rules_check

log = logging.getLogger(__name__)

# 桌面、任务栏、开始菜单、搜索、锁屏这类系统界面不当成"前台窗口"来录
SHELL_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                           "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow", "ForegroundStaging",
                           "MultitaskingViewFrame", "TaskSwitcherWnd"})
SHELL_PROCESSES = frozenset({"searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "lockapp.exe",
                             "searchapp.exe", "textinputhost.exe"})


def make_rules(excluded_processes=(), excluded_title_keywords=(), allowed_processes=()) -> RulesConfig:
    """和 config.load_config 一样规整：进程名去空白、转小写；关键词去空白；空项丢掉。"""
    def procs(xs):
        return tuple(str(x).strip().lower() for x in xs if str(x).strip())

    return RulesConfig(
        allowed_processes=procs(allowed_processes),
        excluded_processes=procs(excluded_processes),
        excluded_title_keywords=tuple(str(x).strip() for x in excluded_title_keywords if str(x).strip()),
    )


def rule_reason(process: str | None, title: str | None, rules: RulesConfig | None) -> str | None:
    """命中排除（或不在允许列表）时返回原因；可以录时返回 None。"""
    if rules is None:
        return None
    return _rules_check(process, title, rules)


def popup_owner(hwnd: int, *, root_owner: Callable[[int], int], pid: Callable[[int], int | None],
                title: Callable[[int], str], usable: Callable[[int], bool]) -> int:
    """前台是没有标题的附属小窗（查找栏、下拉框、菜单这类有所有者的弹出窗口）时，算它的主窗口在前台。

    这类小窗自己是一个顶层窗口，GetForegroundWindow 拿到的是它，但人在看的还是主窗口里的页面：
    截图、标题、出处都应该按主窗口算。条件（都满足才换）：小窗没有标题；最顶层所有者（GA_ROOTOWNER）不是它自己；
    所有者和它是同一个进程；所有者现在能录（存在、可见、没最小化、没被隐藏）。
    有自己标题的窗口（另存为、设置、单独弹出的聊天窗口）是另一个画面，照旧按它自己算。不按软件名判断。"""
    try:
        owner = int(root_owner(hwnd) or 0)
        if not owner or owner == hwnd or (title(hwnd) or "").strip():
            return hwnd
        if pid(owner) != pid(hwnd) or not usable(owner):
            return hwnd
        return owner
    except Exception:  # noqa: BLE001 - 查不到就按原来的窗口算
        return hwnd


@dataclass(frozen=True)
class WindowState:
    exists: bool
    minimized: bool = False
    cloaked: bool = False
    visible: bool = True

    @property
    def gap_reason(self) -> str | None:
        """这个状态下不能录的原因（缺口原因）；能录时为 None。"""
        if not self.exists:
            return "closed"
        if self.minimized:
            return "minimized"
        if self.cloaked or not self.visible:
            return "cloaked"
        return None


class WindowSystem(Protocol):
    def foreground(self) -> int: ...
    def state(self, hwnd: int) -> WindowState: ...
    def describe(self, hwnd: int) -> tuple[str, str] | None: ...  # (进程名, 标题)
    def is_shell(self, hwnd: int) -> bool: ...
    def locked(self) -> bool: ...
    def root_at(self, x: int, y: int) -> int: ...


class RealWindows:
    """真实的 Windows 窗口查询。进程名按窗口缓存（同一个窗口的进程不会变）。"""

    def __init__(self):
        from . import win32

        self._w = win32
        self._proc: dict[int, tuple[int, str]] = {}

    def foreground(self) -> int:
        """前台窗口；没有标题的附属小窗（查找栏这类）拿着焦点时是它的主窗口（popup_owner）。"""
        from .. import winapi

        h = self._w.foreground_window()
        if not h:
            return 0
        w = self._w
        return popup_owner(h, root_owner=w.root_owner, pid=winapi.window_pid, title=winapi.window_title,
                           usable=lambda o: self.state(o).gap_reason is None)

    def state(self, hwnd: int) -> WindowState:
        w = self._w
        if not w.is_window(hwnd):
            self._proc.pop(hwnd, None)
            return WindowState(False)
        return WindowState(True, w.is_minimized(hwnd), w.is_cloaked(hwnd), w.is_visible(hwnd))

    def _process(self, hwnd: int) -> str:
        from .. import winapi

        pid = winapi.window_pid(hwnd)
        hit = self._proc.get(hwnd)
        if hit is None or hit[0] != pid:
            hit = (pid, winapi.process_name(pid))
            self._proc[hwnd] = hit
        return hit[1]

    def describe(self, hwnd: int) -> tuple[str, str] | None:
        from .. import winapi

        if not self._w.is_window(hwnd):
            return None
        return self._process(hwnd), winapi.window_title(hwnd)

    def pid(self, hwnd: int) -> int | None:
        """窗口所属进程号（活动日志用）。"""
        self._process(hwnd)
        hit = self._proc.get(hwnd)
        return hit[0] if hit else None

    def is_shell(self, hwnd: int) -> bool:
        from .. import winapi

        if winapi.class_name(hwnd) in SHELL_CLASSES:
            return True
        return self._process(hwnd).lower() in SHELL_PROCESSES

    def locked(self) -> bool:
        from .. import winapi

        return winapi.is_input_desktop_locked()

    def root_at(self, x: int, y: int) -> int:
        return self._w.root_window_at(x, y)


class ForegroundWatcher:
    """WinEvent 钩子：前台切换、最小化、标题变化时记下窗口并调用 wake()。drain() 取走变化过的窗口。"""

    def __init__(self, wake: Callable[[], None] | None = None):
        from . import win32

        self._w = win32
        self._wake = wake
        self._changed: deque[int] = deque(maxlen=256)
        self._hooks: list = []
        self._proc = None
        self.events = 0
        self._thread = win32.MessageThread("sr-foreground", self._setup, self._teardown)

    def start(self) -> bool:
        ok = self._thread.start()
        if not ok:
            log.warning("前台窗口钩子没装上，退化为轮询：%s", self._thread.error)
        return ok

    def stop(self) -> None:
        self._thread.stop()

    def drain(self) -> set[int]:
        out = set()
        while True:
            try:
                out.add(self._changed.popleft())
            except IndexError:
                return out

    def _setup(self) -> None:
        w = self._w
        self._proc = w.WINEVENTPROC(self._on_event)
        for lo, hi in ((w.EVENT_SYSTEM_FOREGROUND, w.EVENT_SYSTEM_FOREGROUND),
                       (w.EVENT_SYSTEM_MINIMIZESTART, w.EVENT_SYSTEM_MINIMIZEEND),
                       (w.EVENT_OBJECT_NAMECHANGE, w.EVENT_OBJECT_NAMECHANGE)):
            h = w.SetWinEventHook(lo, hi, None, self._proc, 0, 0, w.WINEVENT_OUTOFCONTEXT)
            if not h:
                raise OSError("SetWinEventHook 失败")
            self._hooks.append(h)

    def _teardown(self) -> None:
        for h in self._hooks:
            self._w.UnhookWinEvent(h)
        self._hooks.clear()

    def _on_event(self, hook, event, hwnd, id_object, id_child, thread, t):
        if id_object != self._w.OBJID_WINDOW or id_child != 0 or not hwnd:
            return
        self.events += 1
        self._changed.append(int(hwnd))
        if self._wake is not None and event != self._w.EVENT_OBJECT_NAMECHANGE:
            try:
                self._wake()
            except Exception:  # noqa: BLE001
                pass


class FakeWindows:
    """测试用的窗口系统：直接改 fg / windows / locked。"""

    def __init__(self):
        self.fg = 0
        self.windows: dict[int, dict] = {}
        self.is_locked = False
        self.under_cursor: int | None = None
        self._lock = threading.Lock()

    def add(self, hwnd: int, process: str = "notepad.exe", title: str = "测试窗口", **state) -> None:
        self.windows[hwnd] = dict(process=process, title=title, minimized=False, cloaked=False, shell=False, **state)

    def foreground(self) -> int:
        """和 RealWindows 一样：add(…, owner=主窗口) 的无标题小窗在前台时算主窗口。"""
        h = self.fg
        if h not in self.windows:
            return h
        return popup_owner(h, root_owner=lambda x: self.windows.get(x, {}).get("owner") or x, pid=self.pid,
                           title=lambda x: self.windows[x]["title"], usable=lambda o: self.state(o).gap_reason is None)

    def state(self, hwnd: int) -> WindowState:
        w = self.windows.get(hwnd)
        if w is None:
            return WindowState(False)
        return WindowState(True, w["minimized"], w["cloaked"], True)

    def describe(self, hwnd: int) -> tuple[str, str] | None:
        w = self.windows.get(hwnd)
        return (w["process"], w["title"]) if w else None

    def pid(self, hwnd: int) -> int | None:
        w = self.windows.get(hwnd)
        return int(w.get("pid", 1000 + hwnd)) if w else None

    def is_shell(self, hwnd: int) -> bool:
        w = self.windows.get(hwnd)
        return bool(w and w["shell"])

    def locked(self) -> bool:
        return self.is_locked

    def root_at(self, x: int, y: int) -> int:
        return self.under_cursor if self.under_cursor is not None else self.fg
