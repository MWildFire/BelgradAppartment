"""T4-1 — `otz photos-backfill` CLI surface (contract §P.1 б / §C).

The heavy lifting lives in src/site/photos.backfill_variants (unit-tested in
tests/site/test_photos.py); here we pin the CLI wiring: root comes from settings,
--dry-run / --limit are honoured, and the run reports progress + a final summary.
No DB involved — the backfill walks the photos_root filesystem.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

import src.site.photos as photos
from src.cli import cli
from src.config import get_settings


def _make_jpeg(path: Path, w: int = 320, h: int = 240) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(7)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    img.save(path, "JPEG", quality=95)
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "site_photos_root", root)
    photos.invalidate_manifest_cache()
    return root


def test_backfill_generates_variants_and_manifest(root: Path) -> None:
    _make_jpeg(root / "1" / "10" / "00.jpg")
    _make_jpeg(root / "1" / "10" / "01.jpg")

    result = CliRunner().invoke(cli, ["photos-backfill"])

    assert result.exit_code == 0, result.output
    assert (root / "1" / "10" / "00.jpg.card.webp").is_file()
    assert (root / "1" / "10" / "01.jpg.detail.webp").is_file()
    assert (root / photos.MANIFEST_NAME).is_file()
    assert "originals=2" in result.output
    assert "generated=4" in result.output


def test_backfill_dry_run_reports_but_writes_nothing(root: Path) -> None:
    _make_jpeg(root / "a.jpg")

    result = CliRunner().invoke(cli, ["photos-backfill", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "would generate=2" in result.output
    leftovers = [p for p in root.rglob("*") if p.is_file() and p.suffix != ".jpg"]
    assert leftovers == [], "dry-run must not touch the filesystem"


def test_backfill_limit(root: Path) -> None:
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        _make_jpeg(root / name)

    result = CliRunner().invoke(cli, ["photos-backfill", "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "originals=1" in result.output
    webps = list(root.rglob("*.webp"))
    assert len(webps) == 2


def test_backfill_second_run_skips(root: Path) -> None:
    _make_jpeg(root / "a.jpg")
    first = CliRunner().invoke(cli, ["photos-backfill"])
    second = CliRunner().invoke(cli, ["photos-backfill"])

    assert first.exit_code == 0 and second.exit_code == 0
    assert "generated=4" not in second.output
    assert "generated=0" in second.output
    assert "skipped=2" in second.output


def test_backfill_missing_root_fails_cleanly(root: Path, monkeypatch) -> None:
    """A mistyped SITE_PHOTOS_ROOT must not crash with a traceback — clear message, no work."""
    monkeypatch.setattr(get_settings(), "site_photos_root", root / "does-not-exist")

    result = CliRunner().invoke(cli, ["photos-backfill"])

    assert result.exit_code == 0, result.output
    assert "originals=0" in result.output
