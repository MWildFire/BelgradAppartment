"""Per-part alpha refinement for reverse-blend removal of semi-transparent white marks.

A platform mark is applied as J = a*255 + (1 - a)*O with a = opacity * coverage. The bundled
template is only an approximation: the real mark sits a fraction of a pixel away from the
detected place, a part (a 260 px tall icon bar) can be 0.5 % taller or wider than the detected
global scale says, edges are softer or sharper than the template, and parts (letters, icon
bars) differ slightly in opacity. Subtracting a slightly wrong alpha leaves a ghost *edge* along
every contour; with a wide logo (~1750 px) the errors differ from one end to the other.

Every connected part of the template (a letter, an icon bar; tiny parts are grouped) is
therefore fitted separately: a sub-pixel shift (dx, dy), a size correction (sx, sy) about the
part's centre, an edge blur sigma and an opacity multiplier k are chosen to minimise the squared
gradient of the restored image in a thin tube along the part's contour. Real scene edges that
cross the tube contribute almost the same for every candidate (and are winsorised), so the
minimum sits where the mark is cancelled best. A second pass re-fits every part inside a small
box around the consensus of the well-measured parts, so tiny parts or parts over a busy scene
cannot run to degenerate solutions.

The result is a coverage*k map: `reverse_blend` with it needs no Telea band along the contour,
which is what used to interrupt mouldings, furniture lines and foliage.

numpy/scipy only (no OpenCV).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage as ndi

CORE_FRACTION = 0.5  # a part core is where coverage >= this fraction of the template peak
MIN_PART_AREA = 24  # core pixels; smaller parts are grouped with their neighbours
GROUP_GAP_PX = 5  # small parts closer than this form one group
TUBE_PX = 4  # half-width of the contour tube the energy is measured in
GRAD_CLIP = 5.0  # per-pixel gradient magnitude cap (levels/px): real scene edges saturate
MARGIN = 8  # zero padding of a part tile (room for shift, scale and blur)
EVAL_MARGIN = 14  # context around a part used when evaluating the energy
MIN_TUBE_PIXELS = 30
K_RANGE = (0.5, 1.5)
K_SCAN = tuple(round(0.5 + 0.1 * i, 2) for i in range(11))  # 0.5 .. 1.5
SIGMA_MAX = 2.5
SIGMA_START = 0.6
SHIFT_MAX = 3.0
SCALE_RANGE = (0.985, 1.015)
IMPROVEMENT = 0.002  # relative energy gain required to accept a move
# Second pass: parts are re-fitted inside a box around the consensus of the well-measured parts.
CONSENSUS_MIN_AREA = 600  # core pixels a part needs to vote for the consensus
CONSENSUS_GAIN = 0.05  # relative energy drop that makes a part's own fit trustworthy
CONSENSUS_MIN_PARTS = 3
BOX_SPREAD = (1.0, 1.0, 0.004, 0.004, 0.7, 0.15)  # +/- around the consensus
K_BOX_SCAN = (-0.1, -0.05, 0.0, 0.05, 0.1)  # k offsets tried first in the second pass
# descent steps per level: dx/dy (px), sx/sy, sigma (px), k
_STEPS = (
    (0.5, 0.004, 0.5, 0.06),
    (0.25, 0.002, 0.25, 0.03),
    (0.125, 0.001, 0.125, 0.015),
)
_MAX_ROUNDS = 3

# (dx, dy, sx, sy, sigma, k)
Params = tuple[float, float, float, float, float, float]
Bounds = tuple[tuple[float, float], ...]
NOMINAL: Params = (0.0, 0.0, 1.0, 1.0, 0.0, 1.0)
START: Params = (0.0, 0.0, 1.0, 1.0, SIGMA_START, 1.0)
_GLOBAL_BOUNDS: Bounds = (
    (-SHIFT_MAX, SHIFT_MAX), (-SHIFT_MAX, SHIFT_MAX), SCALE_RANGE, SCALE_RANGE, (0.0, SIGMA_MAX),
    K_RANGE,
)


@dataclass(frozen=True)
class PartFit:
    """Fitted parameters of one part (a letter, an icon bar or a group of tiny parts)."""

    part: int
    area: int  # core pixels
    dx: float
    dy: float
    sx: float
    sy: float
    sigma: float
    k: float
    energy_before: float  # edge energy at the nominal position (dx = dy = 0, no blur, k = 1)
    energy_after: float


@dataclass(frozen=True)
class RefinedMark:
    cov_k: np.ndarray  # float32 HxW: coverage * k of the whole mark, zero elsewhere
    parts: tuple[PartFit, ...]
    k_center: float  # core-area weighted median of the parts' k (1.0 when nothing was fitted)


def split_parts(cov: np.ndarray) -> tuple[np.ndarray, int]:
    """(owner map, number of parts): every pixel with coverage belongs to its nearest part core.

    Cores are connected components of coverage >= CORE_FRACTION * peak. Components smaller than
    MIN_PART_AREA are merged into groups (components closer than GROUP_GAP_PX): a lone speck
    cannot be fitted reliably, a word of small letters can.
    """
    peak = float(cov.max())
    if peak <= 0.0:
        return np.zeros(cov.shape, np.int32), 0
    core = cov >= CORE_FRACTION * peak
    labels, count = ndi.label(core, structure=np.ones((3, 3)))
    areas = ndi.sum(core, labels, index=np.arange(1, count + 1))
    small = np.isin(labels, 1 + np.flatnonzero(np.asarray(areas) < MIN_PART_AREA))
    ids = np.zeros(cov.shape, np.int32)
    next_id = 0
    for i in np.flatnonzero(np.asarray(areas) >= MIN_PART_AREA):
        next_id += 1
        ids[labels == i + 1] = next_id
    if small.any():
        grown = ndi.binary_dilation(small, structure=np.ones((3, 3)), iterations=GROUP_GAP_PX)
        glabels, gcount = ndi.label(grown, structure=np.ones((3, 3)))
        for g in range(1, gcount + 1):
            members = small & (glabels == g)
            if members.any():
                next_id += 1
                ids[members] = next_id
    if next_id == 0:
        return np.zeros(cov.shape, np.int32), 0
    near = ndi.distance_transform_edt(ids == 0, return_distances=False, return_indices=True)
    owner = ids[near[0], near[1]]
    owner = np.where(cov > 0.003, owner, 0).astype(np.int32)
    return owner, next_id


def _render(tile: np.ndarray, frac: tuple[float, float], params: Params) -> np.ndarray:
    """Part tile blurred by sigma, scaled about its centre and shifted (bilinear).

    `frac` is the fractional part of the template position (y, x); params = (dx, dy, sx, sy,
    sigma, k) -- k is not applied here.
    """
    dx, dy, sx, sy, sigma, _k = params
    out = tile
    if sigma > 0.05:
        out = ndi.gaussian_filter(out, sigma, mode="constant")
    ty, tx = frac[0] + dy, frac[1] + dx
    if abs(sy - 1.0) > 1e-6 or abs(sx - 1.0) > 1e-6:
        cy, cx = (out.shape[0] - 1) / 2.0, (out.shape[1] - 1) / 2.0
        offset = (cy - (cy + ty) / sy, cx - (cx + tx) / sx)
        return ndi.affine_transform(
            out, (1.0 / sy, 1.0 / sx), offset=offset, order=1, mode="constant"
        )
    if abs(ty) > 1e-4 or abs(tx) > 1e-4:
        return ndi.shift(out, (ty, tx), order=1, mode="constant")
    return out


def _add(canvas: np.ndarray, tile: np.ndarray, top: int, left: int, weight: float = 1.0) -> None:
    """canvas[top:, left:] += weight * tile, clipped to the canvas."""
    h, w = canvas.shape
    y0, x0 = max(0, top), max(0, left)
    y1, x1 = min(h, top + tile.shape[0]), min(w, left + tile.shape[1])
    if y1 > y0 and x1 > x0:
        canvas[y0:y1, x0:x1] += weight * tile[y0 - top : y1 - top, x0 - left : x1 - left]


@dataclass
class _Part:
    index: int
    tile: np.ndarray  # coverage of this part cut to its bbox, padded by MARGIN
    top: int  # tile origin in image coordinates (before the fractional offset)
    left: int
    area: int
    frac: tuple[float, float]  # fractional part of the template position (y, x)
    region: tuple[int, int, int, int]  # evaluation window in image coords (y0, y1, x0, x1)
    tube: np.ndarray  # bool, region-shaped
    params: Params = START


def _energy(lum: np.ndarray, alpha: np.ndarray, tube: np.ndarray) -> float:
    a = np.clip(alpha, 0.0, 0.95)
    restored = (lum - 255.0 * a) / (1.0 - a)
    gy, gx = np.gradient(restored)
    g2 = np.minimum(gx * gx + gy * gy, GRAD_CLIP * GRAD_CLIP)
    return float(g2[tube].mean())


def _part_alpha(part: _Part, params: Params, opacity: float) -> np.ndarray:
    """Alpha (coverage*k*opacity) of one part in its evaluation region."""
    y0, y1, x0, x1 = part.region
    canvas = np.zeros((y1 - y0, x1 - x0), np.float32)
    _add(canvas, _render(part.tile, part.frac, params), part.top - y0, part.left - x0,
         opacity * params[5])
    return canvas


def _stamp(total: np.ndarray, part: _Part, params: Params, opacity: float, sign: float = 1.0):
    """total += sign * alpha of `part` rendered with `params` (in place)."""
    _add(total, _render(part.tile, part.frac, params), part.top, part.left,
         sign * opacity * params[5])


def _fit_part(
    part: _Part,
    window: np.ndarray,
    around: np.ndarray,
    opacity: float,
    start: Params,
    bounds: Bounds,
    k_scan: tuple[float, ...],
) -> tuple[Params, float, float]:
    """Coordinate descent on (dx, dy, sx, sy, sigma, k) inside `bounds` from `start`.

    `window` is the photo luminance and `around` the alpha of all other parts, both cut to the
    part's evaluation region. A move is accepted only when it lowers the energy by IMPROVEMENT
    (relative), so a part with no evidence in its tube (a flat energy surface) stays exactly at
    `start`. Returns (params, energy at the nominal template, energy at the result).
    """

    def energy(params: Params) -> float:
        return _energy(window, around + _part_alpha(part, params, opacity), part.tube)

    nominal = energy(NOMINAL)
    best_params, best = start, energy(start)
    for k in k_scan:
        trial: Params = (*start[:5], k)
        e = energy(trial)
        if e < best * (1.0 - IMPROVEMENT) - 1e-6:
            best_params, best = trial, e
    for step_xy, step_scale, step_sigma, step_k in _STEPS:
        steps = (step_xy, step_xy, step_scale, step_scale, step_sigma, step_k)
        for _ in range(_MAX_ROUNDS):
            moved = False
            for slot, step in enumerate(steps):
                for sign in (-1.0, 1.0):
                    values = list(best_params)
                    values[slot] += sign * step
                    pairs = zip(values, bounds, strict=True)
                    if not all(lo - 1e-9 <= v <= hi + 1e-9 for v, (lo, hi) in pairs):
                        continue
                    trial = (values[0], values[1], values[2], values[3], values[4], values[5])
                    e = energy(trial)
                    if e < best * (1.0 - IMPROVEMENT) - 1e-6:
                        best_params, best, moved = trial, e, True
            if not moved:
                break
    return best_params, nominal, best


def _weighted_median(values: list[tuple[float, int]]) -> float:
    ordered = sorted(values)
    half = sum(w for _k, w in ordered) / 2.0
    acc = 0
    for k, w in ordered:
        acc += w
        if acc >= half:
            return k
    return ordered[-1][0]


def _consensus(fits: list[tuple[_Part, Params, float, float]]) -> Params:
    """Area-weighted medians of the parts whose own fit clearly lowered the energy."""
    solid = [
        (p, params)
        for p, params, before, after in fits
        if p.area >= CONSENSUS_MIN_AREA and before > 0.0
        and (before - after) / before >= CONSENSUS_GAIN
    ]
    if len(solid) < CONSENSUS_MIN_PARTS:
        return START
    med = [_weighted_median([(params[i], p.area) for p, params in solid]) for i in range(6)]
    return (med[0], med[1], med[2], med[3], med[4], med[5])


def _box_around(center: Params) -> Bounds:
    """Per-part search box around the consensus (clipped to the global limits)."""
    return tuple(
        (max(lo, value - spread), min(hi, value + spread))
        for value, spread, (lo, hi) in zip(center, BOX_SPREAD, _GLOBAL_BOUNDS, strict=True)
    )


def _build_parts(
    cov: np.ndarray, owner: np.ndarray, count: int, x: float, y: float, shape_hw: tuple[int, int]
) -> list[_Part]:
    height, width = shape_hw
    ix, iy = int(np.floor(x)), int(np.floor(y))
    frac = (float(y - iy), float(x - ix))
    parts: list[_Part] = []
    for index in range(1, count + 1):
        mask = owner == index
        ys, xs = np.nonzero(mask)
        if ys.size == 0:
            continue
        by0, by1, bx0, bx1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        tile = np.zeros((by1 - by0 + 2 * MARGIN, bx1 - bx0 + 2 * MARGIN), np.float32)
        tile[MARGIN : MARGIN + by1 - by0, MARGIN : MARGIN + bx1 - bx0] = (
            cov[by0:by1, bx0:bx1] * mask[by0:by1, bx0:bx1]
        )
        top, left = iy + int(by0) - MARGIN, ix + int(bx0) - MARGIN
        ry0, rx0 = max(0, top - EVAL_MARGIN), max(0, left - EVAL_MARGIN)
        ry1 = min(height, top + tile.shape[0] + EVAL_MARGIN)
        rx1 = min(width, left + tile.shape[1] + EVAL_MARGIN)
        if ry1 <= ry0 or rx1 <= rx0:
            continue
        base = np.zeros((ry1 - ry0, rx1 - rx0), np.float32)
        _add(base, _render(tile, frac, NOMINAL), top - ry0, left - rx0)
        support = base > 0.01
        tube = ndi.binary_dilation(support, iterations=TUBE_PX) & ~ndi.binary_erosion(
            support, iterations=TUBE_PX
        )
        parts.append(
            _Part(index, tile, top, left, int(mask.sum()), frac, (ry0, ry1, rx0, rx1), tube)
        )
    return parts


def refine_alpha(
    img_rgb: np.ndarray, cov: np.ndarray, *, x: float, y: float, opacity: float
) -> RefinedMark:
    """Fit the mark part by part; `cov` is the template coverage already scaled to the photo.

    (x, y) is the detected top-left corner (sub-pixel). Returns the coverage*k map for the
    whole image (float32, zero away from the mark) and the per-part fits. Parts that cannot be
    measured (no contour pixels inside the image) keep the nominal template with a light blur.
    """
    height, width = img_rgb.shape[:2]
    lum = img_rgb.astype(np.float32).mean(axis=2)
    owner, count = split_parts(cov)
    parts = _build_parts(cov, owner, count, x, y, (height, width))

    total = np.zeros((height, width), np.float32)  # alpha (opacity included) of all parts
    for part in parts:
        _stamp(total, part, part.params, opacity)

    def refit(part: _Part, start: Params, bounds: Bounds, k_scan: tuple[float, ...]):
        y0, y1, x0, x1 = part.region
        around = total[y0:y1, x0:x1] - _part_alpha(part, part.params, opacity)
        found = _fit_part(part, lum[y0:y1, x0:x1], around, opacity, start, bounds, k_scan)
        _stamp(total, part, part.params, opacity, -1.0)
        part.params = found[0]
        _stamp(total, part, part.params, opacity)
        return found

    fittable = [p for p in parts if int(p.tube.sum()) >= MIN_TUBE_PIXELS]
    first = [(part, *refit(part, START, _GLOBAL_BOUNDS, K_SCAN)) for part in fittable]
    center = _consensus(first)
    box = _box_around(center)
    results: dict[int, tuple[Params, float, float]] = {}
    for part in fittable:
        scan = tuple(
            round(min(max(center[5] + d, box[5][0]), box[5][1]), 4) for d in K_BOX_SCAN
        )
        results[part.index] = refit(part, center, box, scan)
    fits: list[PartFit] = []
    for part in parts:
        if part.index in results:
            params, before, after = results[part.index]
            fits.append(PartFit(part.index, part.area, *params, before, after))
        else:  # no contour pixels to measure: nominal template with the default edge blur
            fits.append(PartFit(part.index, part.area, *START, 0.0, 0.0))
    cov_k = np.clip(total / max(opacity, 1e-6), 0.0, None).astype(np.float32)
    measured = [(f.k, f.area) for f in fits if f.energy_before > 0.0]
    k_center = _weighted_median(measured) if measured else 1.0
    return RefinedMark(cov_k=cov_k, parts=tuple(fits), k_center=float(k_center))
