"""把一个任务文件夹里的截图拼成联系表，方便人工 / 代理一眼抽查遮挡、空白和重复（只读原图，不改它们）。

    .venv/bin/python datasets/contact_sheet.py data-dataset/raw/cao/cao-web-02 --out data-dataset/reports/sheets
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image, ImageDraw


def sheet(task_dir: Path, out_dir: Path, cols: int = 5, thumb_w: int = 360, only_new_from: int = 0) -> list[Path]:
    files = sorted(p for p in task_dir.glob("*.png")
                   if (m := re.search(r"_(\d{3})\.png$", p.name)) and int(m.group(1)) >= only_new_from)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages, per = [], cols * 4
    for k in range(0, len(files), per):
        chunk = files[k:k + per]
        thumbs = []
        for p in chunk:
            with Image.open(p) as im:
                im = im.convert("RGB")
                h = int(im.height * thumb_w / im.width)
                thumbs.append((p, im.resize((thumb_w, h))))
        th = max(t.height for _, t in thumbs) + 22
        rows = (len(thumbs) + cols - 1) // cols
        canvas = Image.new("RGB", (cols * (thumb_w + 8), rows * th), "white")
        d = ImageDraw.Draw(canvas)
        for i, (p, t) in enumerate(thumbs):
            x, y = (i % cols) * (thumb_w + 8), (i // cols) * th
            canvas.paste(t, (x, y + 20))
            d.text((x + 2, y + 4), re.search(r"_(\d{3})\.png$", p.name).group(1) + "  " + p.name[-30:-8], fill="black")
        dest = out_dir / f"{task_dir.name}-sheet{k // per + 1}.jpg"
        canvas.save(dest, quality=80)
        pages.append(dest)
    return pages


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dirs", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--from-seq", type=int, default=0, help="只拼编号不小于它的截图")
    a = ap.parse_args()
    for t in a.task_dirs:
        for p in sheet(Path(t), Path(a.out), only_new_from=a.from_seq):
            print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
