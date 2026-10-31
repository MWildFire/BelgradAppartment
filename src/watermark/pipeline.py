"""Public API: find listing-platform marks on a photo and remove them.

Steps: decode (with EXIF rotation) -> for every template instance find the best match near its
predicted place -> keep matches that pass the template threshold -> remove each mark with its
template strategy -> measure the residual. The original is never modified: functions return a
new image or write a new file.
"""

from __future__ import annotations

import io
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from src.watermark.detect import Detection, match_instance, score_at, to_gray
from src.watermark.files import atomic_write
from src.watermark.inpaint import Inpainter, get_inpainter
from src.watermark.refine import remove_refined
from src.watermark.remove import remove_inpaint, remove_reverse_blend
from src.watermark.selfcheck import mask_too_large, removal_failed
from src.watermark.settings import get_watermark_settings
from src.watermark.templates import WatermarkTemplate, load_templates

logger = logging.getLogger(__name__)

PLATFORM_TITLES = {"halooglasi": "Halo Oglasi", "4zida": "4zida", "cityexpert": "CityExpert"}
DEFAULT_JPEG_QUALITY = 95
# A real reverse-blend mark fits k at 0.94-1.00; a false match hits the 0.60 lower bound.
MIN_PLAUSIBLE_K = 0.7
MAX_PIXELS = 40_000_000  # decompression-bomb guard: Telegram <= 2560 px, platforms <= 1920 px


class NotAnImageError(ValueError):
    """Bytes do not decode as an image, or the image is unreasonably large."""


@dataclass(frozen=True)
class MarkResult:
    template_id: str
    platform: str
    instance: int
    score: float
    x: float
    y: float
    scale: float
    method: str  # "reverse_blend" | "inpaint:lama" | "inpaint:telea" | "skip"
    residual: float  # correlation at the same place after removal
    # Only for method == "skip" (mark found, pixels untouched): corner_mode | disabled |
    # self_check | lama_unavailable. None for removed marks and for sidecars written before WM05.
    kept_reason: str | None = None


@dataclass
class CleanReport:
    width: int
    height: int
    marks: list[MarkResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return any(mark.method != "skip" for mark in self.marks)

    def platform_counts(self) -> Counter[str]:
        return Counter(PLATFORM_TITLES.get(m.platform, m.platform) for m in self.marks)

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "seconds": round(self.seconds, 3),
            "marks": [m.__dict__ for m in self.marks],
        }


def decode_image(data: bytes) -> np.ndarray:
    """RGB uint8 with EXIF orientation applied."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise NotAnImageError(f"image too large: {im.width}x{im.height}")
            rgb = ImageOps.exif_transpose(im).convert("RGB")
            return np.asarray(rgb, dtype=np.uint8).copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise NotAnImageError(str(error)) from error


def encode_jpeg(img_rgb: np.ndarray, quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img_rgb).save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def max_offset_px(image_width: int) -> float:
    """Allowed distance from the prediction (real marks are within 1.5 px)."""
    return 2.0 + 0.004 * image_width


def is_confident(tpl: WatermarkTemplate, det: Detection, image_width: int) -> bool:
    limit = max_offset_px(image_width)
    return det.score >= tpl.min_score and abs(det.dx) <= limit and abs(det.dy) <= limit


def find_marks(
    img_rgb: np.ndarray, templates: tuple[WatermarkTemplate, ...] | None = None
) -> list[tuple[WatermarkTemplate, Detection]]:
    gray = to_gray(img_rgb)
    h, w = gray.shape
    found: list[tuple[WatermarkTemplate, Detection]] = []
    for tpl in templates if templates is not None else load_templates():
        for placement in tpl.predicted(w, h):
            det = match_instance(gray, tpl, placement)
            if det is not None and is_confident(tpl, det, w):
                found.append((tpl, det))
    return found


class Removal(NamedTuple):
    """How one found mark is treated: a method, its inpainter and, for a kept mark, why."""

    method: str
    inpainter: Inpainter | None
    kept_reason: str | None = None


def plan_removal(tpl: WatermarkTemplate) -> Removal:
    """Decide the treatment of a template.

    A mark that needs LaMa while LaMa is missing is kept (``skip`` + ``lama_unavailable``);
    classic (Telea-style) filling happens only when the operator asks for it explicitly with
    ``WATERMARK_HALO_CORNER_MODE=telea``. A template that declares ``lama_fallback:
    reverse_blend`` keeps that deterministic fallback. A kind listed in
    ``WATERMARK_DISABLED_KINDS`` is found but never removed (``disabled``).
    """
    if tpl.id in get_watermark_settings().disabled_kinds():
        return Removal("skip", None, "disabled")
    if tpl.id == "halo_corner":
        mode = get_watermark_settings().watermark_halo_corner_mode
        if mode == "skip":
            return Removal("skip", None, "corner_mode")
        if mode == "lama":
            from src.watermark.lama_corner import get_lama_corner_inpainter

            corner = get_lama_corner_inpainter()
            if corner is None:
                logger.warning("[watermark] corner LaMa unavailable; keeping corner (skip)")
                return Removal("skip", None, "lama_unavailable")
            return Removal("inpaint:lama", corner)
    if tpl.strategy == "reverse_blend":
        return Removal("reverse_blend", None)
    inpainter = get_inpainter(tpl.inpaint_backend)
    if inpainter is None:
        if tpl.lama_fallback == "reverse_blend":
            return Removal("reverse_blend", None)
        return Removal("skip", None, "lama_unavailable")
    return Removal(f"inpaint:{inpainter.name}", inpainter)


def removal_for(tpl: WatermarkTemplate) -> tuple[str, Inpainter | None]:
    """(method name, inpainter) for a template; see `plan_removal` for the kept reason."""
    plan = plan_removal(tpl)
    return plan.method, plan.inpainter


def _remove_mark(
    out: np.ndarray, tpl: WatermarkTemplate, det: Detection, plan: Removal
) -> tuple[np.ndarray, str, str | None, float] | None:
    """Apply one planned removal: (image, method, kept_reason, residual) or None.

    None means "not a real mark" (a reverse-blend fit that stops at its lower bound): the photo
    and the report stay as they are. A mark the self-checks reject (LaMa mask too large, or the
    template still correlates after removal) is kept: the input image comes back unchanged.
    """
    if plan.method == "skip":
        return out, "skip", plan.kept_reason, score_at(to_gray(out), tpl, det)
    if plan.inpainter is None:
        remover = remove_refined if tpl.refine else remove_reverse_blend
        candidate, k = remover(out, tpl, det)
        if k <= MIN_PLAUSIBLE_K:
            return None
    else:
        grow = tpl.mask_grow_px(det.scale)
        if mask_too_large(tpl, det, out.shape[:2], grow):
            return out, "skip", "self_check", score_at(to_gray(out), tpl, det)
        candidate = remove_inpaint(out, tpl, det, plan.inpainter, grow_px=grow)
    residual = score_at(to_gray(candidate), tpl, det)
    if removal_failed(tpl, residual):
        return out, "skip", "self_check", score_at(to_gray(out), tpl, det)
    return candidate, plan.method, None, residual


def clean_array(
    img_rgb: np.ndarray, templates: tuple[WatermarkTemplate, ...] | None = None
) -> tuple[np.ndarray, CleanReport]:
    started = time.monotonic()
    h, w = img_rgb.shape[:2]
    report = CleanReport(width=w, height=h)
    out = img_rgb
    for tpl, det in find_marks(img_rgb, templates):
        removed = _remove_mark(out, tpl, det, plan_removal(tpl))
        if removed is None:
            continue  # not a real mark: leave the photo and the report as they are
        out, method, kept_reason, residual = removed
        report.marks.append(
            MarkResult(
                template_id=tpl.id,
                platform=tpl.platform,
                instance=det.instance,
                score=round(det.score, 4),
                x=round(det.x, 2),
                y=round(det.y, 2),
                scale=round(det.scale, 5),
                method=method,
                residual=round(residual, 4),
                kept_reason=kept_reason,
            )
        )
    report.seconds = time.monotonic() - started
    return (out if report.changed else img_rgb.copy()), report


def clean_bytes(data: bytes, *, quality: int = DEFAULT_JPEG_QUALITY) -> tuple[bytes, CleanReport]:
    """Photo bytes -> (JPEG without marks, report); unmarked input is returned unchanged."""
    img = decode_image(data)
    out, report = clean_array(img)
    return (encode_jpeg(out, quality) if report.changed else data), report


def clean_file(src: Path, dst: Path, *, quality: int = DEFAULT_JPEG_QUALITY) -> CleanReport:
    """Write dst (JPEG) atomically only when a mark was removed; src is never touched."""
    if dst.resolve() == src.resolve():
        raise ValueError(f"refusing to overwrite the original: {src}")
    img = decode_image(src.read_bytes())
    out, report = clean_array(img)
    if report.changed:
        atomic_write(dst, encode_jpeg(out, quality))
    return report
