"""Image operations for the watermark code on numpy/scipy/Pillow (no OpenCV, no torch).

The production server has no OpenCV and the release route "code only" cannot add a dependency,
so every OpenCV call of the R04 code lives here as a small function with the same numbers:

- ``rgb_to_gray``            OpenCV cvtColor(RGB2GRAY), bit-exact (BT.601, 15-bit fixed point);
- ``gaussian_blur``          OpenCV GaussianBlur(src, (0, 0), sigma), reflect-101 border;
- ``sobel3``                 OpenCV Sobel(src, CV_32F, dx, dy, ksize=3);
- ``gradient_field``         the 2-channel (gx, gy) field used for template matching;
- ``resize``                 OpenCV resize for nearest / linear / area / cubic(a=-0.75);
- ``match_template_ccorr_normed``  OpenCV matchTemplate(TM_CCORR_NORMED), any channel count;
- ``translate_bilinear``     OpenCV warpAffine with a pure translation (1/32 px quantisation);
- ``dilate_square`` / ``morph_gradient``  rectangular-kernel morphology with OpenCV borders;
- ``connected_components_stats``  8-connected labels + (x, y, w, h, area);
- ``distance_transform_l2``  exact Euclidean distance to the nearest zero pixel;
- ``classic_inpaint``        harmonic (Laplace) fill of a mask, replaces OpenCV inpaint TELEA.

Equality with OpenCV is checked in tests/watermark/test_imgops.py.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy import fft as sp_fft
from scipy import ndimage, sparse
from scipy.sparse import linalg as sp_linalg

_DBL_EPSILON = 2.220446049250313e-16


# --------------------------------------------------------------------------- colour / filters


def rgb_to_gray(img_rgb: np.ndarray) -> np.ndarray:
    """uint8 HxWx3 RGB -> uint8 HxW, identical to OpenCV cvtColor(img, COLOR_RGB2GRAY).

    BT.601 weights (0.299, 0.587, 0.114) in the 15-bit fixed point OpenCV 4.x uses.
    """
    px = img_rgb.astype(np.int32)
    gray = (px[..., 0] * 9798 + px[..., 1] * 19235 + px[..., 2] * 3735 + 16384) >> 15
    return gray.astype(np.uint8)


def gaussian_blur(src: np.ndarray, sigma: float) -> np.ndarray:
    """OpenCV GaussianBlur(src, (0, 0), sigma) for float arrays.

    The kernel size is what OpenCV derives for float data (``round(8 * sigma + 1) | 1``), the
    border is reflect-101 (scipy ``mirror``) and the weights are normalised.
    """
    radius = (round(sigma * 8 + 1) | 1) // 2
    return ndimage.gaussian_filter(src, sigma, mode="mirror", radius=radius)


def sobel3(src: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """OpenCV Sobel(src, CV_32F, dx, dy, ksize=3) for one derivative (dx + dy == 1)."""
    if (dx, dy) not in {(1, 0), (0, 1)}:
        raise ValueError("sobel3 supports exactly one first derivative")
    deriv, smooth = np.array([-1.0, 0.0, 1.0]), np.array([1.0, 2.0, 1.0])
    axis_d, axis_s = (1, 0) if dx else (0, 1)
    out = ndimage.correlate1d(src.astype(np.float32, copy=False), deriv, axis=axis_d, mode="mirror")
    out = ndimage.correlate1d(out, smooth, axis=axis_s, mode="mirror")
    return out.astype(np.float32, copy=False)


def gradient_field(gray: np.ndarray) -> np.ndarray:
    """(gx, gy) of the lightly blurred image as a float32 HxWx2 array (two Sobels, stacked)."""
    g = gaussian_blur(gray.astype(np.float32, copy=False), 1.0)
    return np.stack([sobel3(g, 1, 0), sobel3(g, 0, 1)], axis=-1)


# --------------------------------------------------------------------------- resize


def _taps(interpolation: str, n_in: int, n_out: int) -> tuple[np.ndarray, np.ndarray]:
    """Source indices (n_out, k) and weights (n_out, k) of a 1-D resize, OpenCV conventions."""
    scale = n_in / n_out
    dst = np.arange(n_out, dtype=np.float64)
    if interpolation == "nearest":
        idx = np.minimum(np.floor(dst * scale).astype(np.int64), n_in - 1)
        return idx[:, None], np.ones((n_out, 1))
    if interpolation == "area":
        lo, hi = dst * scale, dst * scale + scale
        k = int(np.ceil(scale)) + 1
        first = np.floor(lo).astype(np.int64)
        idx = first[:, None] + np.arange(k)[None, :]
        overlap = np.minimum(idx + 1, hi[:, None]) - np.maximum(idx, lo[:, None])
        weights = np.where(idx < n_in, np.clip(overlap, 0.0, None), 0.0)
        weights = weights / weights.sum(axis=1, keepdims=True)
        return np.minimum(idx, n_in - 1), weights
    fx = (dst + 0.5) * scale - 0.5
    base = np.floor(fx).astype(np.int64)
    frac = fx - base
    if interpolation == "linear":
        idx = base[:, None] + np.arange(2)[None, :]
        weights = np.stack([1.0 - frac, frac], axis=1)
    elif interpolation == "cubic":
        a = -0.75
        t0, t1, t2 = frac + 1.0, frac, 1.0 - frac
        w0 = ((a * t0 - 5 * a) * t0 + 8 * a) * t0 - 4 * a
        w1 = ((a + 2) * t1 - (a + 3)) * t1 * t1 + 1
        w2 = ((a + 2) * t2 - (a + 3)) * t2 * t2 + 1
        weights = np.stack([w0, w1, w2, 1.0 - w0 - w1 - w2], axis=1)
        idx = base[:, None] + np.arange(-1, 3)[None, :]
    else:
        raise ValueError(f"unknown interpolation {interpolation!r}")
    return np.clip(idx, 0, n_in - 1), weights


@lru_cache(maxsize=512)
def _axis_matrix(interpolation: str, n_in: int, n_out: int) -> sparse.csr_matrix:
    idx, weights = _taps(interpolation, n_in, n_out)
    rows = np.repeat(np.arange(n_out), idx.shape[1])
    matrix = sparse.coo_matrix((weights.ravel(), (rows, idx.ravel())), shape=(n_out, n_in))
    return matrix.tocsr().astype(np.float32)


def resize(img: np.ndarray, size: tuple[int, int], interpolation: str = "linear") -> np.ndarray:
    """OpenCV resize(img, (width, height), interpolation) without OpenCV.

    ``interpolation``: nearest | linear (half-pixel centres) | area (exact pixel-area average,
    for shrinking) | cubic (Keys a=-0.75). Borders replicate. uint8 input is rounded back to
    uint8 (OpenCV works in fixed point there: the difference is at most one level); float32
    stays float32.
    """
    width, height = size
    arr = np.asarray(img)
    h_in, w_in = arr.shape[:2]
    if (w_in, h_in) == (width, height):
        return arr.copy()
    work = arr.astype(np.float32, copy=False)
    squeeze = work.ndim == 2
    if squeeze:
        work = work[..., None]
    channels = work.shape[2]
    tmp = _axis_matrix(interpolation, h_in, height) @ work.reshape(h_in, w_in * channels)
    tmp = np.ascontiguousarray(tmp.reshape(height, w_in, channels).transpose(1, 0, 2))
    tmp = _axis_matrix(interpolation, w_in, width) @ tmp.reshape(w_in, height * channels)
    out = tmp.reshape(width, height, channels).transpose(1, 0, 2)
    out = out[..., 0] if squeeze else out
    if arr.dtype == np.uint8:
        return np.clip(np.rint(out), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(out, dtype=arr.dtype)


# --------------------------------------------------------------------------- matching


def match_template_ccorr_normed(image: np.ndarray, templ: np.ndarray) -> np.ndarray:
    """OpenCV matchTemplate(image, templ, TM_CCORR_NORMED): float32 (H-h+1, W-w+1) scores.

    Works for any number of channels: the cross-correlation and both energies are summed over
    all channels (so a 2-channel gradient field is matched as one vector field). The
    correlation runs in float64 through FFT.
    """
    img = np.asarray(image, dtype=np.float64)
    tpl = np.asarray(templ, dtype=np.float64)
    if img.ndim == 2:
        img, tpl = img[..., None], tpl[..., None]
    height, width, channels = img.shape
    th, tw, _ = tpl.shape
    if th > height or tw > width:
        raise ValueError("template is larger than the image")
    shape = (sp_fft.next_fast_len(height), sp_fft.next_fast_len(width))
    spectrum = None
    for c in range(channels):
        term = sp_fft.rfft2(img[..., c], s=shape) * np.conj(sp_fft.rfft2(tpl[..., c], s=shape))
        spectrum = term if spectrum is None else spectrum + term
    out_h, out_w = height - th + 1, width - tw + 1
    num = sp_fft.irfft2(spectrum, s=shape)[:out_h, :out_w]
    energy = (img * img).sum(axis=2)
    integral = np.zeros((height + 1, width + 1))
    integral[1:, 1:] = energy.cumsum(axis=0).cumsum(axis=1)
    window = (
        integral[th : th + out_h, tw : tw + out_w]
        - integral[0:out_h, tw : tw + out_w]
        - integral[th : th + out_h, 0:out_w]
        + integral[0:out_h, 0:out_w]
    )
    norm = np.sqrt(np.maximum(window * float((tpl * tpl).sum()), _DBL_EPSILON))
    return (num / norm).astype(np.float32)


# --------------------------------------------------------------------------- warp / morphology


def _quantised_shift(t: float) -> float:
    """Source offset of OpenCV warpAffine for a translation by ``t``: 1/32 px grid, rounded."""
    return float(np.floor((np.rint(-t * 1024.0) + 16.0) / 32.0) / 32.0)


def _lerp_shift(arr: np.ndarray, shift: float, n_out: int, axis: int) -> np.ndarray:
    """out[i] = arr[i + shift] sampled linearly along ``axis``; zeros outside the array."""
    base = int(np.floor(shift))
    frac = shift - base
    n_in = arr.shape[axis]
    padded = np.pad(arr, [(1, 1) if a == axis else (0, 0) for a in range(arr.ndim)])
    first = np.clip(np.arange(n_out) + base + 1, 0, n_in + 1)
    second = np.clip(np.arange(n_out) + base + 2, 0, n_in + 1)
    out = (1.0 - frac) * padded.take(first, axis=axis) + frac * padded.take(second, axis=axis)
    return out.astype(np.float32, copy=False)


def translate_bilinear(
    cov: np.ndarray, tx: float, ty: float, shape_hw: tuple[int, int]
) -> np.ndarray:
    """Place ``cov`` (float32 HxW) at (tx, ty) in a zero canvas of ``shape_hw``.

    Equals OpenCV warpAffine(cov, [[1,0,tx],[0,1,ty]], (W, H), INTER_LINEAR, BORDER_CONSTANT, 0):
    bilinear, pixels outside ``cov`` count as 0, and the offset is rounded to 1/32 px like
    OpenCV's fixed-point tables.
    """
    height, width = shape_hw
    rows = _lerp_shift(cov.astype(np.float32, copy=False), _quantised_shift(ty), height, axis=0)
    return _lerp_shift(rows, _quantised_shift(tx), width, axis=1)


def dilate_square(mask: np.ndarray, size: int) -> np.ndarray:
    """OpenCV dilate(mask, ones((size, size))): the border does not dilate anything in."""
    return ndimage.maximum_filter(mask, size=size, mode="constant", cval=0)


def _erode_square(mask: np.ndarray, size: int) -> np.ndarray:
    """OpenCV erode(mask, ones((size, size))): outside the image counts as "set" (+inf)."""
    return ndimage.minimum_filter(mask, size=size, mode="constant", cval=np.iinfo(mask.dtype).max)


def morph_gradient(mask: np.ndarray, size: int = 3) -> np.ndarray:
    """OpenCV morphologyEx(mask, MORPH_GRADIENT, ones((size, size))) = dilate - erode (uint8)."""
    return dilate_square(mask, size) - _erode_square(mask, size)


# --------------------------------------------------------------------------- components / distance


def connected_components_stats(mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """8-connected components of ``mask > 0``: (count incl. background, labels, stats).

    The partition equals OpenCV's (connectivity=8); the numbering of the components may differ
    from OpenCV's (callers must not rely on it). ``stats`` rows are (x, y, width, height,
    area); row 0 is the background (area only, box = whole image).
    """
    labels, n = ndimage.label(mask > 0, structure=np.ones((3, 3), dtype=int))
    labels = labels.astype(np.int32, copy=False)
    stats = np.zeros((n + 1, 5), dtype=np.int32)
    stats[0, :4] = (0, 0, mask.shape[1], mask.shape[0])
    stats[:, 4] = np.bincount(labels.ravel(), minlength=n + 1)
    for i, box in enumerate(ndimage.find_objects(labels), start=1):
        if box is not None:
            stats[i, :4] = (box[1].start, box[0].start,
                            box[1].stop - box[1].start, box[0].stop - box[0].start)
    return n + 1, labels, stats


def distance_transform_l2(mask: np.ndarray) -> np.ndarray:
    """Euclidean distance of every set pixel to the nearest zero pixel (float32), exact."""
    return ndimage.distance_transform_edt(mask > 0).astype(np.float32)


# --------------------------------------------------------------------------- classic inpaint


def classic_inpaint(img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Fill ``mask > 0`` of a uint8 HxWxC image by solving the Laplace equation per channel.

    The masked pixels become the smoothest (harmonic) interpolation of the known pixels around
    them: right for thin bands and small marks, blurry on large ones. Replaces OpenCV inpaint
    (TELEA), which the server cannot have. Unmasked pixels are returned byte for byte; a mask
    with no known neighbour anywhere (nothing to interpolate from) leaves the image unchanged.
    """
    hole = mask > 0
    if not hole.any() or hole.all():
        return img_rgb.copy()
    ys, xs = np.nonzero(hole)
    y0, y1 = max(0, ys.min() - 1), min(hole.shape[0], ys.max() + 2)
    x0, x1 = max(0, xs.min() - 1), min(hole.shape[1], xs.max() + 2)
    local = hole[y0:y1, x0:x1]
    crop = img_rgb[y0:y1, x0:x1].astype(np.float64)
    index = np.full(local.shape, -1, dtype=np.int64)
    index[local] = np.arange(int(local.sum()))
    count = int(local.sum())
    degree = np.zeros(count)
    rhs = np.zeros((count, crop.shape[2]))
    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        src_y = slice(max(0, -dy), local.shape[0] - max(0, dy))
        src_x = slice(max(0, -dx), local.shape[1] - max(0, dx))
        dst_y = slice(max(0, dy), local.shape[0] - max(0, -dy))
        dst_x = slice(max(0, dx), local.shape[1] - max(0, -dx))
        here = local[src_y, src_x]
        neighbour = local[dst_y, dst_x]
        index_here = index[src_y, src_x][here]
        degree[index_here] += 1.0  # an in-image neighbour exists (Neumann at the image edge)
        inside = neighbour[here]
        rows.append(index_here[inside])
        cols.append(index[dst_y, dst_x][here][inside])
        known = ~inside
        rhs_idx = index_here[known]
        known_values = crop[dst_y, dst_x][here][known]
        np.add.at(rhs, rhs_idx, known_values)
    eps = 1e-6  # keeps a component without any known neighbour solvable (it keeps its pixels)
    rows_all, cols_all = np.concatenate(rows), np.concatenate(cols)
    diagonal = np.arange(count)
    system = sparse.coo_matrix(
        (
            np.concatenate([-np.ones(len(rows_all)), degree + eps]),
            (np.concatenate([rows_all, diagonal]), np.concatenate([cols_all, diagonal])),
        ),
        shape=(count, count),
    ).tocsc()
    rhs += eps * crop[local]
    solution = sp_linalg.splu(system).solve(rhs)
    out = img_rgb.copy()
    patch = out[y0:y1, x0:x1]
    patch[local] = np.clip(np.rint(solution), 0, 255).astype(np.uint8)
    return out
