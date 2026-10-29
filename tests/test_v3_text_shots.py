"""v3 ⑤ 留截图：JPEG 编码与缩放、框换算、全黑判断、读不出字的新画面面积、ShotPolicy 的决定表（合成图，不碰真实屏幕）。"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from screenrecall.v3 import config
from screenrecall.v3.shots import (ShotPolicy, encode_jpeg, is_black, picture_tiles, shot_boxes, tile_signature,
                                   uncovered_change_ratio)
from screenrecall.v3.source import label_source
from screenrecall.v3.types import EncodedShot, TextLine


@pytest.fixture
def cfg(tmp_path):
    return config.build({}, data_dir=tmp_path / "data", home=tmp_path / "home")


def test_encode_jpeg_downscales_and_decodes():
    img = np.zeros((1200, 3840, 3), np.uint8)
    img[:, :1920] = (0, 0, 255)  # 左半红、右半黑
    shot = encode_jpeg(img, quality=80, max_width=1920)
    assert (shot.width, shot.height) == (1920, 600) and shot.scale == pytest.approx(0.5) and shot.boxes == []
    im = Image.open(io.BytesIO(shot.jpeg))
    assert im.format == "JPEG" and im.size == (1920, 600)
    r, g, b = im.convert("RGB").getpixel((400, 300))
    assert r > 200 and g < 50 and b < 50  # BGR 进、RGB 出：颜色没反
    small = encode_jpeg(np.full((100, 200, 4), 128, np.uint8), quality=80, max_width=1920)  # BGRA 也行
    assert (small.width, small.height, small.scale) == (200, 100, 1.0)
    assert len(encode_jpeg(img, quality=20, max_width=1920).jpeg) <= len(shot.jpeg)


def test_shot_boxes_scale_and_dedupe():
    lines = [TextLine("Hello 合成", (100, 50, 300, 20), "uia"), TextLine("hello合成", (100, 90, 300, 20), "ocr"),
             TextLine("   ", (0, 0, 10, 10), "ocr"), TextLine("第二行", (11, 31, 1, 1), "ocr")]
    assert shot_boxes(lines, 0.5) == [("hello合成", (50, 25, 150, 10)), ("第二行", (6, 16, 1, 1))]
    assert shot_boxes(lines[:1], 1.0) == [("hello合成", (100, 50, 300, 20))]


def test_is_black():
    assert is_black(np.zeros((720, 1280, 3), np.uint8))
    assert is_black(np.full((720, 1280, 4), 3, np.uint8))
    dark = Image.new("RGB", (1280, 720), (0, 0, 0))
    ImageDraw.Draw(dark).text((40, 300), "合成的深色主题文字 dark theme text " * 3, fill=(200, 200, 200))
    assert not is_black(np.asarray(dark)[:, :, ::-1])
    assert not is_black(np.full((10, 10, 3), 40, np.uint8))  # 深灰底不是"全黑"
    assert not is_black(np.zeros((0, 0, 3), np.uint8))


def test_uncovered_change_ratio():
    shape = (100, 200)
    lines = [TextLine("合成", (0, 0, 200, 50), "uia")]
    assert uncovered_change_ratio(None, shape, []) == 1.0
    assert uncovered_change_ratio(None, shape, lines) == pytest.approx(0.5)  # 上半有字盖住
    assert uncovered_change_ratio((0, 0, 100, 100), shape, lines) == pytest.approx(0.25)
    assert uncovered_change_ratio((0, 0, 200, 40), shape, lines) == 0.0  # 变的地方全是字
    assert uncovered_change_ratio((150, 80, 100, 100), shape, []) == pytest.approx(50 * 20 / 20000)  # 裁到帧内
    assert uncovered_change_ratio((0, 0, 0, 0), shape, []) == 0.0


def _text_page(h=480, w=640, rows=12):
    """合成的文字页：白底黑字，行和行之间是留白。"""
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    for i in range(rows):
        d.text((20, 20 + i * 36), f"synthetic text line {i} with some words", fill=(0, 0, 0))
    return np.asarray(im)[:, :, ::-1].copy()


def test_text_page_whitespace_is_not_a_picture():
    """文字页的行距、页边是一大片底色：没被字框盖住也不算"读不出字的新画面"。"""
    page = _text_page()
    assert not picture_tiles(page).any()
    assert uncovered_change_ratio(None, page.shape[:2], [], bgr=page) == 0.0
    photo = page.copy()
    photo[200:400, 300:600] = np.random.default_rng(1).integers(0, 255, (200, 300, 3), dtype=np.uint8)
    r = uncovered_change_ratio(None, page.shape[:2], [], bgr=photo)
    assert r == pytest.approx(200 * 300 / (480 * 640), rel=0.25)


def test_tile_signature_finds_what_changed_since_last_look():
    page = _text_page()
    a = tile_signature(page)
    assert a.shape == (15, 20) and (a == tile_signature(page.copy())).all()
    photo = page.copy()
    photo[200:400, 300:600] = np.random.default_rng(1).integers(0, 255, (200, 300, 3), dtype=np.uint8)
    b = tile_signature(photo)
    changed = a != b
    assert changed[7:12, 10:18].all() and not changed[:5].any()
    # 回到看过的同一屏：什么都没变，就不是新画面；新出来的照片才算
    assert uncovered_change_ratio(None, page.shape[:2], [], tiles=a != a, bgr=photo) == 0.0
    assert uncovered_change_ratio(None, page.shape[:2], [], tiles=changed, bgr=photo) > 0.1
    assert uncovered_change_ratio(None, page.shape[:2], [], tiles=changed) >         uncovered_change_ratio(None, page.shape[:2], [], tiles=changed, bgr=photo)


def _shot():
    return EncodedShot(b"jpeg", 10, 10, 1.0)


URL = label_source("msedge.exe", "合成文章", "https://article.test/a")
FILE = label_source("msedge.exe", "x.pdf", "file:///C:/x.pdf")
WEAK = label_source("chat.exe", "合成群聊", None)


def _decide(p, label, **kw):
    args = dict(label=label, source_id=1, visit_id=10, source_has_shot=False, t_ms=0, new_lines=0, has_text=True,
                uncovered_change=0.0, black=False)
    args.update(kw)
    return p.decide(**args)


def test_policy_decision_table(cfg):
    p = ShotPolicy(cfg)
    assert _decide(p, URL) == "first"
    assert _decide(p, FILE) == "first"  # 本地文件：每个文件 1 张
    assert _decide(p, URL, black=True) == "none"
    assert _decide(p, URL, source_has_shot=True, new_lines=5) == "none"  # 强标签已有图：新字也不再留
    assert _decide(p, WEAK, new_lines=3) == "visit"
    assert _decide(p, WEAK, new_lines=0) == "none"
    assert _decide(p, WEAK, new_lines=3, black=True) == "none"
    # 读不出字的新画面：够大才算，每个出处 fallback_shot_ms 最多一张，强弱标签都适用
    assert _decide(p, WEAK, uncovered_change=0.04) == "none"
    assert _decide(p, WEAK, uncovered_change=0.5, t_ms=1000) == "fallback"
    p.mark_fallback(1, 1000)
    assert _decide(p, WEAK, uncovered_change=0.5, t_ms=1000 + cfg.fallback_shot_ms - 1) == "none"
    assert _decide(p, WEAK, uncovered_change=0.5, t_ms=1000, source_id=2) == "fallback"  # 别的出处不受影响
    assert _decide(p, WEAK, uncovered_change=0.5, t_ms=1000 + cfg.fallback_shot_ms) == "fallback"
    assert _decide(p, URL, source_has_shot=True, uncovered_change=0.5, t_ms=1000) == "none"  # 出处 1 刚留过
    assert _decide(p, URL, source_has_shot=True, uncovered_change=0.5, t_ms=0, source_id=3) == "fallback"
    # 本地文件：只有第一次那 1 张，翻页、滚动（大片新画面）都不再留
    assert _decide(p, FILE, source_has_shot=True, uncovered_change=0.9, t_ms=0, source_id=4) == "none"
    assert _decide(p, FILE, source_has_shot=True, new_lines=3, source_id=4) == "none"


def test_weak_visit_saved_now_and_replaced_by_more_new_lines(cfg):
    """弱标签：一条里第一次有新字就存；之后新字更多的那拍替换它（一样多留先来的）；别的条目各算各的。"""
    p = ShotPolicy(cfg)
    assert _decide(p, WEAK, t_ms=0, new_lines=3) == "visit" and p.replaces(10, 0) is None
    p.saved_visit(10, 500, 3, 0)
    assert _decide(p, WEAK, t_ms=5000, new_lines=2) == "none"
    assert _decide(p, WEAK, t_ms=6000, new_lines=3) == "none"         # 一样多：留先来的
    assert _decide(p, WEAK, t_ms=9000, new_lines=7) == "visit" and p.replaces(10, 9000) == 500
    p.saved_visit(10, 501, 7, 9000)
    assert p.replaces(10, 9500) == 501
    assert _decide(p, WEAK, t_ms=9000, new_lines=1, visit_id=11) == "visit" and p.replaces(11, 9000) is None


def test_weak_visit_new_shot_every_weak_visit_ms(cfg):
    """一直不走：从这一段第一次存图起过了 weak_visit_ms，下一次有新字另存一张（不替换），重新开始计。"""
    p = ShotPolicy(cfg)
    w = cfg.weak_visit_ms
    p.saved_visit(10, 500, 5, 1000)
    p.saved_visit(10, 501, 9, 2000)                                    # 替换不改这一段的起点
    assert _decide(p, WEAK, t_ms=1000 + w - 1, new_lines=1) == "none"
    assert _decide(p, WEAK, t_ms=1000 + w, new_lines=1) == "visit" and p.replaces(10, 1000 + w) is None
    p.saved_visit(10, 502, 1, 1000 + w)
    assert p.replaces(10, 1000 + w + 1) == 502
    assert _decide(p, URL, t_ms=0, visit_id=12) == "first" and p.replaces(12, 0) is None  # 强标签不计
    p.clear()
    assert p.replaces(10, 1000 + w + 1) is None
