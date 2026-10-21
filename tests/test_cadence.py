"""cadence.py 纯逻辑：等稳、到期保存、排错限频（方案 4.1）。"""

from screenrecall import cadence as cad

from .conftest import make_image, tweak

W, H = 40, 30
CTX = (1, "p.exe", W, H)
P = cad.CadenceParams(stable_change_ratio=0.01, stable_timeout_s=10.0, debug_min_save_gap_s=2.0)


def b(img):
    return img.tobytes()


def test_change_ratio():
    a = make_image(1)
    assert cad.change_ratio(b(a), b(a), W, H) == 0.0
    assert cad.change_ratio(b(a), b(tweak(a, 6)), W, H) == 6 / (W * H)
    assert cad.change_ratio(b(a), b(a)[:-3], W, H) == 1.0  # 尺寸不符按完全变化处理


def test_first_change_after_start_saves_directly():
    c = cad.Cadence(P)
    assert c.observe(0, CTX, b(make_image(1)), W, H, changed=True) == cad.SAVE


def test_small_change_is_stable_and_saved_not_filtered():
    """单个数字变化低于阈值：属于稳定的新画面，立即保存（A01）。"""
    c = cad.Cadence(P)
    a = make_image(1)
    c.observe(0, CTX, b(a), W, H, changed=True)
    c.on_saved(0)
    c.observe(3, CTX, b(a), W, H, changed=False)
    assert c.observe(6, CTX, b(tweak(a, 5)), W, H, changed=True) == cad.SAVE


def test_big_change_waits_one_stable_interval():
    c = cad.Cadence(P)
    a, bb = make_image(1), make_image(2)
    c.observe(0, CTX, b(a), W, H, changed=True)
    c.on_saved(0)
    assert c.observe(3, CTX, b(bb), W, H, changed=True) == cad.WAIT
    assert c.observe(6, CTX, b(bb), W, H, changed=True) == cad.SAVE


def test_continuous_change_saves_at_first_sample_after_timeout_without_reset():
    c = cad.Cadence(P)
    c.observe(0, CTX, b(make_image(0)), W, H, changed=True)
    c.on_saved(0)
    decisions = [c.observe(t, CTX, b(make_image(100 + t)), W, H, changed=True) for t in (3, 6, 9, 12)]
    # 等待起点为 3，持续变化不重置；12 是首个满 10 秒的采样点
    assert decisions == [cad.WAIT, cad.WAIT, cad.WAIT, cad.WAIT]
    assert c.observe(13, CTX, b(make_image(999)), W, H, changed=True) == cad.SAVE


def test_change_that_disappears_clears_wait():
    c = cad.Cadence(P)
    c.observe(0, CTX, b(make_image(0)), W, H, changed=True)
    c.on_saved(0)
    assert c.observe(3, CTX, b(make_image(1)), W, H, changed=True) == cad.WAIT
    assert c.observe(6, CTX, b(make_image(0)), W, H, changed=False) == cad.NONE
    assert c.waiting_since() is None


def test_context_change_rebuilds_baseline():
    c = cad.Cadence(P)
    a = make_image(1)
    c.observe(0, CTX, b(a), W, H, changed=True)
    c.on_saved(0)
    other = (1, "p.exe", W, H + 1)
    # 尺寸变化后不与旧画面比较变化比例，即使像素很接近也先等待
    assert c.observe(3, other, b(a), W, H, changed=True) == cad.WAIT


def test_rebaseline_after_gap_does_not_save_directly():
    c = cad.Cadence(P)
    c.observe(0, CTX, b(make_image(1)), W, H, changed=True)
    c.on_saved(0)
    c.rebaseline()
    assert c.observe(30, CTX, b(make_image(2)), W, H, changed=True) == cad.WAIT
    assert c.observe(33, CTX, b(make_image(2)), W, H, changed=True) == cad.SAVE


def test_debug_mode_rate_limit_and_no_stability_wait():
    c = cad.Cadence(P)
    c.set_mode(cad.DEBUG)
    assert c.observe(0, CTX, b(make_image(1)), W, H, changed=True) == cad.SAVE
    c.on_saved(0)
    assert c.observe(1, CTX, b(make_image(2)), W, H, changed=True) == cad.HOLD
    assert c.observe(1.99, CTX, b(make_image(3)), W, H, changed=True) == cad.HOLD
    assert c.observe(2, CTX, b(make_image(4)), W, H, changed=True) == cad.SAVE


def test_debug_mode_respects_gap_from_last_normal_save_even_after_reset():
    c = cad.Cadence(P)
    c.observe(0, CTX, b(make_image(1)), W, H, changed=True)
    c.on_saved(0)
    c.reset(direct=True)  # 例如切换目标
    c.set_mode(cad.DEBUG)
    assert c.observe(1, CTX, b(make_image(2)), W, H, changed=True) == cad.HOLD
