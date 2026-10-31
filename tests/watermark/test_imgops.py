"""imgops: numpy/scipy replacements of the OpenCV calls used by the watermark code (WM05 C1).

Each function is checked against cv2 when OpenCV is installed (dev venv only; production has no
OpenCV) and by properties/edge cases that need no OpenCV at all.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.watermark import imgops

RNG = np.random.default_rng(12345)


def _noise(h, w, c=None, dtype=np.float32, scale=255.0):
    shape = (h, w) if c is None else (h, w, c)
    return (RNG.random(shape) * scale).astype(dtype)


def _blobs(h, w, density=0.3):
    return (RNG.random((h, w)) < density).astype(np.uint8)


@pytest.fixture(scope="module")
def cv2():
    return pytest.importorskip("cv2")


# ---------------------------------------------------------------- gray / blur / sobel / merge


def test_gray_matches_opencv_exactly(cv2):
    img = _noise(37, 53, 3, np.uint8)
    assert np.array_equal(imgops.rgb_to_gray(img), cv2.cvtColor(img, cv2.COLOR_RGB2GRAY))


def test_gray_extremes():
    white = np.full((2, 2, 3), 255, np.uint8)
    black = np.zeros((2, 2, 3), np.uint8)
    assert (imgops.rgb_to_gray(white) == 255).all() and (imgops.rgb_to_gray(black) == 0).all()


@pytest.mark.parametrize("sigma", [1.0, 3.0, 4.0])
def test_gaussian_blur_matches_opencv(cv2, sigma):
    arr = _noise(61, 47)
    expected = cv2.GaussianBlur(arr, (0, 0), sigma)
    assert np.abs(imgops.gaussian_blur(arr, sigma) - expected).max() < 1e-3


def test_gaussian_blur_keeps_constant_image_and_border_is_mirrored():
    flat = np.full((20, 30), 7.0, np.float32)
    assert np.allclose(imgops.gaussian_blur(flat, 4.0), 7.0, atol=1e-5)
    small = np.arange(9, dtype=np.float32).reshape(3, 3)  # smaller than the kernel
    assert imgops.gaussian_blur(small, 4.0).shape == (3, 3)


@pytest.mark.parametrize(("dx", "dy"), [(1, 0), (0, 1)])
def test_sobel_matches_opencv(cv2, dx, dy):
    arr = _noise(40, 33)
    expected = cv2.Sobel(arr, cv2.CV_32F, dx, dy, ksize=3)
    assert np.abs(imgops.sobel3(arr, dx, dy) - expected).max() < 1e-3


def test_sobel_sign_and_flat():
    ramp = np.tile(np.arange(10, dtype=np.float32), (10, 1))  # grows to the right
    assert (imgops.sobel3(ramp, 1, 0)[2:-2, 2:-2] > 0).all()
    assert (imgops.sobel3(ramp.T.copy(), 0, 1)[2:-2, 2:-2] > 0).all()
    assert np.allclose(imgops.sobel3(np.full((6, 6), 3.0, np.float32), 1, 0), 0)


def test_gradient_field_stacks_gx_gy():
    gray = _noise(20, 20)
    field = imgops.gradient_field(gray)
    assert field.shape == (20, 20, 2) and field.dtype == np.float32
    blur = imgops.gaussian_blur(gray, 1.0)
    assert np.array_equal(field[..., 0], imgops.sobel3(blur, 1, 0))
    assert np.array_equal(field[..., 1], imgops.sobel3(blur, 0, 1))


# ---------------------------------------------------------------- resize


@pytest.mark.parametrize(("src", "dst"), [((120, 90), (47, 31)), ((100, 100), (50, 50)),
                                          ((37, 41), (36, 40)), ((300, 200), (150, 101))])
def test_resize_area_matches_opencv(cv2, src, dst):
    arr = _noise(src[1], src[0])
    expected = cv2.resize(arr, dst, interpolation=cv2.INTER_AREA)
    assert np.abs(imgops.resize(arr, dst, "area") - expected).max() < 1e-3


@pytest.mark.parametrize(("src", "dst"), [((30, 20), (61, 47)), ((16, 16), (33, 21)),
                                          ((30, 20), (30, 40))])
def test_resize_linear_matches_opencv(cv2, src, dst):
    arr = _noise(src[1], src[0])
    expected = cv2.resize(arr, dst, interpolation=cv2.INTER_LINEAR)
    assert np.abs(imgops.resize(arr, dst, "linear") - expected).max() < 1e-3


@pytest.mark.parametrize(("src", "dst"), [((30, 20), (61, 47)), ((40, 40), (97, 83))])
def test_resize_cubic_matches_opencv_within_one_level(cv2, src, dst):
    arr = _noise(src[1], src[0], 3, np.uint8)
    expected = cv2.resize(arr, dst, interpolation=cv2.INTER_CUBIC)
    got = imgops.resize(arr, dst, "cubic")
    assert got.dtype == np.uint8 and np.abs(got.astype(int) - expected.astype(int)).max() <= 1


def test_resize_nearest_matches_opencv_exactly(cv2):
    mask = _blobs(77, 91)
    for dst in [(40, 33), (91, 77), (150, 120)]:
        expected = cv2.resize(mask, dst, interpolation=cv2.INTER_NEAREST)
        assert np.array_equal(imgops.resize(mask, dst, "nearest"), expected)


def test_resize_area_uint8_rgb_matches_opencv_within_one_level(cv2):
    img = _noise(300, 400, 3, np.uint8)
    expected = cv2.resize(img, (170, 127), interpolation=cv2.INTER_AREA)
    got = imgops.resize(img, (170, 127), "area")
    assert got.dtype == np.uint8 and np.abs(got.astype(int) - expected.astype(int)).max() <= 1


def test_resize_properties():
    arr = _noise(25, 40, 3, np.uint8)
    same = imgops.resize(arr, (40, 25), "area")
    assert np.array_equal(same, arr) and same is not arr
    flat = np.full((50, 80), 9.0, np.float32)
    for method in ("area", "linear", "cubic", "nearest"):
        assert np.allclose(imgops.resize(flat, (13, 21), method), 9.0, atol=1e-4)
    with pytest.raises(ValueError):
        imgops.resize(arr, (10, 10), "lanczos")


# ---------------------------------------------------------------- matchTemplate


def test_match_template_matches_opencv_on_gradient_fields(cv2):
    gray = _noise(90, 120)
    templ = _noise(30, 40)
    image = imgops.gradient_field(gray)
    kernel = imgops.gradient_field(templ)
    expected = cv2.matchTemplate(image, kernel, cv2.TM_CCORR_NORMED)
    got = imgops.match_template_ccorr_normed(image, kernel)
    assert got.shape == expected.shape and got.dtype == np.float32
    assert np.abs(got - expected).max() < 1e-4


def test_match_template_single_channel_and_peak_location(cv2):
    image = _noise(60, 80)
    templ = image[20:35, 30:52].copy()
    got = imgops.match_template_ccorr_normed(image, templ)
    assert np.unravel_index(np.argmax(got), got.shape) == (20, 30)
    assert abs(float(got.max()) - 1.0) < 1e-5
    expected = cv2.matchTemplate(image, templ, cv2.TM_CCORR_NORMED)
    assert np.abs(got - expected).max() < 1e-4


def test_match_template_flat_window_scores_zero_not_nan():
    image = np.zeros((20, 20, 2), np.float32)
    templ = _noise(5, 5, 2)
    got = imgops.match_template_ccorr_normed(image, templ)
    assert np.isfinite(got).all() and np.abs(got).max() < 1e-6


def test_match_template_template_as_large_as_image():
    image = _noise(8, 9, 2)
    got = imgops.match_template_ccorr_normed(image, image.copy())
    assert got.shape == (1, 1) and abs(float(got[0, 0]) - 1.0) < 1e-5


# ---------------------------------------------------------------- warp / morphology


@pytest.mark.parametrize(("tx", "ty"), [(10.3, 7.7), (0.0, 0.0), (5.5, 3.25), (-2.4, 4.9), (31.99, 0.01)])
def test_translate_matches_opencv_warp_affine(cv2, tx, ty):
    cov = _noise(23, 31)
    shape = (60, 80)
    m = np.float32([[1, 0, tx], [0, 1, ty]])
    expected = cv2.warpAffine(cov, m, (shape[1], shape[0]), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    got = imgops.translate_bilinear(cov, tx, ty, shape)
    assert got.shape == shape and got.dtype == np.float32
    assert np.abs(got - expected).max() < 1e-4


def test_translate_outside_is_zero_and_integer_shift_is_exact():
    cov = np.ones((4, 5), np.float32)
    out = imgops.translate_bilinear(cov, 100.0, 100.0, (20, 20))
    assert not out.any()
    out = imgops.translate_bilinear(cov, 3.0, 2.0, (20, 20))
    assert np.array_equal(out[2:6, 3:8], cov) and out.sum() == cov.sum()


@pytest.mark.parametrize("size", [3, 5, 9])
def test_dilate_matches_opencv(cv2, size):
    mask = _blobs(40, 50, 0.05)
    expected = cv2.dilate(mask, np.ones((size, size), np.uint8))
    assert np.array_equal(imgops.dilate_square(mask, size), expected)


def test_morph_gradient_matches_opencv_including_the_border(cv2):
    mask = _blobs(30, 30, 0.6)
    mask[0, :] = 1
    mask[:, -1] = 1  # touching the border: erosion must not eat from outside
    expected = cv2.morphologyEx(mask, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    assert np.array_equal(imgops.morph_gradient(mask, 3), expected)


def test_dilate_and_gradient_basic_shapes():
    mask = np.zeros((9, 9), np.uint8)
    mask[4, 4] = 1
    assert imgops.dilate_square(mask, 3).sum() == 9
    block = np.zeros((9, 9), np.uint8)
    block[2:7, 2:7] = 1
    grad = imgops.morph_gradient(block, 3)
    assert grad.sum() == 49 - 9  # 7x7 dilated block minus the 3x3 eroded core
    assert grad[4, 4] == 0 and grad[2, 2] == 1


# ---------------------------------------------------------------- components / distance


def test_connected_components_match_opencv(cv2):
    mask = _blobs(60, 70, 0.35)
    n, labels, stats = imgops.connected_components_stats(mask)
    n_cv, labels_cv, stats_cv, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
    assert n == n_cv
    # Same partition; the numbering may differ, so compare components through their pixels.
    assert np.array_equal(labels > 0, labels_cv > 0)
    mapping = {(a, b) for a, b in zip(labels.ravel(), labels_cv.ravel(), strict=True) if a}
    assert len(mapping) == n - 1 and len({a for a, _ in mapping}) == len({b for _, b in mapping})
    assert {tuple(row) for row in stats[1:]} == {tuple(row) for row in stats_cv[1:]}
    for label in range(1, n):
        assert stats[label, 4] == (labels == label).sum()


def test_connected_components_are_eight_connected_and_empty_mask_ok():
    diagonal = np.eye(5, dtype=np.uint8)
    n, labels, stats = imgops.connected_components_stats(diagonal)
    assert n == 2 and stats[1, 4] == 5
    n, labels, stats = imgops.connected_components_stats(np.zeros((4, 4), np.uint8))
    assert n == 1 and not labels.any()


def test_distance_transform_matches_opencv(cv2):
    mask = _blobs(50, 60, 0.9)
    expected = cv2.distanceTransform(mask, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    got = imgops.distance_transform_l2(mask)
    assert got.dtype == np.float32 and np.abs(got - expected).max() < 1e-4


# ---------------------------------------------------------------- classic inpaint


def test_classic_inpaint_reproduces_a_linear_ramp():
    ramp = np.tile(np.linspace(0, 200, 40, dtype=np.float32), (30, 1))
    img = np.stack([ramp, ramp * 0.5, 255 - ramp], axis=-1).astype(np.uint8)
    mask = np.zeros((30, 40), np.uint8)
    mask[10:20, 12:24] = 1
    out = imgops.classic_inpaint(img, mask)
    assert np.array_equal(out[mask == 0], img[mask == 0])
    assert np.abs(out.astype(int) - img.astype(int)).max() <= 2  # harmonic fill is exact on ramps


def test_classic_inpaint_only_changes_masked_pixels_and_handles_edges():
    img = _noise(30, 30, 3, np.uint8)
    mask = np.zeros((30, 30), np.uint8)
    mask[0:4, 0:6] = 1  # in the corner
    mask[26:, 20:] = 1
    out = imgops.classic_inpaint(img, mask)
    assert out.dtype == np.uint8 and np.array_equal(out[mask == 0], img[mask == 0])
    assert not np.array_equal(out[mask == 1], img[mask == 1])


def test_classic_inpaint_empty_and_full_masks_are_safe():
    img = _noise(10, 12, 3, np.uint8)
    assert np.array_equal(imgops.classic_inpaint(img, np.zeros((10, 12), np.uint8)), img)
    full = imgops.classic_inpaint(img, np.ones((10, 12), np.uint8))
    assert np.array_equal(full, img)  # nothing known: leave the photo alone


def test_classic_inpaint_fill_lies_between_the_surroundings():
    img = np.full((20, 20, 3), 100, np.uint8)
    img[:, :8] = 50
    img[:, 12:] = 150
    mask = np.zeros((20, 20), np.uint8)
    mask[5:15, 8:12] = 1
    out = imgops.classic_inpaint(img, mask)
    fill = out[5:15, 8:12, 0]
    assert fill.min() >= 50 and fill.max() <= 150
    assert (np.diff(fill.mean(axis=0)) >= 0).all()  # monotonic across the gap
