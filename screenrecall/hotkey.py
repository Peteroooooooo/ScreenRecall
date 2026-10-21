"""全局快捷键补拍（方案 4.1）：后台注册系统热键，按下时请求立即补拍。

解析是纯函数（可在任意平台测试）；监听依赖 Windows RegisterHotKey，
在独立线程里运行消息循环，停止时向该线程投递 WM_QUIT。
注册失败（例如组合键已被其他程序占用）不阻止后台启动，只在页面上提示。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}
_NAMED_KEYS = {
    "space": 0x20, "pause": 0x13, "printscreen": 0x2C, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
}


@dataclass(frozen=True)
class Hotkey:
    modifiers: int
    vk: int
    label: str


def parse_hotkey(spec: str) -> Hotkey:
    """把 "ctrl+alt+s" 之类的写法解析为 RegisterHotKey 参数。至少需要一个修饰键。"""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError(f"快捷键需要至少一个修饰键和一个按键：{spec!r}")
    mods = 0
    for p in parts[:-1]:
        if p not in _MODS:
            raise ValueError(f"未知修饰键 {p!r}（可用 ctrl、alt、shift、win）")
        mods |= _MODS[p]
    key = parts[-1]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"不支持的按键 {key!r}（可用字母、数字、F1–F24 及 {', '.join(_NAMED_KEYS)}）")
    label = "+".join([p.capitalize() for p in parts[:-1]] + [key.upper()])
    return Hotkey(mods | MOD_NOREPEAT, vk, label)


class HotkeyListener:
    """在自己的线程中注册热键并运行消息循环。on_status(ok, text) 报告注册结果。"""

    HOTKEY_ID = 0x5352  # 'SR'

    def __init__(self, hotkey: Hotkey, on_press: Callable[[], None], on_status: Callable[[bool, str], None],
                 log: logging.Logger):
        self.hotkey = hotkey
        self.on_press = on_press
        self.on_status = on_status
        self.log = log
        self._thread_id: int | None = None
        self._ready = threading.Event()

    def run(self) -> None:
        from . import winapi

        self._thread_id = winapi.current_thread_id()
        winapi.ensure_message_queue()
        self._ready.set()
        if not winapi.register_hotkey(self.HOTKEY_ID, self.hotkey.modifiers, self.hotkey.vk):
            text = f"快捷键 {self.hotkey.label} 注册失败（可能已被其他程序占用），可在页面点击补拍"
            self.log.warning(text)
            self.on_status(False, text)
            return
        self.on_status(True, f"快捷键 {self.hotkey.label} 可随时补拍")
        self.log.info("hotkey registered %s", self.hotkey.label)
        try:
            for hotkey_id in winapi.hotkey_messages():
                if hotkey_id == self.HOTKEY_ID:
                    try:
                        self.on_press()
                    except Exception:
                        self.log.exception("处理快捷键补拍失败")
        finally:
            winapi.unregister_hotkey(self.HOTKEY_ID)

    def stop(self) -> None:
        from . import winapi

        if self._ready.wait(timeout=2) and self._thread_id:
            winapi.post_quit(self._thread_id)
