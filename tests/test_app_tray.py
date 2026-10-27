"""v2 主进程：托盘菜单模型、动作分发和图标（不显示真实托盘图标）。"""
from __future__ import annotations

from datetime import datetime

from screenrecall.app import tray

NOW = datetime(2026, 10, 27, 23, 50, 5)


class Recorder:
    def __init__(self):
        self.calls = []
        self.paused = False

    def actions(self):
        def pause(until):
            self.calls.append(("pause", until))
            self.paused = True

        def resume():
            self.calls.append(("resume",))
            self.paused = False

        return tray.TrayActions(pause_until=pause, resume=resume,
                                delete_recent=lambda m: self.calls.append(("delete", m)),
                                open_home=lambda: self.calls.append(("home",)),
                                quit=lambda: self.calls.append(("quit",)),
                                status=lambda: tray.TrayStatus("已暂停" if self.paused else "正在录制", self.paused))


def test_menu_model_labels_and_enabled_state():
    m = tray.menu_model(tray.TrayStatus("正在录制：notepad.exe"))
    labels = [e.label for e in m if not e.separator]
    assert labels == ["正在录制：notepad.exe", "暂停 15 分钟", "暂停 1 小时", "暂停到明天", "继续录制",
                      "删除最近 5 分钟", "打开主页", "退出"]
    by = {e.key: e for e in m}
    assert not by["status"].enabled and by["pause_15m"].enabled and not by["resume"].enabled
    assert by["open_home"].default
    p = {e.key: e for e in tray.menu_model(tray.TrayStatus("已暂停", paused=True))}
    assert not p["pause_1h"].enabled and p["resume"].enabled


def test_actions_dispatch_with_times():
    r = Recorder()
    t = tray.Tray(r.actions(), clock=lambda: NOW)
    for key in ("pause_15m", "pause_1h", "pause_tomorrow", "resume", "delete_5m", "open_home", "quit"):
        t.invoke(key)
    assert r.calls == [("pause", datetime(2026, 10, 28, 0, 5, 5)), ("pause", datetime(2026, 10, 28, 0, 50, 5)),
                       ("pause", datetime(2026, 10, 28, 0, 0)), ("resume",), ("delete", 5), ("home",), ("quit",)]


def test_tomorrow_start_hour():
    assert tray.tomorrow_start(NOW, hour=7) == datetime(2026, 10, 28, 7, 0)
    assert tray.tomorrow_start(datetime(2026, 12, 31, 9), 0) == datetime(2027, 1, 1)


def test_callback_errors_are_contained():
    r = Recorder()
    a = r.actions()
    a.delete_recent = lambda m: 1 / 0
    a.status = lambda: 1 / 0
    t = tray.Tray(a)
    t.invoke("delete_5m")  # 不抛出
    assert t._status(fresh=True).text == "状态未知"


def test_pystray_menu_builds_and_reads_status():
    r = Recorder()
    t = tray.Tray(r.actions())
    menu = t.build_menu()
    items = list(menu.items)
    texts = [i.text for i in items if i.text]
    assert texts[0] == "正在录制" and "继续录制" in texts
    resume = next(i for i in items if i.text == "继续录制")
    assert not resume.enabled
    r.paused = True
    t._cache = (0.0, None)
    assert resume.enabled
    assert next(i for i in items if i.text == "打开主页").default


def test_icon_drawing():
    a, b = tray.draw_icon(64), tray.draw_icon(64, paused=True)
    assert a.size == (64, 64) and a.mode == "RGBA"
    assert a.getpixel((0, 0))[3] == 0                # 圆角外透明
    assert a.getpixel((48, 52))[:3] == (29, 34, 43)  # 底色
    assert a.getpixel((16, 23))[:3] == (242, 180, 0) and b.getpixel((16, 23))[:3] != (242, 180, 0)
