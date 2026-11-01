from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.watermark.templates import ASSETS_DIR, TemplateError, load_template, load_templates


def _by_id():
    return {t.id: t for t in load_templates()}


def test_all_builtin_templates_load():
    tpls = _by_id()
    assert set(tpls) == {"cityexpert_center", "halo_center", "halo_corner", "zida_logo"}
    for t in tpls.values():
        assert t.coverage.dtype.name == "float32"
        assert 0.99 <= float(t.coverage.max()) <= 1.0
        assert all(0.0 < o < 0.95 for o in t.opacity)
        assert t.min_score > 0


@pytest.mark.parametrize(
    ("tid", "size", "expected"),
    [
        ("cityexpert_center", (1920, 1080), [(83.2, 365.8, 1.0)]),
        ("cityexpert_center", (900, 1600), [(39.0, 718.3, 0.46875)]),
        ("zida_logo", (1440, 1080), [(98.9, 99.9, 1.0), (1059.23, 700.21, 1.0)]),
        ("zida_logo", (810, 1080), [(55.63, 56.19, 0.5625), (595.82, 866.38, 0.5625)]),
        ("halo_center", (1000, 750), [(248.0, 271.0, 1.0)]),
        ("halo_center", (562, 750), [(139.38, 316.55, 0.562)]),
        ("halo_corner", (1000, 750), [(759.0, 725.0, 1.0)]),
    ],
)
def test_predicted_placement_matches_measured_geometry(tid, size, expected):
    placements = _by_id()[tid].predicted(*size)
    assert len(placements) == len(expected)
    for p, (x, y, s) in zip(placements, expected, strict=True):
        assert p.x == pytest.approx(x, abs=0.05)
        assert p.y == pytest.approx(y, abs=0.05)
        assert p.scale == pytest.approx(s, abs=1e-4)


def test_broken_template_is_rejected(tmp_path: Path):
    meta = json.loads((ASSETS_DIR / "zida_logo.json").read_text(encoding="utf-8"))
    (tmp_path / "zida_logo.png").write_bytes((ASSETS_DIR / "zida_logo.png").read_bytes())
    meta["opacity"] = [0.5, 0.5]
    (tmp_path / "zida_logo.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(TemplateError):
        load_template(tmp_path / "zida_logo.json")
