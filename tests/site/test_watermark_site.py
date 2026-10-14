"""The site shows watermark-free photos with WATERMARK_SITE_USE_CLEAN=1 (and only then)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

import src.site.photos as photos
import src.watermark.cli as wm_cli
import src.watermark.published as published
from src.cli import cli
from src.config import get_settings
from src.site.media import _permitted_file
from src.site.og_image import _photo_file
from src.watermark.pipeline import decode_image, find_marks
from src.watermark.published import clean_batch
from src.watermark.store import ensure_clean
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    s = get_settings()
    monkeypatch.setattr(s, "inbound_photos_dir", inbound)
    monkeypatch.setattr(s, "site_photos_root", inbound)
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "watermark_site_use_clean", False)
    photos.invalidate_manifest_cache()
    return inbound


def _marked_photo(root: Path) -> Path:
    path = root / "1" / "9" / "00.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    img = apply_mark(make_background(1440, 1080, seed=8), {t.id: t for t in load_templates()}["zida_logo"])
    Image.fromarray(img).save(path, "JPEG", quality=92)
    return path


def _detail_has_marks(root: Path, rel: str) -> bool:
    detail = decode_image((root / f"{rel}.detail.webp").read_bytes())
    return bool(find_marks(detail))


def test_variants_come_from_clean_copy_only_with_flag(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    photos.process_photo(orig, rel, root)
    assert _detail_has_marks(root, rel)  # flag off -> variants come from the original
    ensure_clean(str(orig))
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    _entry, generated, _skipped = photos.process_photo(orig, rel, root)
    assert generated == 2  # the clean copy is newer than the old variants
    assert not _detail_has_marks(root, rel)
    _entry, generated, skipped = photos.process_photo(orig, rel, root)
    assert (generated, skipped) == (0, 2)  # idempotent again afterwards


def test_clean_copy_older_than_variants_is_rebuilt(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    ensure_clean(str(orig))
    photos.process_photo(orig, rel, root)  # flag off: variants from the original, marks visible
    assert _detail_has_marks(root, rel)
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    assert published.rebuild_site_variants([str(orig)]) == 1  # clean copy is older than variants
    assert not _detail_has_marks(root, rel)
    assert published.rebuild_site_variants([str(orig)]) == 0


def test_original_url_serves_clean_copy_with_flag(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    ensure_clean(str(orig))
    assert _permitted_file(rel, [str(orig)]) == orig
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    served = _permitted_file(rel, [str(orig)])
    assert served is not None and served != orig and not find_marks(decode_image(served.read_bytes()))
    assert _photo_file([f"/photos/{rel}"]) == served
    assert _permitted_file("1/9/other.jpg", [str(orig)]) is None


def test_without_clean_copy_everything_stays_original(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    assert _permitted_file(rel, [str(orig)]) == orig


def test_cleaned_then_reverted_apartment_on_the_site(root, monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    assert clean_batch([str(orig)]) == (1, 0)  # what the bot's site sweep does
    photos.generate_for_stored_paths([str(orig)])
    assert not _detail_has_marks(root, rel)

    async def fake_paths(_match_id: int) -> list[str]:
        return [str(orig)]

    monkeypatch.setattr(wm_cli, "_match_photo_paths", fake_paths)
    result = CliRunner().invoke(cli, ["wm-revert", "5"])
    assert result.exit_code == 0, result.output
    assert _detail_has_marks(root, rel)  # card/detail rebuilt from the original
    assert _permitted_file(rel, [str(orig)]) == orig
