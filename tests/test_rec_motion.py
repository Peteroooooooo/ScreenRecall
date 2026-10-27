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


_TEMPLATE_FIRST = ["A tired cartographer sketched eleven pewter spoons in the drafty attic during",
                   "The village baker polished a jar of amber resin on the ferry deck during the",
                   "Our night shift nurse described the broken weather vane at the edge of the",
                   "A retired astronomer photographed three rusty bicycle bells in the narrow",
                   "The museum guard rearranged an old brass telescope near the market square",
                   "A curious botanist repaired the cedar bookshelf beside the frozen pond in"]


def _templated(width: int, n: int, period: int, para_h: int, line_h: int = 34) -> np.ndarray:
    """每 period 段重复同一个首行、第二行（短，只占左半边）各不相同的合成页面。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, n * para_h + 40), "white")
    d = ImageDraw.Draw(img)
    f = S.font(22)
    for i in range(n):
        y = 10 + i * para_h
        d.text((16, y), _TEMPLATE_FIRST[i % period], fill="black", font=f)
        d.text((16, y + line_h), f"week {i + 3}, then wrote {i * 13 % 97} notes.", fill="black", font=f)
    return np.ascontiguousarray(np.array(img)[:, :, ::-1])


@pytest.mark.parametrize("period,dy", [(6, 480), (5, 460), (6, 500)])
def test_repeating_template_lines_do_not_give_a_wrong_shift(head, period, dy):
    """翻页（滚了将近一屏）时真正的重叠很少；每隔几段完全一样的首行让右边几条竖带对上一个差几段的错偏移。
    看得到第二行的条带在这个偏移下对不上 → 接不上（新块），不能给错的数（会把两屏的行交错着拼进同一块）。"""
    doc = _templated(W, 80, period, 84)
    m = M.analyze(M.Fp.from_image(S.view(doc, 300, VH, head)), M.Fp.from_image(S.view(doc, 300 + dy, VH, head)))
    assert m.kind == "break" or (m.kind == "shift" and m.dy == dy), m
    # 交叉复核（别的条带对不上）或整屏复核（独特的行在别的偏移下对上 / 几个偏移都说得通）拦下
    assert m.kind == "break" and any(k in m.agree for k in ("对不上", "都说得通", "别的偏移")), m
    # 同一页面小步滚动照样认得出
    m = M.analyze(M.Fp.from_image(S.view(doc, 300, VH, head)), M.Fp.from_image(S.view(doc, 340, VH, head)))
    assert m.kind == "shift" and m.dy == 40


def _mono(size: int = 18):
    from pathlib import Path

    from PIL import ImageFont

    p = Path(r"C:\Windows\Fonts\consola.ttf")
    return ImageFont.truetype(str(p), size) if p.exists() else S.font(size)


def _page(width: int, rows: list[str], line_h: int, *, font=None) -> np.ndarray:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, len(rows) * line_h + 40), "white")
    d = ImageDraw.Draw(img)
    f = font or _mono()
    for i, t in enumerate(rows):
        d.text((14, 10 + i * line_h), t, fill="black", font=f)
    return np.ascontiguousarray(np.array(img)[:, :, ::-1])


def _log_page(n: int = 400) -> np.ndarray:
    """日志：每行的日期、时、分一样，只有秒和毫秒那几个字不同，后半段按 4 / 9 / 7 行循环。"""
    return _page(W, [f"2026-10-27 10:{(i // 60) % 60:02d}:{i % 60:02d}.{(i * 37) % 1000:03d} INFO  [worker-{i % 4}] "
                     f"GET /api/items?page={i % 9} 200 {12 + i % 7} ms" for i in range(n)], 33)


def _chat_page(n: int = 200) -> np.ndarray:
    """头像、气泡一样的聊天，每 10 条完全重复（整页是严格周期的，周期 480 像素）。"""
    from PIL import Image, ImageDraw

    msgs = ["ok", "收到", "好的，我看一下", "明天再说", "ok"]
    img = Image.new("RGB", (W, n * 48 + 40), "white")
    d = ImageDraw.Draw(img)
    f = S.font(18)
    for i in range(n):
        y, left = 10 + i * 48, i % 2 == 0
        ax = 14 if left else W - 54
        d.ellipse((ax, y, ax + 36, y + 36), fill=(68, 170, 136))
        d.text((ax + 12, y + 6), "A", fill="white", font=f)
        tx = 62 if left else W - 260
        d.rectangle((tx, y + 2, tx + 190, y + 34), fill=(238, 238, 238))
        d.text((tx + 10, y + 6), msgs[i % len(msgs)], fill="black", font=f)
    return np.ascontiguousarray(np.array(img)[:, :, ::-1])


def _chrome(width: int, height: int = 120) -> np.ndarray:
    """浏览器那样的静止顶栏：几行横跨整个宽度、各不相同的字（每条竖带里都有十几行只出现一次的行）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (235, 238, 242))
    d = ImageDraw.Draw(img)
    f = S.font(18)
    for k, y in enumerate(range(8, height - 24, 28)):
        d.text((8, y), " ".join(f"tab{k}{j} 地址栏{j * 7 + k}" for j in range(12)), fill=(30, 30, 30), font=f)
    return np.asarray(img)[:, :, ::-1].copy()


@pytest.mark.parametrize("dy", [37, 122, 306, 490])
def test_log_with_one_distinctive_strip_joins_at_the_true_offset(dy):
    """只有一两条竖带看得到各不相同的字（秒），其余竖带的行在屏幕里都重复：
    单靠条带多数给不出数（原来一律接不上）；整屏复核在所有竖带一起看时站得住 → 接上，数是对的。"""
    doc, top = _log_page(), _chrome(W)
    a, b = S.view(doc, 300, VH, top), S.view(doc, 300 + dy, VH, top)
    m = M.analyze(M.Fp.from_image(a), M.Fp.from_image(b))
    assert m.kind == "shift" and m.dy == dy, m


@pytest.mark.parametrize("dy", [40, 153, 275, 490])
def test_strictly_periodic_chat_never_gives_a_number(dy):
    """完全周期的页面（每 10 条一样）：错开整段的偏移和真偏移一样说得通，没法知道真正滚了多少 → 接不上。
    静止的顶栏让好几条竖带说"没动"，也不能当成原地变了。"""
    doc, top = _chat_page(), _chrome(W)
    a, b = S.view(doc, 300, VH, top), S.view(doc, 300 + dy, VH, top)
    m = M.analyze(M.Fp.from_image(a), M.Fp.from_image(b))
    assert m.kind == "break", m


@pytest.mark.parametrize("dy", [10, 490, 560])
def test_periodic_chat_with_title_change_and_scrollbar_never_gives_a_number(dy):
    """完全周期的聊天页翻了将近一屏，同时网页标题（顶栏里的一段字）变了、右边的滚动条滑块挪了位置：
    标题那几行和滑块让"错开整段"的偏移看起来比真偏移差一点，但版式本身每 480 像素就重复 → 照样接不上。"""
    doc, top = _chat_page(), _chrome(W)

    def frame(y, title):
        f = S.write_text(S.view(doc, y, VH, top).copy(), 300, 8, title, size=18)
        f[top.shape[0]:, -14:] = 230  # 滚动条轨道
        t0 = top.shape[0] + y * VH // doc.shape[0]
        f[t0:t0 + VH * VH // doc.shape[0], -12:-2] = 120   # 滑块：长短、位置按页面比例，跟着位置走，不跟着内容走
        return f

    m = M.analyze(M.Fp.from_image(frame(300, "标题 s7 pd")), M.Fp.from_image(frame(300 + dy, "标题 e7 done")))
    assert m.kind == "break", m


@pytest.mark.parametrize("dy", [92, 214])
def test_static_chrome_votes_do_not_make_a_scroll_inplace(dy):
    """表格：只有最左边一列的序号各不相同；高高的静止顶栏让每条竖带都有十几行"只出现一次"的行说"没动"。
    原来判成原地变了（位置不动，新一屏按旧位置写进同一块 = 偏移错）；现在整屏复核按在动的行算 → 平移 dy。"""
    rows = [f"{i:4d}   合成条目 Item     OK     2026-10-27" for i in range(300)]
    doc = _page(W, rows, 34, font=S.font(20))
    top = _chrome(W, 200)
    a, b = S.view(doc, 300, VH, top), S.view(doc, 300 + dy, VH, top)
    m = M.analyze(M.Fp.from_image(a), M.Fp.from_image(b))
    assert m.kind != "inplace", m
    assert m.kind == "shift" and m.dy == dy, m


def test_jump_without_overlap_under_static_chrome_is_break_not_inplace():
    """翻过了一整屏（没有重叠，认不出偏移）：每条竖带都因为静止的顶栏说"没动"，空白行也都没变，
    但在变的行里没有一张"没动"的票 → 接不上，不当成原地变了。"""
    doc, top = _log_page(), _chrome(W, 200)
    m = M.analyze(M.Fp.from_image(S.view(doc, 300, VH, top)), M.Fp.from_image(S.view(doc, 300 + VH + 40, VH, top)))
    assert m.kind == "break", m


def test_title_text_change_under_static_content_is_inplace():
    """只有顶栏里的一段字变了（网页标题、标签页）：照样是原地变了。"""
    doc, top = _log_page(), _chrome(W, 120)
    a = S.view(doc, 300, VH, top)
    b = S.write_text(a.copy(), 300, 8, "新的标题 new title 2026", size=18)
    m = M.analyze(M.Fp.from_image(a), M.Fp.from_image(b))
    assert m.kind == "inplace", m


@pytest.mark.parametrize("dy", [530, 600])
def test_page_jump_with_static_side_panel_is_break_not_inplace(doc, head, dy):
    """翻页滚了将近一屏（重叠太少认不出偏移），只有固定的侧栏那一条说"没动"：不能当成原地变了
    （那样新一屏的行会按旧位置写进同一块）→ 接不上。"""
    side = np.random.default_rng(3).integers(0, 255, size=(VH + 60, 70, 3), dtype=np.uint8)
    a, b = S.view(doc, 300, VH, head).copy(), S.view(doc, 300 + dy, VH, head).copy()
    for f in (a, b):
        f[:, :70] = side[:f.shape[0]]
        f[:, -70:] = side[:f.shape[0]]
    m = M.analyze(M.Fp.from_image(a), M.Fp.from_image(b))
    assert m.kind == "break" and ("说没动" in m.agree or "站不住" in m.agree), m


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
