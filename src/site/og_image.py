"""Stage S Wave 5 (S5-2) — dynamic OpenGraph share-image for /apt/{id}.

`render_og_png(item)` composes a **1200x630** brand PNG for rich link previews (Telegram/WhatsApp):
the listing's first LOCAL photo (cover-cropped) under a dark scrim, with the price, the район, an
optional ВНЖ badge and the RentBelgrad wordmark. No photo / unreadable photo → a solid brand-paper
panel (never pure white). The function is intentionally crash-proof: any failure reading or
decoding the photo degrades to the paper panel rather than raising.

Privacy (DECISIONS-S D1): район ONLY. This module reads price/район/ВНЖ/photo and nothing that
identifies the exact building — no door-level location field ever reaches the canvas. (A privacy
test asserts the source never references those identifying fields.)

Fonts (cross-OS robustness): the brand faces are woff2, which Pillow cannot load. We try a short
list of common system TrueType paths first (nicer glyphs where present), then fall back to
`ImageFont.load_default(size=N)` — Pillow ≥10 returns a *scalable* default, so text is legible at
brand sizes on both dev macOS and prod Linux with zero bundled font files. Exact brand typeface is
a future polish.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.config import get_settings

# A sized font is either a TrueType face (system path) or Pillow's scalable default. Both expose
# the textbbox/draw API we use; the union keeps mypy --strict happy across the fallback.
_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont

# Canonical OpenGraph image box (Telegram/WhatsApp/Twitter all crop to ~1.91:1).
OG_W = 1200
OG_H = 630

# «Belgrade Editorial» palette (design system). Paper is never pure white.
PAPER = (244, 239, 230)  # #F4EFE6
OXBLOOD = (168, 54, 42)  # #A8362A
NAVY = (21, 50, 76)  # #15324C
OCHRE = (216, 154, 78)  # #D89A4E
WHITE = (255, 255, 255)

# A scrim over the photo so light/busy images keep the text legible.
_SCRIM = (21, 50, 76)  # navy, applied at partial alpha along the bottom

# Common system TrueType candidates (dev macOS + prod Linux). Optional — load_default is the
# guaranteed fallback. Bold for the price, regular for the rest.
_FONT_CANDIDATES_BOLD = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_FONT_CANDIDATES_REG = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def _font(size: int, *, bold: bool = False) -> _Font:
    """A sized font: try system TrueType, else Pillow's scalable default (Pillow ≥10).

    Never raises and never hard-depends on a bundled face — the default keeps prod Linux legible
    even when no system TTF is present.
    """
    candidates = _FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _photo_file(photos: list[str]) -> Path | None:
    """Map the first `/photos/<relpath>` URL back to a real file under the photos root.

    Returns None when there's no photo, the URL isn't a local /photos path, or the file is absent.
    Defense: reject any `..` segment so a tainted relpath can't escape the root.
    """
    if not photos:
        return None
    url = photos[0]
    if not url.startswith("/photos/"):
        return None
    rel = url[len("/photos/") :]
    if not rel or ".." in rel.split("/"):
        return None
    root = Path(get_settings().site_photos_root)
    candidate = root / rel
    try:
        if candidate.is_file():
            from src.watermark.store import site_source

            return site_source(candidate)
    except OSError:
        return None
    return None


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Resize+center-crop `img` to exactly wxh (CSS object-fit: cover)."""
    img = img.convert("RGB")
    src_w, src_h = img.size
    if src_w == 0 or src_h == 0:
        return Image.new("RGB", (w, h), PAPER)
    scale = max(w / src_w, h / src_h)
    new_w, new_h = max(int(src_w * scale), w), max(int(src_h * scale), h)
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _base_canvas(photos: list[str]) -> Image.Image:
    """The background: cover-cropped first photo under a bottom scrim, else a paper panel.

    Any photo failure (missing/corrupt/unreadable) silently falls back to the paper panel — the
    image must always render.
    """
    path = _photo_file(photos)
    if path is not None:
        try:
            with Image.open(path) as raw:
                raw.load()
                photo = _cover(raw, OG_W, OG_H)
            return _apply_scrim(photo)
        except (OSError, ValueError, Image.DecompressionBombError):
            pass  # fall through to the paper panel
    return _paper_panel()


def _paper_panel() -> Image.Image:
    """Brand-paper background with a subtle oxblood top rule (no photo case)."""
    canvas = Image.new("RGB", (OG_W, OG_H), PAPER)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, OG_W, 12), fill=OXBLOOD)  # editorial top rule
    return canvas


def _apply_scrim(photo: Image.Image) -> Image.Image:
    """Darken the lower portion of a photo so overlaid text stays legible."""
    scrim = Image.new("RGBA", (OG_W, OG_H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    # Gradient-ish scrim: a few stacked bands from transparent → navy toward the bottom.
    band_h = OG_H // 2
    for i in range(band_h):
        alpha = int(200 * (i / band_h))
        y = OG_H - band_h + i
        sdraw.line((0, y, OG_W, y), fill=(*_SCRIM, alpha))
    sdraw.rectangle((0, 0, OG_W, 10), fill=(*OXBLOOD, 255))  # top rule
    return Image.alpha_composite(photo.convert("RGBA"), scrim).convert("RGB")


def _rounded_badge(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: _Font,
    *, fill: tuple[int, int, int], fg: tuple[int, int, int], pad: int = 18,
) -> int:
    """Draw a pill badge at (x, y); return its right edge x for chaining."""
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])
    w, h = tw + pad * 2, th + pad
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)
    draw.text((x + pad, y + (h - th) // 2 - int(bbox[1])), text, font=font, fill=fg)
    return x + w


def render_og_png(item) -> bytes:  # type: ignore[no-untyped-def]
    """Compose the 1200x630 OpenGraph PNG for a listing view.

    Consumes ONLY privacy-safe fields: price_eur, district (район), for_vnj, and the first local
    photo. No door-level location field is ever read or drawn (D1). Returns raw PNG bytes.
    """
    canvas = _base_canvas(item.photos)
    draw = ImageDraw.Draw(canvas)

    on_photo = _photo_file(item.photos) is not None
    price_color = WHITE if on_photo else OXBLOOD
    district_color = (235, 235, 235) if on_photo else NAVY

    margin = 64
    # Wordmark (top-left).
    wm_font = _font(34, bold=True)
    draw.text((margin, 40), "RentBelgrad", font=wm_font, fill=WHITE if on_photo else NAVY)

    # Price (big, bottom-left) — район beneath it.
    price_font = _font(118, bold=True)
    price_text = f"€{item.price_eur}"
    pbbox = draw.textbbox((0, 0), price_text, font=price_font)
    price_top = int(pbbox[1])
    price_h = int(pbbox[3] - pbbox[1])
    price_w = int(pbbox[2] - pbbox[0])
    price_y = OG_H - margin - price_h - 70
    draw.text((margin, price_y - price_top), price_text, font=price_font, fill=price_color)

    # «/мес» suffix, smaller, baseline-aligned to the price.
    per_font = _font(40, bold=False)
    draw.text(
        (margin + price_w + 16, price_y + price_h - 44),
        "/мес",
        font=per_font,
        fill=district_color,
    )

    # Район (the neighbourhood label ONLY — no door-level location field, per D1).
    district_font = _font(52, bold=False)
    district = item.district or "Белград"
    draw.text((margin, OG_H - margin - 56), district, font=district_font, fill=district_color)

    # ВНЖ badge (top-right), only when the listing is flagged.
    if item.for_vnj:
        badge_font = _font(34, bold=True)
        label = "ВНЖ"
        bbox = draw.textbbox((0, 0), label, font=badge_font)
        bw = int(bbox[2] - bbox[0]) + 36 * 2
        _rounded_badge(
            draw, (OG_W - margin - bw, 40), label, badge_font, fill=OCHRE, fg=NAVY, pad=36
        )

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
