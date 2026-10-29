"""名单页用的软件图标和名字：从 exe 本身读（图标资源 + "文件说明"），不按软件写死。

找 exe 的顺序：正开着的窗口 → App Paths 登记 → "已安装的程序"表。都找不到就没有图标（页面显示首字母）。
找到的结果一直缓存；找不到的 60 秒后再找（软件可能刚打开）。"""
from __future__ import annotations

import io
import os
import threading
import time
from typing import Any

MISS_RETRY_S = 60.0
INSTALLED_TTL_S = 600.0
ICON_PX = 64


class AppIcons:
    def __init__(self, winapi: Any):
        self._w = winapi
        self._lock = threading.Lock()
        self._hits: dict[str, dict] = {}       # 进程名（小写）→ {"name", "png"}
        self._misses: dict[str, float] = {}    # 进程名（小写）→ 上次没找到的时间
        self._installed: dict[str, str] = {}
        self._installed_at = -INSTALLED_TTL_S

    def _fn(self, name: str):
        return getattr(self._w, name, None)

    def _exe_path(self, low: str, running: dict[str, str] | None) -> str | None:
        if running is None:
            fn = self._fn("running_exe_paths")
            running = fn() if fn else {}
        if low in running:
            return running[low]
        reg = self._fn("registered_exe_path")
        p = reg(low) if reg else None
        if p:
            return p
        now = time.monotonic()
        if now - self._installed_at > INSTALLED_TTL_S:
            fn = self._fn("installed_exe_paths")
            self._installed = fn() if fn else {}
            self._installed_at = now
        return self._installed.get(low)

    def info(self, process: str, running: dict[str, str] | None = None) -> dict | None:
        """{"name": 显示名或 None, "png": 图标 PNG 字节或 None}；exe 找不到返回 None。"""
        low = os.path.basename(process or "").lower()
        if not low:
            return None
        with self._lock:
            if low in self._hits:
                return self._hits[low]
            if time.monotonic() - self._misses.get(low, -MISS_RETRY_S) < MISS_RETRY_S:
                return None
        try:
            path = self._exe_path(low, running)
        except Exception:  # noqa: BLE001  注册表 / 窗口枚举出错就当没找到
            path = None
        if not path:
            with self._lock:
                self._misses[low] = time.monotonic()
            return None
        hit = {"name": self._name(path), "png": self._png(path)}
        with self._lock:
            self._hits[low] = hit
            self._misses.pop(low, None)
        return hit

    def running_paths(self) -> dict[str, str]:
        fn = self._fn("running_exe_paths")
        try:
            return fn() if fn else {}
        except Exception:  # noqa: BLE001
            return {}

    def _name(self, path: str) -> str | None:
        fn = self._fn("file_description")
        try:
            name = fn(path) if fn else None
        except Exception:  # noqa: BLE001
            name = None
        return name.strip() or None if name else None

    def _png(self, path: str) -> bytes | None:
        fn = self._fn("exe_icon_rgba")
        try:
            rgba = fn(path, ICON_PX) if fn else None
        except Exception:  # noqa: BLE001
            rgba = None
        if rgba is None:
            return None
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(buf, "PNG")
        return buf.getvalue()
