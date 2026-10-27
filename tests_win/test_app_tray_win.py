"""真实托盘：显示图标约 2 秒、刷新一次状态，再移除。"""
from __future__ import annotations

import time

from screenrecall.app import tray


def test_tray_icon_shows_and_stops():
    state = {"paused": False}
    actions = tray.TrayActions(pause_until=lambda u: state.update(paused=True), resume=lambda: state.update(paused=False),
                               delete_recent=lambda m: None, open_home=lambda: None, quit=lambda: None,
                               status=lambda: tray.TrayStatus("测试图标（会自动消失）", state["paused"]))
    t = tray.Tray(actions, title="ScreenRecall 测试")
    th = t.run_detached()
    try:
        assert t.wait_ready(5) and th.is_alive()
        assert t._icon.visible
        time.sleep(1.0)
        t.invoke("pause_15m")  # 图标变灰、提示文字更新
        assert state["paused"] and t._shown_paused is True
        time.sleep(1.0)
    finally:
        t.stop()
        th.join(5)
    assert not th.is_alive()
