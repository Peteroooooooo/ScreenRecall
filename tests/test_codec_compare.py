"""WebP 编码对照工具：合成文字图上的流程测试（不是编码实验结果）。"""

import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from screenrecall import codec_compare as cc


def _text_img(path, seed, fmt="PNG", **kw):
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", (640, 400), (30, 30, 30))
    d = ImageDraw.Draw(im)
    for i in range(12):
        d.text((10, 10 + i * 30), f"row {i} total {int(rng.integers(100, 999))}.{int(rng.integers(10, 99))} SGD",
               fill=(220, 220, 220))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, fmt, **kw)
    return path


class FakeOcr:
    """原图返回固定文字；像素与原图不同时把第一个数字改掉，模拟有损编码改字。"""

    def __init__(self):
        self.ref: dict[bytes, str] = {}

    def recognize(self, image):
        a = np.asarray(image)
        key = a[:40].tobytes()
        if not self.ref:
            self.ref[key] = "total 123.45 SGD\nv1.2.3"
        if key in self.ref:
            return [type("B", (), {"text": self.ref[key]})()]
        return [type("B", (), {"text": "total 128.45 SGD\nv1.2.3"})()]


def test_lossless_identical_lossy_smaller_and_reported(tmp_path):
    paths = [_text_img(tmp_path / "src" / f"t{k}" / f"{i}.png", 10 * k + i) for k in range(2) for i in range(3)]
    variants = cc.parse_variants("q92,lossless")
    rows, skipped = cc.compare(paths, variants, method=4, repeat=2, crops=2, crop_dir=tmp_path / "crops")
    assert not skipped and len(rows) == 6
    for r in rows:
        ll, q = r["variants"]["lossless"], r["variants"]["q92"]
        assert ll["max_diff"] == 0 and ll["psnr"] is None and ll["changed_px"] == 0
        assert q["max_diff"] > 0 and q["psnr"] is not None
        assert len(q["encode_ms"]) == 2
    s = cc.summarize(rows, variants, method=4)
    assert s["variants"]["lossless"]["pixel_identical"] == 6 and s["variants"]["q92"]["pixel_identical"] == 0
    assert s["variants"]["q92"]["frames_in_10GiB"] > 0
    assert len(list((tmp_path / "crops").glob("*.png"))) == 2
    with Image.open(next((tmp_path / "crops").glob("*.png"))) as im:
        assert im.size == (960 * 3, 640)          # 原图 + 2 个编码，顶部 40 像素标签
    md = cc.to_markdown(s, "合成测试图")
    assert "q92" in md and "lossless" in md
    json.dumps(s)


def test_ocr_digit_change_is_flagged(tmp_path):
    p = _text_img(tmp_path / "a.png", 1)
    variants = cc.parse_variants("q90,lossless")
    rows, _ = cc.compare([p], variants, method=4, repeat=1, ocr=FakeOcr())
    q, ll = rows[0]["variants"]["q90"], rows[0]["variants"]["lossless"]
    assert ll["ocr_identical"] and ll["ocr_cer"] == 0 and not ll["digits_lost"]
    assert not q["ocr_identical"] and q["digits_lost"] == ["123.45"] and q["digits_new"] == ["128.45"]
    s = cc.summarize(rows, variants, method=4)
    assert s["variants"]["q90"]["images_digits_changed"] == 1 and s["variants"]["lossless"]["images_digits_changed"] == 0
    assert "数字有变化" in cc.to_markdown(s, "")


def test_lossy_webp_is_not_a_valid_source(tmp_path):
    lossy = _text_img(tmp_path / "x.webp", 2, "WEBP", quality=80)
    lossless = _text_img(tmp_path / "y.webp", 3, "WEBP", lossless=True)
    assert not cc.is_lossless_source(lossy) and cc.is_lossless_source(lossless)
    rows, skipped = cc.compare([lossy, lossless], cc.parse_variants("q92"), method=4, repeat=1)
    assert len(rows) == 1 and len(skipped) == 1 and "不是无损来源" in skipped[0]


def test_helpers():
    assert cc.cer("abc 123", "abc123") == 0 and cc.cer("abcd", "abed") == 0.25
    assert cc.digit_tokens("v1.2.3 total 16 -> 18") == {"1.2.3": 1, "16": 1, "18": 1}
    assert cc.digit_tokens("[1, 2, 3]") == cc.digit_tokens("[1,2,3]")   # 只是 OCR 空格不同，不算数字变化
    with pytest.raises(ValueError):
        cc.parse_variants("q92,fast")


def test_pick_images_is_stratified(tmp_path):
    for k in range(3):
        for i in range(10 if k == 0 else 2):
            _text_img(tmp_path / f"t{k}" / f"{i}.png", k * 100 + i)
    picked = cc.pick_images([tmp_path], 6, seed=0)
    assert len(picked) == 6
    per = {p.parent.name: 0 for p in picked}
    for p in picked:
        per[p.parent.name] += 1
    assert per == {"t0": 2, "t1": 2, "t2": 2}       # 不会让大文件夹占满样本
    assert cc.pick_images([tmp_path], 0, seed=0) == sorted(tmp_path.rglob("*.png"))
