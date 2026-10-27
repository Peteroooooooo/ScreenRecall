"""v2 录制快路径：像素比对（motion）和一直在变的小块（volatile）。

全部用 PIL 合成的中英文画面；和实验版原算法（tests/_rec_motion_ref.py）逐条带核对结论一致。
"""

from __future__ import annotations

import numpy as np
import pytest

from screenrecall.rec import motion as M
from screenrecall.rec.volatile import VolatileConfig, VolatileTiles
from tests import _rec_motion_ref as REF
from tests import _rec_synth as S

W, VH = 900, 560


@pytest.fixture(scope="module")
def doc():
    return S.document(W, 6000, 1)


@pytest.fixture(scope="module")
def head():
    return S.header(W)


def _ref_verdict(a, b):
    r = REF.analyze(M.green(a), M.green(b))
    return r


def _check_equiv(a: np.ndarray, b: np.ndarray) -> M.Motion:
    """新版结论和原算法逐条带一致；返回新版结论。"""
    ref = _ref_verdict(a, b)
    got = M.analyze(M.Fp.from_image(a), M.Fp.from_image(b))
    if got.kind == "same":
        assert ref["上下结论"] == 0 and ref["整行没变的比例"] == 1.0
        return got
    assert got.dys == ref["dy"]
    ry = ref["上下结论"]
    if ry not in (None, 0):
        assert got.kind == "shift" and got.dy == ry
    else:
        assert got.dxs == ref["dx"]
        rx = ref["左右结论"]
        if rx not in (None, 0):
            assert got.kind == "shift" and got.dx == rx
        elif ry == 0:
            assert got.kind == "inplace"
        else:
            assert got.kind == "break"
    assert round(got.same_rows, 2) == ref["整行没变的比例"]
    return got


# ---------- 指纹 ----------

def test_fingerprint_high_byte_only_differences_do_not_collide():
    """两行只在某些 uint64 字的最高字节不同：不预混时约一半会撞，预混后一个都不撞。"""
    rng = np.random.default_rng(7)
    base = rng.integers(0, 16, size=(1, 96), dtype=np.uint8)
    rows = np.repeat(base, 256, axis=0)
    # 每行：第 0、1 个 uint64 字的最高字节（小端下是第 7、15 个字节）随机取值，其余字节完全一样
    rows[:, 7] = rng.integers(0, 256, size=256)
    rows[:, 15] = rng.integers(0, 256, size=256)
    rows = np.unique(rows, axis=0)
    n = len(rows)
    assert n > 200
    fps = M.row_fingerprints(rows)
    assert len(set(fps.tolist())) == n
    # 对照：不预混的加权和确实会撞（说明这个测试测到了要测的东西）
    words = rows.view(np.uint64)
    naive = (words * M._weights(words.shape[1])).sum(axis=1, dtype=np.uint64)
    assert len(set(naive.tolist())) < n * 0.8


def test_fingerprint_equal_rows_equal_and_padding_irrelevant():
    rng = np.random.default_rng(3)
    a = rng.integers(0, 16, size=(5, 37), dtype=np.uint8)
    b = a.copy()
    b[2, 36] ^= 1
    fa, fb = M.row_fingerprints(a), M.row_fingerprints(b)
    assert (fa[[0, 1, 3, 4]] == fb[[0, 1, 3, 4]]).all() and fa[2] != fb[2]
    # 行长补 0：短的行补到更宽的缓冲里，指纹不变
    wide = np.zeros((5, 48), np.uint8)
    wide[:, :37] = a
    assert (M.row_fingerprints(wide) == fa).all()


def test_strip_layout_matches_reference_columns():
    rng = np.random.default_rng(5)
    g = rng.integers(0, 16, size=(40, 2082), dtype=np.uint8)
    g[::7] = g[3]  # 制造一些相同的行
    g[8] = g[3]
    g[8, 1000] ^= 1  # 只差一个像素的行：偶数列 / 奇数列各一种，取不取得到看条带的起点
    g[9] = g[3]
    g[9, 1001] ^= 1
    v = M.vstrips(g)
    ref = REF.vstrips(g)
    for s in range(M.STRIPS):
        # 同一条带里"哪两行相等"的关系完全一致（取的是同样的列）
        eq_new = v[s][:, None] == v[s][None, :]
        eq_ref = np.array(ref[s], dtype=object)[:, None] == np.array(ref[s], dtype=object)[None, :]
        assert np.array_equal(eq_new, eq_ref)
    h = M.hbands(g)
    assert h.shape == (M.BANDS, 2082)


# ---------- 和原算法一致 ----------

@pytest.mark.parametrize("dy", [1, 3, 5, 37, 101, 250, 333])
def test_vertical_scroll_non_multiple_of_4(doc, head, dy):
    a = S.view(doc, 400, VH, head)
    b = S.view(doc, 400 + dy, VH, head)
    m = _check_equiv(a, b)
    assert m.kind == "shift" and m.dy == dy and m.dx == 0


def test_scroll_up_gives_negative(doc, head):
    m = _check_equiv(S.view(doc, 800, VH, head), S.view(doc, 800 - 123, VH, head))
    assert m.kind == "shift" and m.dy == -123


def test_horizontal_shift(head):
    wide = S.document(1800, 1200, 4)
    a = np.ascontiguousarray(wide[100:100 + VH, 300:300 + W])
    b = np.ascontiguousarray(wide[100:100 + VH, 300 + 90:300 + 90 + W])
    m = _check_equiv(a, b)
    assert m.kind == "shift" and m.dx == 90 and m.dy == 0


def test_two_independent_panes(doc):
    other = S.document(W, 6000, 9)
    half = W // 2

    def frame(t1, t2):
        out = np.empty((VH, W, 3), np.uint8)
        out[:, :half] = doc[t1:t1 + VH, :half]
        out[:, half:] = other[t2:t2 + VH, :half]  # 右栏也从行首开始，两栏都是满满的字
        return out

    # 只有左边一栏在滚：右栏 6 条竖带说 0，左栏 6 条说 80 → 在动的条一致 → 平移 80
    m = _check_equiv(frame(300, 900), frame(380, 900))
    assert m.kind == "shift" and m.dy == 80
    # 两栏各滚各的：在动的条分成两派、谁也不过半 → 接不上
    m = _check_equiv(frame(300, 900), frame(380, 1020))
    assert m.kind == "break"


def test_parallax_two_speed_layers_is_break(doc):
    """前后两层速度不同、在每条竖带里交错：没有一个数站得住 → 接不上。"""
    other = S.document(W, 6000, 11)

    def frame(t):
        out = doc[t:t + VH].copy()
        bg = other[t // 2: t // 2 + VH]
        cols = (np.arange(W) // 23) % 2 == 1
        out[:, cols] = bg[:, cols]
        return out

    m = _check_equiv(frame(600), frame(700))
    assert m.kind == "break"


def test_inplace_change(doc, head):
    a = S.view(doc, 1000, VH, head)
    b = S.write_text(a, 20, 300, "这一行原地改了 changed in place 42")
    m = _check_equiv(a, b)
    assert m.kind == "inplace" and m.dy == 0


def test_video_tile_with_scroll(doc, head):
    a = S.paint_box(S.view(doc, 1500, VH, head), 600, 200, 200, 120, seed=1)
    b = S.paint_box(S.view(doc, 1500 + 64, VH, head), 600, 200, 200, 120, seed=2)
    m = _check_equiv(a, b)
    assert m.kind == "shift" and m.dy == 64


def test_identical_is_same(doc, head):
    a = S.view(doc, 10, VH, head)
    m = M.analyze(M.Fp.from_image(a), M.Fp.from_image(a.copy()))
    assert m.kind == "same"


def test_resize_is_break(doc):
    m = M.analyze(M.Fp.from_image(S.view(doc, 0, VH)), M.Fp.from_image(S.view(doc, 0, VH + 10)))
    assert m.kind == "break"


def test_blank_frames_cannot_join():
    a = np.full((VH, W, 3), 255, np.uint8)
    b = a.copy()
    b[10:20, 10:20] = 0
    m = _check_equiv(a, b)
    assert m.kind in ("inplace", "break")


def test_fp_from_image_matches_green():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, size=(77, 131, 4), dtype=np.uint8)
    fp = M.Fp.from_image(img)
    assert fp.shape == (77, 131)
    assert np.array_equal(fp.g, img[:, :, 1] >> 4)
    assert fp.gp.shape == (96, 160) and not fp.gp[77:].any() and not fp.gp[:, 131:].any()


# ---------- 小块变化和遮罩 ----------

def test_tile_changes_and_rows_equal(doc):
    a = S.view(doc, 0, VH)
    b = a.copy()
    b[100, 333] = 0
    fa, fb = M.Fp.from_image(a), M.Fp.from_image(b)
    t = M.tile_changes(fa, fb)
    assert t.sum() == 1 and t[100 // 32, 333 // 32]
    eq = M.rows_equal(fa, fb)
    assert not eq[100] and eq.sum() == VH - 1


def test_volatile_after_three_looks_and_mask(doc, head):
    vt = VolatileTiles(VolatileConfig(min_changes=3, max_looks=8, window_ms=8000))
    base = S.view(doc, 2000, VH, head)
    frames = [S.paint_box(base, 600, 200, 160, 96, seed=i) for i in range(6)]
    fps = [M.Fp.from_image(f) for f in frames]
    t = 0
    results = []
    for a, b in zip(fps, fps[1:]):
        t += 1000
        ch = M.tile_changes(a, b)
        results.append(vt.only_volatile(ch, t))
        vt.look(ch, t)
    assert results[:3] == [False, False, False] and all(results[3:])
    mask = vt.mask(t, *fps[0].shape)
    assert mask is not None and mask.tiles.any()
    # 有遮罩时：视频区域在变，其余不变 → same
    m = M.analyze(fps[-2], fps[-1], mask)
    assert m.kind == "same"
    # 视频之外也变了 → 不是 same
    c = M.Fp.from_image(S.write_text(frames[-1], 20, 450, "视频之外的变化 outside"))
    assert M.analyze(fps[-1], c, mask).kind == "inplace"
    # 过了时间窗口，不再算一直在变
    assert vt.volatile(t + 20000) is None
    vt.reset()
    assert vt.mask(t, *fps[0].shape) is None


def test_mask_from_tiles_rows():
    tiles = np.zeros((4, 5), bool)
    tiles[1, 2] = True  # 行 32..63，列 64..95
    m = M.MotionMask.from_tiles(tiles, 120, 150)
    se = M.strip_edges(150)
    hit = [s for s in range(M.STRIPS) if m.v[s].any()]
    assert hit and all(se[s] < 96 and se[s + 1] > 64 for s in hit)
    assert m.v[hit[0]][32:64].all() and not m.v[hit[0]][:32].any()
