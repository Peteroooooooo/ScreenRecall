"""WebP 编码对照（方案 5.1 末段、6.3「存储与可用性」）：python -m screenrecall.codec_compare --images ... --out ...

对一组**无损**来源截图（PNG，或无损 WebP，例如用 webp_lossless=true 临时录 30 分钟的数据目录 frames/），
逐张用各候选参数编码（默认 q90 / q92 / q95 / 无损，method 取 config），比较：
  - 文件大小（P50 / P95 / 平均）与按 10 GiB 配额估算的可保存帧数
  - 编码耗时 P50 / P95（每张重复 --repeat 次，取单次耗时）
  - 与原始像素的差异：PSNR、最大通道差、改变像素比例；无损必须逐像素一致
  - 关键 OCR 字符是否变化（需真实 RapidOCR，--no-ocr 跳过）：各编码 OCR 全文相对原图 OCR 的字符错误率，
    以及数字串是否增减（数字 / 金额 / 版本号最怕有损编码改字）
  - 小字可读性留给人看：--crops N 张图各取文字最密的一块，原图 / 各编码并排放大 3 倍存成 PNG

输出 results.json（逐张）、summary.json 与 summary.md。数字只代表这批来源图和这台机器；Mac 截图是 2 倍分辨率，
Windows 100% 缩放下字更小，最终参数要用 Windows 实机画面再跑一次。
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .bench import summary

QUOTA_BYTES = 10 * 1024 ** 3
SUFFIXES = {".png", ".webp"}


def is_lossless_source(path: Path) -> bool:
    """PNG 一律当无损；WebP 只认 VP8L（无损）位流，有损 WebP 不能作为"原始像素"。"""
    if path.suffix.lower() == ".png":
        return True
    head = path.read_bytes()[:4096]
    if head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return False
    return b"VP8L" in head[12:64] or (head[12:16] == b"VP8X" and b"VP8L" in head)


def parse_variants(text: str) -> list[tuple[str, int, bool]]:
    out = []
    for v in (x.strip() for x in text.split(",") if x.strip()):
        if v == "lossless":
            out.append(("lossless", 100, True))
        elif re.fullmatch(r"q\d{1,3}", v) and 0 <= int(v[1:]) <= 100:
            out.append((v, int(v[1:]), False))
        else:
            raise ValueError(f"无法识别的编码参数 {v!r}，用 q90 / q92 / lossless 这样的写法")
    return out


def encode(rgb: Image.Image, quality: int, lossless: bool, method: int) -> tuple[bytes, float]:
    t0 = time.perf_counter()
    buf = io.BytesIO()
    rgb.save(buf, format="WEBP", quality=quality, lossless=lossless, method=method)
    return buf.getvalue(), (time.perf_counter() - t0) * 1000


def pixel_diff(a: np.ndarray, b: np.ndarray) -> dict:
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    mse = float(np.mean(d.astype(np.float64) ** 2))
    return {"max_diff": int(d.max()), "changed_px": round(float(np.mean(d.max(axis=2) > 0)), 6),
            "psnr": None if mse == 0 else round(10 * np.log10(255 ** 2 / mse), 2)}


def cer(ref: str, hyp: str) -> float:
    """字符错误率（Levenshtein / len(ref)），忽略空白。"""
    r, h = re.sub(r"\s+", "", ref), re.sub(r"\s+", "", hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, cr in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, ch in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cr != ch))
        prev = cur
    return round(prev[-1] / len(r), 5)


def digit_tokens(text: str) -> Counter:
    """数字串（含小数点、逗号、冒号、斜杠、连字符）。先去掉空白：OCR 对 "1, 2, 3" 和 "1,2,3" 的空格识别不稳定，
    不应算作数字变化。"""
    return Counter(re.findall(r"\d[\d.,:/-]*\d|\d", re.sub(r"\s+", "", text)))


def densest_crop(gray: np.ndarray, w: int = 320, h: int = 200) -> tuple[int, int]:
    """文字最密的一块：水平梯度能量最大的窗口（步长取窗口一半）。"""
    g = np.abs(np.diff(gray.astype(np.int16), axis=1))
    best, pos = -1.0, (0, 0)
    H, W = g.shape
    for y in range(0, max(1, H - h), h // 2):
        for x in range(0, max(1, W - w), w // 2):
            e = float(g[y:y + h, x:x + w].mean())
            if e > best:
                best, pos = e, (x, y)
    return pos


def pick_images(roots: list[Path], sample: int, seed: int) -> list[Path]:
    """按子文件夹分层抽样，避免一个任务占满样本。"""
    files = sorted(p for r in roots for p in (r.rglob("*") if r.is_dir() else [r])
                   if p.is_file() and p.suffix.lower() in SUFFIXES and not p.name.startswith("."))
    if not sample or sample >= len(files):
        return files
    rng = random.Random(seed)
    groups: dict[Path, list[Path]] = {}
    for p in files:
        groups.setdefault(p.parent, []).append(p)
    for g in groups.values():
        rng.shuffle(g)
    out, keys = [], sorted(groups)
    while len(out) < sample:
        for k in keys:
            if groups[k] and len(out) < sample:
                out.append(groups[k].pop())
    return sorted(out)


def compare(paths: list[Path], variants, *, method: int, repeat: int, ocr=None, crops: int = 0,
            crop_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    rows, skipped = [], []
    for n, path in enumerate(paths):
        if not is_lossless_source(path):
            skipped.append(f"{path}：不是无损来源（有损 WebP 不能当原始像素）")
            continue
        with Image.open(path) as im:
            src = im.convert("RGB")
        a = np.asarray(src)
        rec = {"image": str(path), "width": src.width, "height": src.height, "raw_bytes": a.size,
               "source_bytes": path.stat().st_size, "variants": {}}
        ref_text = None
        if ocr is not None:
            ref_text = "\n".join(b.text for b in ocr.recognize(src))
            rec["ocr_chars"] = len(ref_text)
        shown = [src]
        for name, q, lossless in variants:
            payload, ms = None, []
            for _ in range(max(1, repeat)):
                payload, t = encode(src, q, lossless, method)
                ms.append(t)
            with Image.open(io.BytesIO(payload)) as im:
                dec = im.convert("RGB")
            v = {"bytes": len(payload), "encode_ms": [round(x, 2) for x in ms], **pixel_diff(a, np.asarray(dec))}
            if ref_text is not None:
                hyp = "\n".join(b.text for b in ocr.recognize(dec))
                rd, hd = digit_tokens(ref_text), digit_tokens(hyp)
                v.update(ocr_cer=cer(ref_text, hyp), ocr_identical=(hyp == ref_text),
                         digits_lost=sorted((rd - hd).elements()), digits_new=sorted((hd - rd).elements()))
            rec["variants"][name] = v
            shown.append(dec)
        rows.append(rec)
        if crop_dir is not None and n < crops:
            x, y = densest_crop(np.asarray(src.convert("L")))
            tiles = [s.crop((x, y, x + 320, y + 200)).resize((960, 600), Image.Resampling.NEAREST) for s in shown]
            sheet = Image.new("RGB", (960 * len(tiles), 640), "white")
            draw = ImageDraw.Draw(sheet)
            for i, (t, name) in enumerate(zip(tiles, ["original"] + [v[0] for v in variants])):
                sheet.paste(t, (960 * i, 40))
                draw.text((960 * i + 10, 12), f"{name}  (crop x={x} y={y}, 3x)", fill=(200, 0, 0))
            crop_dir.mkdir(parents=True, exist_ok=True)
            sheet.save(crop_dir / f"{n:03d}-{path.stem}.png")
    return rows, skipped


def summarize(rows: list[dict], variants, *, method: int) -> dict:
    out = {"images": len(rows), "method": method, "variants": {}}
    for name, q, lossless in variants:
        vs = [r["variants"][name] for r in rows]
        sizes = [v["bytes"] for v in vs]
        single = [min(v["encode_ms"]) for v in vs]
        s = {"quality": q, "lossless": lossless, "bytes": summary(sizes), "encode_ms": summary(single),
             "frames_in_10GiB": int(QUOTA_BYTES / (sum(sizes) / len(sizes))) if sizes else None,
             "ratio_to_raw": round(sum(sizes) / sum(r["raw_bytes"] for r in rows), 4) if rows else None,
             "max_diff": max((v["max_diff"] for v in vs), default=None),
             "psnr_min": min((v["psnr"] for v in vs if v["psnr"] is not None), default=None),
             "pixel_identical": sum(v["max_diff"] == 0 for v in vs)}
        if vs and "ocr_cer" in vs[0]:
            s.update(ocr_cer=summary([v["ocr_cer"] for v in vs]),
                     ocr_identical=sum(v["ocr_identical"] for v in vs),
                     images_digits_changed=sum(bool(v["digits_lost"] or v["digits_new"]) for v in vs),
                     digit_examples=[{"image": Path(r["image"]).name, "lost": v["digits_lost"][:5],
                                      "new": v["digits_new"][:5]}
                                     for r, v in zip(rows, vs) if v["digits_lost"] or v["digits_new"]][:10])
        out["variants"][name] = s
    return out


def to_markdown(s: dict, note: str) -> str:
    def kib(x):
        return "—" if x is None else f"{x / 1024:.0f}"

    ocr = any("ocr_cer" in v for v in s["variants"].values())
    head = "| 参数 | 平均 KiB | P50 / P95 KiB | 编码 P50 / P95 ms | 10 GiB 约可存帧 | 最小 PSNR | 逐像素一致 |"
    sep = "| --- | --- | --- | --- | --- | --- | --- |"
    if ocr:
        head += " OCR 全文不变 | 数字有变化的图 | OCR CER P95 |"
        sep += " --- | --- | --- |"
    lines = [f"# WebP 编码对照（{s['images']} 张无损来源，method={s['method']}）", "", note, "", head, sep]
    for name, v in s["variants"].items():
        b, e = v["bytes"], v["encode_ms"]
        row = (f"| {name} | {kib(b['mean'])} | {kib(b['p50'])} / {kib(b['p95'])} | "
               f"{e['p50']:.1f} / {e['p95']:.1f} | {v['frames_in_10GiB']:,} | "
               f"{'∞' if v['psnr_min'] is None else v['psnr_min']} | {v['pixel_identical']}/{s['images']} |")
        if ocr:
            row += (f" {v['ocr_identical']}/{s['images']} | {v['images_digits_changed']} | "
                    f"{v['ocr_cer']['p95']:.4f} |")
        lines.append(row)
    lines += ["", "OCR 全文不变不等于可读性没问题；小字请看 crops/ 里的并排放大图（原图在最左）。"
              "OCR 本身对同一张图是确定的，但有损编码的轻微变化可能让 OCR 在原图上本来就认错的字上换一种错法，"
              "数字变化要回到原图人工核对是不是编码造成的。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m screenrecall.codec_compare", description=__doc__.split("\n\n")[0])
    ap.add_argument("--images", nargs="+", required=True, help="无损来源截图的文件或文件夹（递归）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-dir", help="读取 config 里的 webp_method 和 OCR 线程设置")
    ap.add_argument("--variants", default="q90,q92,q95,lossless")
    ap.add_argument("--method", type=int, help="WebP method 0–6；默认取 config")
    ap.add_argument("--sample", type=int, default=120, help="按文件夹分层抽多少张（0 = 全部）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--crops", type=int, default=12)
    ap.add_argument("--note", default="", help="写进报告的来源说明，例如机器、缩放比例、采集方式")
    args = ap.parse_args(argv)

    from .config import load_config

    cfg = load_config(args.data_dir)
    method = cfg.capture.webp_method if args.method is None else args.method
    variants = parse_variants(args.variants)
    paths = pick_images([Path(p) for p in args.images], args.sample, args.seed)
    if not paths:
        print("错误：没有找到 PNG / WebP 来源图", file=sys.stderr)
        return 1
    ocr = None
    if not args.no_ocr:
        from .ocr import OcrEngine

        ocr = OcrEngine(cfg.ocr)
    out = Path(args.out)
    rows, skipped = compare(paths, variants, method=method, repeat=args.repeat, ocr=ocr, crops=args.crops,
                            crop_dir=out / "crops")
    if not rows:
        print("错误：没有可用的无损来源：" + "；".join(skipped[:3]), file=sys.stderr)
        return 1
    s = summarize(rows, variants, method=method)
    s["skipped"] = skipped
    s["sources"] = [str(p) for p in args.images]
    note = args.note or "来源：" + "、".join(str(p) for p in args.images)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(to_markdown(s, note), encoding="utf-8")
    print(f"{len(rows)} 张（跳过 {len(skipped)}），报告 {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
