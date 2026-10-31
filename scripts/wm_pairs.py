"""QA pairs for the watermark remover: one lossless PNG per detected mark.

For every photo in a folder the script runs the production pipeline (`clean_array`) and writes,
for each found mark, ``<photo>__<kind>#<i>.png``: the full frame before | after (top) and a 2x
crop around the mark with a 48 px margin before | after (bottom; stacked when the mark is wide).
It also writes ``index.csv`` (photo, instance, kind, pair, method, kept_reason, score, residual,
seconds) -- no quality grades, so a blind reviewer can use the folder as is.

Usage (corner mode is read from the settings, so pass it explicitly):

    PYTHONPATH=$PWD $PY scripts/wm_pairs.py <photos-folder> <out-dir> --corner-mode skip|lama|telea

The folder with originals is only read; ``--out-dir`` must lie outside it.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
CROP_MARGIN = 48
CROP_ZOOM = 2
FULL_SIDE = 900  # px: width of each full-frame panel
MAX_ROW = 3200  # px: wider crop rows are stacked (before above after)
GAP = 10


def _panel(arr, width: int):  # type: ignore[no-untyped-def]
    from PIL import Image

    im = Image.fromarray(arr)
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
    return im


def _mark_box(tpl_size: tuple[int, int], mark, shape_hw) -> tuple[int, int, int, int]:  # type: ignore[no-untyped-def]
    h, w = shape_hw
    tw, th = tpl_size
    x0 = max(0, int(mark.x) - CROP_MARGIN)
    y0 = max(0, int(mark.y) - CROP_MARGIN)
    x1 = min(w, int(mark.x + tw * mark.scale) + CROP_MARGIN)
    y1 = min(h, int(mark.y + th * mark.scale) + CROP_MARGIN)
    return x0, y0, x1, y1


def build_pair(before, after, box):  # type: ignore[no-untyped-def]
    """One PNG canvas: full frame before | after over the 2x crop before | after."""
    from PIL import Image

    x0, y0, x1, y1 = box
    full_b, full_a = _panel(before, FULL_SIDE), _panel(after, FULL_SIDE)
    f = full_b.width / before.shape[1]
    # outline the crop on the "before" panel so the reader sees where the zoom is taken from
    from PIL import ImageDraw

    ImageDraw.Draw(full_b).rectangle(
        [x0 * f, y0 * f, x1 * f, y1 * f], outline=(255, 0, 0), width=2
    )
    top_w = full_b.width + GAP + full_a.width
    cw, ch = (x1 - x0) * CROP_ZOOM, (y1 - y0) * CROP_ZOOM
    crop_b = Image.fromarray(before[y0:y1, x0:x1]).resize((cw, ch), Image.NEAREST)
    crop_a = Image.fromarray(after[y0:y1, x0:x1]).resize((cw, ch), Image.NEAREST)
    if 2 * cw + GAP <= MAX_ROW:
        bottom_w, bottom_h = 2 * cw + GAP, ch
        place = [(crop_b, (0, 0)), (crop_a, (cw + GAP, 0))]
    else:
        bottom_w, bottom_h = cw, 2 * ch + GAP
        place = [(crop_b, (0, 0)), (crop_a, (0, ch + GAP))]
    width = max(top_w, bottom_w)
    canvas = Image.new("RGB", (width, full_b.height + GAP + bottom_h), "white")
    canvas.paste(full_b, (0, 0))
    canvas.paste(full_a, (full_b.width + GAP, 0))
    for img, (px, py) in place:
        canvas.paste(img, (px, full_b.height + GAP + py))
    return canvas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("folder", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--corner-mode", choices=["skip", "telea", "lama"], default="skip")
    parser.add_argument("--only", default="", help="substring filter on the photo path")
    parser.add_argument(
        "--kinds", default="", help="comma-separated template ids: write pairs only for these"
    )
    parser.add_argument(
        "--torch-threads", type=int, default=0, help="torch.set_num_threads (0 = leave as is)"
    )
    parser.add_argument(
        "--save-cleaned", action="store_true", help="also write cleaned full frames (PNG)"
    )
    args = parser.parse_args(argv)
    folder, out_dir = args.folder.resolve(), args.out_dir.resolve()
    if out_dir.is_relative_to(folder):
        parser.error("out_dir must lie outside the input folder")
    os.environ["WATERMARK_HALO_CORNER_MODE"] = args.corner_mode
    if args.torch_threads:
        import torch

        torch.set_num_threads(args.torch_threads)

    from PIL import Image

    from src.watermark.pipeline import NotAnImageError, clean_array, decode_image
    from src.watermark.templates import load_templates

    sizes = {t.id: (t.width, t.height) for t in load_templates()}
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(
        p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES and args.only in str(p)
    )
    rows: list[list[str]] = []
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    for src in files:
        rel = src.relative_to(folder).as_posix()
        try:
            img = decode_image(src.read_bytes())
        except NotAnImageError:
            print(f"{rel}: not an image, skipped")
            continue
        cleaned, report = clean_array(img)
        stem = Path(rel).stem
        if args.save_cleaned:
            (out_dir / "cleaned").mkdir(exist_ok=True)
            Image.fromarray(cleaned).save(out_dir / "cleaned" / f"{stem}.png")
        if not report.marks and not kinds:
            rows.append([rel, "", "", "", "", "", "", "", f"{report.seconds:.2f}"])
        for m in report.marks:
            if kinds and m.template_id not in kinds:
                continue
            name = f"{stem}__{m.template_id}#{m.instance}.png"
            box = _mark_box(sizes[m.template_id], m, img.shape[:2])
            build_pair(img, cleaned, box).save(out_dir / name)
            rows.append(
                [rel, str(m.instance), m.template_id, name, m.method,
                 str(getattr(m, "kept_reason", None) or ""), f"{m.score:.3f}",
                 f"{m.residual:.3f}", f"{report.seconds:.2f}"]
            )
        marks = "; ".join(f"{m.template_id}#{m.instance}:{m.method}" for m in report.marks)
        print(f"{rel}: {marks or 'no marks'} ({report.seconds:.2f}s)", flush=True)
    with (out_dir / "index.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["photo", "instance", "kind", "pair", "method", "kept_reason", "score", "residual",
             "seconds"]
        )
        writer.writerows(rows)
    print(f"=== {len(files)} photos, {sum(1 for r in rows if r[2])} marks -> {out_dir} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
