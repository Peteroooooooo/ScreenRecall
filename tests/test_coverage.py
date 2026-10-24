"""捕获覆盖实验工具：脚本、模拟播放与统计。

模拟播放用真实 Recorder + 真实 SQLite，窗口是假 winapi（conftest），时间是注入时钟，只在采样时刻渲染画面。
这只验证 plan / analyze 的口径和节奏逻辑在冻结脚本下的表现，**不是实测覆盖率**：真实数字要在 Windows 实机上跑 stimulus。
"""

import json

import numpy as np
import pytest

from screenrecall import coverage

from .conftest import make_image, tweak


def test_plan_is_deterministic_and_timeline_consistent():
    a = coverage.make_plan("normal", reps=2, seed=7, interval_s=3)
    b = coverage.make_plan("normal", reps=2, seed=7, interval_s=3)
    assert a == b and len(a["trials"]) == 3 * 5 * 4 * 2
    assert coverage.make_plan("normal", reps=2, seed=8, interval_s=3)["trials"] != a["trials"]
    cells = {(t["kind"], t["duration_s"], t["background"]) for t in a["trials"]}
    assert len(cells) == 60
    for t in a["trials"]:
        assert 0 <= t["jitter_s"] < 3
        assert t["t_event"] - t["t_base"] >= coverage.SETTLE_S
        assert t["t_return"] - t["t_event"] == pytest.approx(t["duration_s"])
        mid = (t["t_event"] + t["t_return"]) / 2
        assert coverage.state_at(a, mid) == (t, "event")
    assert coverage.state_at(a, a["total_s"] + 1) == (None, "done")
    assert len({t["token"] for t in a["trials"]}) == len(a["trials"])
    assert len(a["parts_s"]) == 1   # 普通模式不分段
    d = coverage.make_plan("debug", reps=2, seed=7, interval_s=1)
    assert len(d["parts_s"]) >= 2 and max(d["parts_s"]) <= coverage.PART_MAX_S["debug"]
    for p in range(len(d["parts_s"])):
        first = min(t["t_base"] for t in d["trials"] if t["part"] == p)
        assert first == 0.0


def _render(tr, phase, t):
    base = make_image(500 + tr["trial"])
    img = base.copy()
    if phase == "event":
        if tr["kind"] == "single_char":
            img = tweak(base, 4)                       # 4/1200 ≈ 0.3%
        elif tr["kind"] == "passive_message":
            img[22:30, 28:40] = 255                    # 96/1200 = 8%
        else:
            img = make_image(9000 + tr["trial"])       # 整屏换 B
    bg = tr["background"]
    if bg == "cursor_blink" and int(t / 0.53) % 2 == 0:
        img[0, 39] = 0
    elif bg == "big_animation":
        img[5:20, 0:20] = make_image(int(t * 10), 20, 15)   # 300/1200 = 25%，每次采样都不同
    elif bg == "scroll":
        img[10:20] = np.roll(img[10:20], int(t / 0.7), axis=0)
    return img


def _simulate(h, monkeypatch, plan):
    import screenrecall.capture as capture_mod
    import screenrecall.control as control_mod

    clock_ms = lambda: int(h.clock.t * 1000)  # noqa: E731 - 让抓拍时间与日志共用同一时钟
    monkeypatch.setattr(capture_mod, "utc_ms", clock_ms)
    monkeypatch.setattr(control_mod, "utc_ms", clock_ms)
    h.screen.title = plan["title"]
    log = {}
    for part, length in enumerate(plan["parts_s"]):
        t0 = h.clock.t
        trials = [tr for tr in plan["trials"] if tr["part"] == part]
        for tr in trials:
            log[tr["trial"]] = {p: int((t0 + tr[k]) * 1000) for p, k in
                                (("base", "t_base"), ("event", "t_event"), ("return", "t_return"))}
        tr0, ph0 = coverage.state_at(plan, 0, part)
        h.show(_render(tr0, ph0, 0))
        if part:
            h.request("paused")       # 每段是一次新的排错会话：先暂停再重新进入
        h.start(plan["mode"])
        h.rec.sample(h.conn)
        while h.clock.t - t0 < length:
            step = h.rec._interval()
            t = h.clock.t + step - t0
            tr, ph = coverage.state_at(plan, t, part)
            if tr is not None:
                h.show(_render(tr, ph, t))
            h.tick(step)
            h.conn.execute("UPDATE index_jobs SET status = 'done'")  # 模拟 OCR 跟得上，不触发排错过载
    return log


@pytest.mark.parametrize("mode", ["normal", "debug"])
def test_simulated_run_meets_target_and_reports_misses(harness_factory, monkeypatch, mode):
    h = harness_factory()
    plan = coverage.make_plan(mode, reps=1, seed=3, interval_s=3.0 if mode == "normal" else 1.0,
                              durations=(1, 4, 6, 10), backgrounds=("none", "cursor_blink", "big_animation"))
    log = _simulate(h, monkeypatch, plan)
    rep = coverage.analyze(h.conn, plan, log)
    assert rep["trials_played"] == len(plan["trials"]) and rep["trials_mode_mismatch"] == 0
    assert rep["frames_from_stimulus"] > 0
    # 主体静止 / 小光标、事件够长：节奏逻辑保证能存到
    assert rep["target"]["n"] > 0 and rep["target"]["rate"] == 1.0 and rep["target"]["meets_goal"]
    # 1 秒事件与大面积动画下不可能全部覆盖；漏掉的如实计数
    assert rep["overall"]["rate"] < 1.0 and rep["missed"]["n"] > 0
    assert rep["missed"]["marked_unsaved_change"] <= rep["missed"]["n"]
    if mode == "normal":
        # 普通模式：大面积动画让画面一直"不稳定"，只靠 10 秒兜底，6 秒以下事件覆盖明显更低
        anim = [r for r in rep["per_trial"] if r["background"] == "big_animation" and r["duration_s"] <= 6]
        still = [r for r in rep["per_trial"] if r["background"] == "none" and r["duration_s"] >= 4]
        assert sum(r["covered"] for r in anim) / len(anim) < sum(r["covered"] for r in still) / len(still)
    md = coverage.to_markdown(rep)
    assert "目标条件" in md and "达到" in md
    json.dumps(rep)  # 可序列化


def test_unplayed_and_mode_mismatch_are_excluded(harness_factory, monkeypatch):
    h = harness_factory()
    plan = coverage.make_plan("debug", reps=1, seed=1, interval_s=1.0, kinds=("aba",), durations=(6,),
                              backgrounds=("none",))
    log = _simulate(h, monkeypatch, plan)
    rep = coverage.analyze(h.conn, plan, {})
    assert rep["trials_played"] == 0 and all(r["status"] == "not_played" for r in rep["per_trial"])
    # 模拟"排错模式提前退回"：把该试次的画面改记为 normal
    h.conn.execute("UPDATE frames SET capture_meta = json_set(capture_meta, '$.mode', 'normal')")
    rep = coverage.analyze(h.conn, plan, log)
    assert rep["trials_mode_mismatch"] == 1 and rep["overall"]["n"] == 0 and rep["target"]["n"] == 0


def test_event_marker_matches_what_the_event_screen_shows():
    # single_char 的事件画面是"counter = 基线+1"，不显示令牌（2026-10-24 实测：按令牌核对是 0/27、0/38）
    tr = {"kind": "single_char", "base_counter": 1509, "token": "23186"}
    m = coverage.event_marker(tr)
    assert m.search("screen a / counter = 1510 / status: running")
    assert m.search("screena / counter=1510")              # OCR 吞掉空格
    assert not m.search("screena / counter = 1509")        # 基线画面
    assert not m.search("counter = 15100") and not m.search("23186")
    for kind in ("passive_message", "aba"):
        m = coverage.event_marker({"kind": kind, "base_counter": 1509, "token": "23186"})
        assert m.search("new message msg 23186") and not m.search("counter = 1510")


def test_wilson_interval():
    assert coverage.wilson(0, 0) is None
    lo, hi = coverage.wilson(60, 60)
    assert hi == 1.0 and 0.93 < lo < 0.95   # 60/60 的下界约 94%：要声称 ≥95% 需要更多试次
