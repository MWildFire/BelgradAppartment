"""Watermark templates: coverage (PNG, 0..255) + placement geometry (JSON).

Mark model: observed pixel J = a*255 + (1 - a)*O, where a = opacity[c] * coverage(x, y) and
O is the original pixel. Marks are white; opacity is per channel (R, G, B).

Geometry is expressed in the template's reference pixels. For scale_mode="width" the mark
scale in an image of width W is s = W / ref_width (CityExpert, 4zida and the big Halo Oglasi
logo behave like this). For scale_mode="fixed" s = 1.0 (the Halo Oglasi corner text has a
fixed pixel size). Top-left corner of an instance: (anchor_x + dx * s, anchor_y + dy * s),
where the anchor is the image center, top-left or bottom-right corner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

Anchor = Literal["center", "top-left", "bottom-right"]
Strategy = Literal["reverse_blend", "inpaint"]
Backend = Literal["lama", "telea"]
LamaFallback = Literal["telea", "reverse_blend"]
ScaleMode = Literal["width", "fixed"]

_ANCHORS: frozenset[str] = frozenset({"center", "top-left", "bottom-right"})
_STRATEGIES: frozenset[str] = frozenset({"reverse_blend", "inpaint"})
_BACKENDS: frozenset[str] = frozenset({"lama", "telea"})
_LAMA_FALLBACKS: frozenset[str] = frozenset({"telea", "reverse_blend"})
_SCALE_MODES: frozenset[str] = frozenset({"width", "fixed"})


class TemplateError(ValueError):
    """Broken or incomplete watermark template."""


@dataclass(frozen=True)
class Instance:
    """One instance of a mark on the image (4zida has two)."""

    anchor: Anchor
    dx: float
    dy: float


@dataclass(frozen=True)
class Placement:
    """Predicted instance position: top-left corner and scale."""

    instance: int
    x: float
    y: float
    scale: float


@dataclass(frozen=True, eq=False)
class WatermarkTemplate:
    id: str
    platform: str
    title: str
    coverage: np.ndarray  # float32, HxW, 0..1
    opacity: tuple[float, float, float]
    strategy: Strategy
    inpaint_backend: Backend
    ref_width: int
    scale_mode: ScaleMode
    instances: tuple[Instance, ...]
    scale_search: tuple[float, ...]
    pos_tol: float
    min_score: float
    # What to do when inpaint_backend is "lama" but LaMa is unavailable (no torch/model).
    lama_fallback: LamaFallback = "telea"
    # Quality levers (WM05). All optional in the JSON; the defaults keep the R04 behaviour.
    # reverse_blend only: fit every part of the mark (shift, size, edge blur, opacity) and skip
    # the Telea band along the contour (src/watermark/refine.py).
    refine: bool = False
    # LaMa marks: when the inpainting mask covers more than this fraction of the photo, LaMa
    # invents scenery instead of filling, so the mark is kept (kept_reason="self_check").
    lama_max_mask_fraction: float | None = None
    # Inpainting mask = mark support grown by round(base * max(scale, 0.5)) px (R04 rule). On
    # photos smaller than mask_grow_min_below_scale the edges of the mark are softer than that
    # (JPEG blur does not shrink with the photo), so the mask may grow up to mask_grow_min_px
    # on neutral pixels (`mask_extra_grow_px`): saturated scene pixels beside the mark stay.
    mask_grow_base_px: int = 3
    mask_grow_min_px: int = 0
    mask_grow_min_below_scale: float = 0.0
    # Self-check: the template correlating this strongly at its place after removal means the
    # removal failed (good removals measured <= 0.09), so the photo is left as it was.
    max_residual: float = 0.15

    def mask_grow_px(self, scale: float) -> int:
        """Growth of the inpainting mask for a mark detected at `scale` (always covered)."""
        return max(1, round(self.mask_grow_base_px * max(scale, 0.5)))

    def mask_extra_grow_px(self, scale: float) -> int:
        """Extra growth (neutral pixels only) for small photos; 0 when it does not apply."""
        small = bool(self.mask_grow_min_px) and scale < self.mask_grow_min_below_scale
        if small and self.mask_grow_min_px > self.mask_grow_px(scale):
            return self.mask_grow_min_px
        return 0

    @property
    def height(self) -> int:
        return int(self.coverage.shape[0])

    @property
    def width(self) -> int:
        return int(self.coverage.shape[1])

    def scale_for(self, image_width: int) -> float:
        if self.scale_mode == "fixed":
            return 1.0
        return image_width / float(self.ref_width)

    def predicted(self, image_width: int, image_height: int) -> list[Placement]:
        """Where each instance of the mark should be on an image of this size."""
        s = self.scale_for(image_width)
        out: list[Placement] = []
        for idx, inst in enumerate(self.instances):
            if inst.anchor == "center":
                ax, ay = image_width / 2.0, image_height / 2.0
            elif inst.anchor == "top-left":
                ax, ay = 0.0, 0.0
            else:  # bottom-right
                ax, ay = float(image_width), float(image_height)
            out.append(Placement(idx, ax + inst.dx * s, ay + inst.dy * s, s))
        return out


def _require(meta: dict[str, object], key: str, path: Path) -> object:
    if key not in meta:
        raise TemplateError(f"{path.name}: missing key {key!r}")
    return meta[key]


def load_template(json_path: Path) -> WatermarkTemplate:
    """Read the <id>.json + <id>.png pair; validate fields and PNG size."""
    meta = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise TemplateError(f"{json_path.name}: top-level JSON must be an object")
    tid = str(_require(meta, "id", json_path))
    png_path = json_path.with_suffix(".png")
    if not png_path.is_file():
        raise TemplateError(f"{json_path.name}: coverage PNG {png_path.name} not found")
    with Image.open(png_path) as im:
        coverage = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
    if coverage.shape != (int(meta.get("height", -1)), int(meta.get("width", -1))):
        raise TemplateError(f"{json_path.name}: PNG size {coverage.shape} != JSON width/height")
    opacity = tuple(float(v) for v in _require(meta, "opacity", json_path))  # type: ignore[attr-defined]
    if len(opacity) != 3 or not all(0.0 < v < 0.95 for v in opacity):
        raise TemplateError(f"{json_path.name}: opacity must be 3 values in (0, 0.95)")
    strategy = str(_require(meta, "strategy", json_path))
    backend = str(_require(meta, "inpaint_backend", json_path))
    scale_mode = str(_require(meta, "scale_mode", json_path))
    if strategy not in _STRATEGIES or backend not in _BACKENDS or scale_mode not in _SCALE_MODES:
        raise TemplateError(f"{json_path.name}: bad strategy/inpaint_backend/scale_mode")
    lama_fallback = str(meta.get("lama_fallback", "telea"))
    if lama_fallback not in _LAMA_FALLBACKS:
        raise TemplateError(f"{json_path.name}: bad lama_fallback {lama_fallback!r}")
    max_fraction = meta.get("lama_max_mask_fraction")
    if max_fraction is not None and not 0.0 < float(max_fraction) < 1.0:  # type: ignore[arg-type]
        raise TemplateError(f"{json_path.name}: lama_max_mask_fraction must be in (0, 1)")
    max_residual = float(meta.get("max_residual", 0.15))  # type: ignore[arg-type]
    if not 0.0 < max_residual <= 1.0:
        raise TemplateError(f"{json_path.name}: max_residual must be in (0, 1]")
    raw_instances = _require(meta, "instances", json_path)
    if not isinstance(raw_instances, list) or not raw_instances:
        raise TemplateError(f"{json_path.name}: instances must be a non-empty list")
    instances: list[Instance] = []
    for raw in raw_instances:
        anchor = str(raw["anchor"])
        if anchor not in _ANCHORS:
            raise TemplateError(f"{json_path.name}: bad anchor {anchor!r}")
        instances.append(Instance(anchor, float(raw["dx"]), float(raw["dy"])))  # type: ignore[arg-type]
    return WatermarkTemplate(
        id=tid,
        platform=str(_require(meta, "platform", json_path)),
        title=str(meta.get("title", tid)),
        coverage=coverage,
        opacity=(opacity[0], opacity[1], opacity[2]),
        strategy=strategy,  # type: ignore[arg-type]
        inpaint_backend=backend,  # type: ignore[arg-type]
        ref_width=int(_require(meta, "ref_width", json_path)),  # type: ignore[call-overload]
        scale_mode=scale_mode,  # type: ignore[arg-type]
        instances=tuple(instances),
        scale_search=tuple(float(v) for v in _require(meta, "scale_search", json_path)),  # type: ignore[attr-defined]
        pos_tol=float(_require(meta, "pos_tol", json_path)),  # type: ignore[arg-type]
        min_score=float(_require(meta, "min_score", json_path)),  # type: ignore[arg-type]
        lama_fallback=lama_fallback,  # type: ignore[arg-type]
        refine=bool(meta.get("refine", False)),
        lama_max_mask_fraction=None if max_fraction is None else float(max_fraction),  # type: ignore[arg-type]
        mask_grow_base_px=int(meta.get("mask_grow_base_px", 3)),  # type: ignore[call-overload]
        mask_grow_min_px=int(meta.get("mask_grow_min_px", 0)),  # type: ignore[call-overload]
        mask_grow_min_below_scale=float(meta.get("mask_grow_min_below_scale", 0.0)),  # type: ignore[arg-type]
        max_residual=max_residual,
    )


@lru_cache(maxsize=4)
def load_templates(directory: str | None = None) -> tuple[WatermarkTemplate, ...]:
    """All templates from a directory (default: bundled assets), sorted by id."""
    root = Path(directory) if directory else ASSETS_DIR
    paths = sorted(root.glob("*.json"))
    if not paths:
        raise TemplateError(f"no templates in {root}")
    return tuple(load_template(p) for p in paths)
