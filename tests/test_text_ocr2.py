"""v2 OCR：本地模型清单与校验、缺文件报错、禁止运行时下载、下载校验、合成图识别。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from screenrecall.text import ocr2

_HAVE = all((ocr2.DEFAULT_MODELS_DIR / m.name).is_file() for m in ocr2.model_manifest())
needs_models = pytest.mark.skipif(not _HAVE, reason="本机未下载 OCR 模型（python -m screenrecall.text.ocr2 --download）")


def test_manifest_from_installed_package():
    files = ocr2.model_manifest()
    names = {m.name for m in files}
    assert names == {"PP-OCRv6_det_tiny.onnx", "PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_tiny.onnx",
                     "PP-OCRv6_rec_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx"}
    for m in files:
        assert len(m.sha256) == 64 and m.url.endswith(m.name)
    assert {m.task for m in ocr2.model_manifest(("tiny",))} == {"det", "rec", "cls"}


def test_missing_models_clear_error(tmp_path):
    with pytest.raises(ocr2.OcrModelsMissing, match="--download"):
        ocr2.resolve_models("tiny", tmp_path)
    with pytest.raises(ocr2.OcrModelsMissing):
        ocr2.OcrEngine("tiny", models_dir=tmp_path)
    with pytest.raises(ValueError):
        ocr2.resolve_models("medium", tmp_path)


def test_tampered_model_rejected(tmp_path):
    for m in ocr2.model_manifest(("tiny",)):
        (tmp_path / m.name).write_bytes(b"not a model")
    with pytest.raises(ocr2.OcrModelsMissing, match="SHA256"):
        ocr2.resolve_models("tiny", tmp_path)


def test_runtime_download_is_refused():
    from rapidocr.utils.download_file import DownloadFile

    orig = DownloadFile.run
    with ocr2._no_download():
        with pytest.raises(ocr2.OcrModelsMissing):
            DownloadFile.run(None)
    assert DownloadFile.run == orig


def test_download_verifies_hash_and_leaves_no_partial(tmp_path, monkeypatch):
    good = b"model-bytes" * 100
    files = [ocr2.ModelFile("det", "tiny", "a.onnx", "https://example.invalid/a.onnx", hashlib.sha256(good).hexdigest()),
             ocr2.ModelFile("rec", "tiny", "b.onnx", "https://example.invalid/b.onnx", "0" * 64)]
    monkeypatch.setattr(ocr2, "model_manifest", lambda model_types=ocr2.MODEL_TYPES: files)

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, n):
            yield good

    import httpx

    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Resp())
    with pytest.raises(ocr2.OcrModelsMissing, match="SHA256"):
        ocr2.download(tmp_path)
    assert (tmp_path / "a.onnx").read_bytes() == good
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.onnx"]  # 校验失败的不留半截文件
    files.pop()
    assert ocr2.download(tmp_path) == ["已有 a.onnx"]


def test_as_bgr_shapes():
    bgra = np.zeros((4, 5, 4), dtype=np.uint8)
    assert ocr2._as_bgr(bgra).shape == (4, 5, 3)
    assert ocr2._as_bgr(np.zeros((4, 5), dtype=np.uint8)).shape == (4, 5, 3)
    with pytest.raises(TypeError):
        ocr2._as_bgr(np.zeros((4, 5, 3), dtype=np.float32))


def _synthetic() -> tuple[np.ndarray, list[str]]:
    lines = ["今天下午三点开组会 Meeting at 3pm", "请把 report_v2.pdf 发到群里", "Total: 1,234.56 元"]
    im = Image.new("RGB", (720, 240), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 22)
    for i, t in enumerate(lines):
        d.text((20, 30 + i * 60), t, fill="black", font=f)
    return np.ascontiguousarray(np.asarray(im)[:, :, ::-1]), lines


def _site_models() -> set[str]:
    import rapidocr

    d = Path(rapidocr.__file__).parent / "models"
    return {p.name for p in d.iterdir()} if d.exists() else set()


@needs_models
def test_tiny_recognizes_synthetic_and_never_writes_site_packages():
    before = _site_models()
    eng = ocr2.OcrEngine("tiny", threads=2)
    assert "PP-OCRv6 det_tiny+rec_tiny" in eng.version() and "rapidocr 3.9.2" in eng.version()
    bgr, truth = _synthetic()
    for crop in (False, True):
        got = eng.recognize(bgr, crop=crop)
        assert len(got) == 3
        text = "\n".join(ln.text for ln in got)
        assert "report_v2.pdf" in text and "组会" in text
        assert "at 3pm" in text  # 英文空格保留
        first = got[0]
        assert 0 <= first.x0 < first.x1 <= 720 and 0 <= first.y0 < first.y1 <= 240
        assert 0.5 <= first.confidence <= 1.0 and len(first.quad) == 4
    assert eng.recognize(np.zeros((1, 1, 3), dtype=np.uint8)) == []
    assert eng.recognize(np.full((50, 50, 3), 255, dtype=np.uint8)) == []
    assert _site_models() == before
