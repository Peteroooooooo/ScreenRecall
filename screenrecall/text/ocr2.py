"""v2 OCR：rapidocr 3.9.2 + PP-OCRv6（默认 tiny，低置信度复查 / 扫描件用 small）。

只在电脑空闲时由空闲整理进程调用（UIA 读不到字的证据帧、待 OCR 的区域）。

模型文件只从本地目录加载，并按 rapidocr 包内 default_models.yaml 的 SHA256 校验；
rapidocr 3.x 默认会在运行时从 modelscope 下载到 site-packages，这里在构造期间直接禁止下载，
缺文件时报清楚的中文错误。准备模型（联网一次）：

    python -m screenrecall.text.ocr2 --download [--dir PATH]

参数（实测结论）：Global.log_level='error'；不用方向分类（use_cls=false，屏幕文字都是正的）；
截图里裁出来的小块用 Det.limit_type='max'（默认 'min'/736 会把小图放大，慢且不更准）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_DIR = REPO_ROOT / "models" / "ocr"
MODEL_TYPES = ("tiny", "small")
OCR_VERSION = "PP-OCRv6"

# (任务, 模型档) -> default_models.yaml 里 onnxruntime 下的键路径
_YAML_KEYS = {
    ("det", "tiny"): ("PP-OCRv6", "det", "multi_PP-OCRv6_det_tiny"),
    ("det", "small"): ("PP-OCRv6", "det", "multi_PP-OCRv6_det_small"),
    ("rec", "tiny"): ("PP-OCRv6", "rec", "multi_PP-OCRv6_rec_tiny"),
    ("rec", "small"): ("PP-OCRv6", "rec", "multi_PP-OCRv6_rec_small"),
    # 方向分类不使用，但 rapidocr 构造时总会加载它，必须给一个本地文件（585 KB）
    ("cls", "mobile"): ("PP-OCRv4", "cls", "ch_ppocr_mobile_v2.0_cls_mobile"),
}


class OcrModelsMissing(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    quad: list[list[float]]  # 4 个 [x, y]，输入图像素坐标
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class ModelFile:
    task: str
    model: str
    name: str
    url: str
    sha256: str


# ---------- 模型清单与校验 ----------

def _package_dir() -> Path:
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or not spec.origin:
        raise OcrModelsMissing("未安装 rapidocr（需要 rapidocr==3.9.2）")
    return Path(spec.origin).parent


def rapidocr_version() -> str:
    try:
        return importlib.metadata.version("rapidocr")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def model_manifest(model_types: tuple[str, ...] = MODEL_TYPES) -> list[ModelFile]:
    """从已安装的 rapidocr 包内 default_models.yaml 读出要用的模型文件的地址和 SHA256（只读）。"""
    import yaml

    with open(_package_dir() / "default_models.yaml", encoding="utf-8") as f:
        info = yaml.safe_load(f)["onnxruntime"]
    out = []
    for (task, model), (ver, sect, key) in _YAML_KEYS.items():
        if task != "cls" and model not in model_types:
            continue
        item = info[ver][sect][key]
        url = str(item["model_dir"])
        out.append(ModelFile(task, model, url.rsplit("/", 1)[-1], url, str(item["SHA256"]).lower()))
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_VERIFIED: dict[tuple[str, int, int], str] = {}
_VERIFIED_LOCK = threading.Lock()


def _verify(path: Path, expected: str) -> None:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _VERIFIED_LOCK:
        got = _VERIFIED.get(key)
    if got is None:
        got = sha256_file(path)
        with _VERIFIED_LOCK:
            _VERIFIED[key] = got
    if got != expected:
        raise OcrModelsMissing(
            f"OCR 模型文件校验失败：{path.name}（SHA256 不符，文件可能损坏或被替换）。"
            f"请删除后重新运行：python -m screenrecall.text.ocr2 --download"
        )


def resolve_models(model: str, models_dir: Path) -> dict[str, Path]:
    """检查并返回 det / rec / cls 三个本地文件路径；缺失或校验失败时抛 OcrModelsMissing。"""
    if model not in MODEL_TYPES:
        raise ValueError(f"model 只能是 {MODEL_TYPES}，收到 {model!r}")
    files = {m.task: m for m in model_manifest((model,))}
    missing = [m.name for m in files.values() if not (models_dir / m.name).is_file()]
    if missing:
        raise OcrModelsMissing(
            f"缺少 OCR 模型文件：{'、'.join(missing)}（目录 {models_dir}）。"
            f"请先联网运行一次：python -m screenrecall.text.ocr2 --download"
        )
    out = {}
    for task, m in files.items():
        p = models_dir / m.name
        _verify(p, m.sha256)
        out[task] = p
    return out


def download(models_dir: Path = DEFAULT_MODELS_DIR, model_types: tuple[str, ...] = MODEL_TYPES,
             timeout_s: float = 120.0) -> list[str]:
    """联网下载 PP-OCRv6 tiny + small 的 det/rec（和方向分类器）到 models_dir，逐个校验 SHA256。

    已存在且校验通过的跳过；先写临时文件，校验通过再改名，失败不留半截文件。
    """
    import httpx

    models_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for m in model_manifest(model_types):
        dst = models_dir / m.name
        if dst.is_file() and sha256_file(dst) == m.sha256:
            report.append(f"已有 {m.name}")
            continue
        tmp = dst.with_name(dst.name + ".part")
        h = hashlib.sha256()
        try:
            with httpx.stream("GET", m.url, follow_redirects=True, timeout=timeout_s) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
                        h.update(chunk)
            if h.hexdigest() != m.sha256:
                raise OcrModelsMissing(f"下载的 {m.name} SHA256 不符（{h.hexdigest()[:12]}…），已丢弃")
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()
        report.append(f"下载 {m.name}（{dst.stat().st_size / 1e6:.1f} MB）")
    return report


# ---------- 引擎 ----------

@contextmanager
def _no_download():
    """构造 RapidOCR 期间禁止任何下载：本地文件已给全，若仍触发下载说明配置有误，直接报错。"""
    from rapidocr.utils.download_file import DownloadFile

    orig = DownloadFile.run

    def refuse(*_a, **_k):
        raise OcrModelsMissing("rapidocr 试图在运行时下载模型（已禁止）；请检查本地模型目录")

    DownloadFile.run = staticmethod(refuse)
    try:
        yield
    finally:
        DownloadFile.run = orig


_BUILD_LOCK = threading.Lock()


class OcrEngine:
    """PP-OCRv6 OCR 引擎。recognize 线程安全（内部串行）。

    model: 'tiny'（默认）或 'small'；threads: onnxruntime 推理线程数；
    max_side_len: 检测前最长边上限（与旧版 OcrConfig 一致，2000）；
    text_score: 低于此置信度的行丢弃（rapidocr 默认 0.5）。
    """

    def __init__(self, model: str = "tiny", *, threads: int = 2, models_dir: str | Path | None = None,
                 max_side_len: int = 2000, text_score: float = 0.5, crop_limit_type: str = "max",
                 frame_limit_type: str = "min"):
        self.model = model
        self.models_dir = Path(models_dir) if models_dir else DEFAULT_MODELS_DIR
        paths = resolve_models(model, self.models_dir)
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import ModelType, OCRVersion

        mt = ModelType(model)
        params = {
            "Global.log_level": "error",
            "Global.use_cls": False,
            "Global.max_side_len": int(max_side_len),
            "Global.text_score": float(text_score),
            "Global.model_root_dir": str(self.models_dir),
            "EngineConfig.onnxruntime.intra_op_num_threads": int(threads),
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_type": mt,
            "Det.model_path": str(paths["det"]),
            "Det.limit_type": frame_limit_type,
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_type": mt,
            "Rec.model_path": str(paths["rec"]),
            "Cls.model_path": str(paths["cls"]),
        }
        with _BUILD_LOCK, _no_download():
            self._engine = RapidOCR(params=params)
        # 字典必须来自模型自带的元数据；否则 rapidocr 会去 site-packages 找 / 下载字典
        if not self._engine.text_rec.session.have_key():
            raise OcrModelsMissing(f"识别模型 {paths['rec'].name} 不含字符表元数据")
        self._crop_limit = crop_limit_type
        self._frame_limit = frame_limit_type
        self._lock = threading.Lock()
        self._version = (
            f"rapidocr {rapidocr_version()} / {OCR_VERSION} det_{model}+rec_{model} "
            f"(cls off, crop limit={crop_limit_type})"
        )

    def version(self) -> str:
        return self._version

    def recognize(self, bgr: np.ndarray, *, crop: bool = False) -> list[OcrLine]:
        """识别一张 BGR（或 BGRA）uint8 图。crop=True 表示截图里裁出来的小块。"""
        img = _as_bgr(bgr)
        if img.shape[0] < 2 or img.shape[1] < 2:
            return []
        with self._lock:
            self._engine.text_det.limit_type = self._crop_limit if crop else self._frame_limit
            res = self._engine(img)
        txts = getattr(res, "txts", None)
        boxes = getattr(res, "boxes", None)
        if not txts or boxes is None:
            return []
        scores = res.scores or ()
        out = []
        for box, txt, sc in zip(boxes, txts, scores):
            quad = [[float(x), float(y)] for x, y in np.asarray(box).reshape(-1, 2)[:4]]
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            out.append(OcrLine(str(txt), float(sc), quad, min(xs), min(ys), max(xs), max(ys)))
        return out


def _as_bgr(img: np.ndarray) -> np.ndarray:
    a = np.asarray(img)
    if a.dtype != np.uint8:
        raise TypeError(f"需要 uint8 图像，收到 {a.dtype}")
    if a.ndim == 2:
        a = np.repeat(a[:, :, None], 3, axis=2)
    elif a.ndim == 3 and a.shape[2] == 4:
        a = a[:, :, :3]  # WGC 帧是 BGRA：丢掉 alpha
    elif a.ndim != 3 or a.shape[2] != 3:
        raise ValueError(f"不支持的图像形状 {a.shape}")
    return np.ascontiguousarray(a)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.text.ocr2")
    ap.add_argument("--download", action="store_true", help="联网下载 PP-OCRv6 tiny + small 模型并校验")
    ap.add_argument("--dir", default=str(DEFAULT_MODELS_DIR), help="模型目录（默认 <仓库>/models/ocr）")
    args = ap.parse_args(argv)
    d = Path(args.dir)
    if args.download:
        for line in download(d):
            print(line)
    bad = []
    for model in MODEL_TYPES:
        try:
            resolve_models(model, d)
        except OcrModelsMissing as exc:
            bad.append(str(exc))
    print("OCR 模型齐全" if not bad else "\n".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
