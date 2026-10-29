from types import SimpleNamespace

import numpy as np

from screenrecall.v3 import appicons
from screenrecall.v3.appicons import AppIcons


def fake_winapi(calls):
    def icon(path, size):
        calls.append(("icon", path))
        return np.zeros((size, size, 4), np.uint8)

    return SimpleNamespace(
        running_exe_paths=lambda: {"chrome.exe": r"C:\run\chrome.exe"},
        registered_exe_path=lambda p: r"C:\reg\code.exe" if p == "code.exe" else None,
        installed_exe_paths=lambda: calls.append(("installed",)) or {"qq.exe": r"C:\inst\QQ.exe"},
        file_description=lambda path: {"C:\\run\\chrome.exe": "Google Chrome", "C:\\inst\\QQ.exe": " QQ "}.get(path),
        exe_icon_rgba=icon,
    )


def test_finds_exe_from_running_then_registry_then_installed_list_and_caches():
    calls = []
    icons = AppIcons(fake_winapi(calls))
    chrome = icons.info("Chrome.exe")
    assert chrome["name"] == "Google Chrome" and chrome["png"].startswith(b"\x89PNG")
    assert icons.info("code.exe")["name"] is None
    assert icons.info("QQ.exe")["name"] == "QQ"
    assert icons.info("Weixin.exe") is None
    n = len(calls)
    assert icons.info("chrome.exe") is chrome and icons.info("weixin.exe") is None
    assert len(calls) == n  # 找到的一直缓存；没找到的 60 秒内不再找
    assert calls.count(("installed",)) == 1


def test_missing_app_is_looked_up_again_after_a_minute(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(appicons.time, "monotonic", lambda: now[0])
    paths = {}
    w = SimpleNamespace(running_exe_paths=lambda: dict(paths), exe_icon_rgba=lambda p, s: None)
    icons = AppIcons(w)
    assert icons.info("weixin.exe") is None
    paths["weixin.exe"] = r"C:\x\Weixin.exe"
    assert icons.info("weixin.exe") is None
    now[0] += appicons.MISS_RETRY_S + 1
    assert icons.info("weixin.exe") == {"name": None, "png": None}


def test_no_winapi_means_no_icons():
    icons = AppIcons(None)
    assert icons.info("chrome.exe") is None and icons.running_paths() == {}
