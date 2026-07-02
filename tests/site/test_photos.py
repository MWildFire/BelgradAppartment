"""T4-1 — photo variants + manifest (contract §P.1) and the manifest read-side (§P.2 API).

Fixture images are generated with Pillow into tmp_path (no image fixtures in the repo).
Noise JPEGs on purpose: a flat-colour image compresses to nearly nothing in ANY format,
so the «variant ≤ original by weight» assertion would prove nothing on it.

The publish-hook tests reuse tests/conftest.fresh_db (same pattern as test_auto_publish.py):
generation must fire once on overlay CREATE, never on an existing overlay, and a generation
failure must NEVER break the publication itself.
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select

import src.site.photos as photos
from src.config import get_settings
from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)
from tests.conftest import fresh_db

# --------------------------------------------------------------------------- helpers / fixtures


def _make_jpeg(path: Path, w: int = 800, h: int = 600) -> Path:
    """Deterministic noise JPEG (quality 95) — worst case for re-compression, so the
    size assertions actually bite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(42)
    img = Image.new("RGB", (w, h))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(w * h)])
    img.save(path, "JPEG", quality=95)
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """A fake photos_root wired into settings; read-side cache reset so tests are isolated."""
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "site_photos_root", root)
    photos.invalidate_manifest_cache()
    return root


def _manifest(root: Path) -> dict:
    return json.loads((root / photos.MANIFEST_NAME).read_text(encoding="utf-8"))


def _derived_files(root: Path) -> list[Path]:
    """Everything generation could have written (sync helper — keeps async tests lint-clean)."""
    return [p for p in root.rglob("*") if p.is_file() and p.suffix in (".webp", ".json", ".tmp")]


# --------------------------------------------------------------------------- generation (§P.1)


def test_variants_exact_naming_and_weight(root: Path) -> None:
    """card/detail land NEXT to the original with the exact <orig>.<kind>.webp names,
    each variant weighs ≤ the original, and heights stay proportional."""
    orig = _make_jpeg(root / "1" / "10" / "00.jpg", 800, 600)

    stats = photos.backfill_variants(root=root)

    assert stats.total == 1 and stats.generated == 2 and stats.broken == 0
    card = root / "1" / "10" / "00.jpg.card.webp"
    detail = root / "1" / "10" / "00.jpg.detail.webp"
    assert card.is_file() and detail.is_file()
    assert card.stat().st_size <= orig.stat().st_size
    assert detail.stat().st_size <= orig.stat().st_size
    with Image.open(card) as im:
        assert im.size == (640, 480)  # 800x600 → 640w, height proportional
    with Image.open(detail) as im:
        assert im.size == (800, 600)  # original narrower than 1280 → no upscale


def test_no_upscale_small_original(root: Path) -> None:
    """An original narrower than 640 stays its own size in BOTH variants (§P.1: no upscale)."""
    _make_jpeg(root / "a.jpg", 400, 300)

    photos.backfill_variants(root=root)

    for kind in ("card", "detail"):
        with Image.open(root / f"a.jpg.{kind}.webp") as im:
            assert im.size == (400, 300)


def test_backfill_idempotent(root: Path) -> None:
    """Second run regenerates nothing: existing variants are skipped, bytes untouched."""
    _make_jpeg(root / "1" / "10" / "00.jpg")
    first = photos.backfill_variants(root=root)
    card = root / "1" / "10" / "00.jpg.card.webp"
    before = card.stat().st_mtime_ns, card.stat().st_size

    second = photos.backfill_variants(root=root)

    assert first.generated == 2
    assert second.generated == 0 and second.skipped == 2
    assert (card.stat().st_mtime_ns, card.stat().st_size) == before


def test_dry_run_writes_nothing(root: Path) -> None:
    """--dry-run counts the would-be work but leaves the tree byte-for-byte alone."""
    _make_jpeg(root / "1" / "10" / "00.jpg")

    stats = photos.backfill_variants(root=root, dry_run=True)

    assert stats.total == 1 and stats.generated == 2
    leftovers = [p for p in root.rglob("*") if p.is_file() and p.suffix != ".jpg"]
    assert leftovers == [], "dry-run must not write variants or manifest"


def test_limit_caps_originals(root: Path) -> None:
    _make_jpeg(root / "a.jpg")
    _make_jpeg(root / "b.jpg")
    _make_jpeg(root / "c.jpg")

    stats = photos.backfill_variants(root=root, limit=2)

    assert stats.total == 2 and stats.generated == 4


def test_manifest_roundtrip(root: Path) -> None:
    """Manifest lands in the photos_root ROOT, keyed by relpath, w/h = ORIGINAL dims,
    and load_manifest() reads back exactly what was written."""
    _make_jpeg(root / "1" / "10" / "00.jpg", 800, 600)

    photos.backfill_variants(root=root)

    data = _manifest(root)
    assert data == {
        "1/10/00.jpg": {
            "w": 800,
            "h": 600,
            "card": "1/10/00.jpg.card.webp",
            "detail": "1/10/00.jpg.detail.webp",
        }
    }
    photos.invalidate_manifest_cache()
    assert photos.load_manifest(root) == data


def test_broken_photo_skipped_and_logged(root: Path, caplog) -> None:
    """A corrupt file is skipped WITH a warning — never a crash; healthy neighbours and the
    manifest still get processed; the broken relpath never enters the manifest."""
    _make_jpeg(root / "ok.jpg")
    (root / "broken.jpg").write_bytes(b"\xff\xd8 definitely not a jpeg")

    with caplog.at_level(logging.WARNING, logger="src.site.photos"):
        stats = photos.backfill_variants(root=root)

    assert stats.broken == 1 and stats.generated == 2
    assert "broken.jpg" in caplog.text
    assert set(_manifest(root)) == {"ok.jpg"}


def test_generated_variants_not_reprocessed_as_originals(root: Path) -> None:
    """*.card.webp / *.detail.webp are outputs, not inputs — a re-run must not produce
    00.jpg.card.webp.card.webp chains."""
    _make_jpeg(root / "00.jpg")
    photos.backfill_variants(root=root)

    stats = photos.backfill_variants(root=root)

    assert stats.total == 1
    assert not list(root.rglob("*.webp.card.webp"))
    assert not list(root.rglob("*.webp.detail.webp"))


# --------------------------------------------------------------------------- read side (§P.2 API)


def test_variant_for_and_dims_for(root: Path) -> None:
    _make_jpeg(root / "1" / "10" / "00.jpg", 800, 600)
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()

    assert photos.variant_for("/photos/1/10/00.jpg", "card") == "/photos/1/10/00.jpg.card.webp"
    assert photos.variant_for("/photos/1/10/00.jpg", "detail") == "/photos/1/10/00.jpg.detail.webp"
    assert photos.dims_for("/photos/1/10/00.jpg") == (800, 600)


def test_variant_for_fallbacks_return_none(root: Path) -> None:
    """No manifest / no entry / unknown kind / non-/photos src / traversal → None (the T4-2
    Jinja filter falls back to the original; prod must not break without a backfill)."""
    # no manifest at all
    assert photos.variant_for("/photos/1/10/00.jpg", "card") is None
    assert photos.dims_for("/photos/1/10/00.jpg") is None

    _make_jpeg(root / "1" / "10" / "00.jpg")
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()
    assert photos.variant_for("/photos/other.jpg", "card") is None  # no entry
    assert photos.variant_for("/photos/1/10/00.jpg", "huge") is None  # unknown kind
    assert photos.variant_for("https://cdn/x.jpg", "card") is None  # not a local /photos url
    assert photos.variant_for("/photos/../etc/passwd", "card") is None  # traversal guard
    assert photos.dims_for("/photos/other.jpg") is None


def test_variant_for_rejects_tainted_manifest_entry(root: Path) -> None:
    """Defense-in-depth: even a corrupted manifest must not leak a traversal into the URL."""
    (root / photos.MANIFEST_NAME).write_text(
        json.dumps({"a.jpg": {"w": 1, "h": 1, "card": "../../etc/passwd", "detail": "/etc/passwd"}})
    )
    photos.invalidate_manifest_cache()

    assert photos.variant_for("/photos/a.jpg", "card") is None
    assert photos.variant_for("/photos/a.jpg", "detail") is None


def test_variant_for_requires_file_on_disk(root: Path) -> None:
    """A stale manifest entry whose variant file vanished → None (filter falls back to the
    original instead of emitting a 404 <img>)."""
    _make_jpeg(root / "00.jpg")
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()
    (root / "00.jpg.card.webp").unlink()

    assert photos.variant_for("/photos/00.jpg", "card") is None
    assert photos.variant_for("/photos/00.jpg", "detail") is not None


def test_manifest_cache_mtime_invalidation(root: Path, monkeypatch) -> None:
    """A rewritten manifest (backfill on the live server) is picked up without a restart
    once the throttle window passes — modelled here with interval=0 (always re-stat)."""
    monkeypatch.setattr(photos, "_MANIFEST_CHECK_INTERVAL", 0.0)
    mp = root / photos.MANIFEST_NAME
    mp.write_text(json.dumps({"a.jpg": {"w": 1, "h": 1, "card": "a.jpg.card.webp", "detail": "a.jpg.detail.webp"}}))
    assert set(photos.load_manifest(root)) == {"a.jpg"}

    mp.write_text(json.dumps({"b.jpg": {"w": 2, "h": 2, "card": "b.jpg.card.webp", "detail": "b.jpg.detail.webp"}}))
    import os

    os.utime(mp, ns=(mp.stat().st_atime_ns, mp.stat().st_mtime_ns + 1_000_000))

    assert set(photos.load_manifest(root)) == {"b.jpg"}


def test_manifest_cache_throttles_stat(root: Path, monkeypatch) -> None:
    """Within the throttle window the cached copy is served (stat-free hot path);
    invalidate_manifest_cache() forces the reload immediately."""
    monkeypatch.setattr(photos, "_MANIFEST_CHECK_INTERVAL", 3600.0)
    mp = root / photos.MANIFEST_NAME
    mp.write_text(json.dumps({"a.jpg": {"w": 1, "h": 1, "card": "c", "detail": "d"}}))
    assert set(photos.load_manifest(root)) == {"a.jpg"}

    mp.write_text(json.dumps({"b.jpg": {"w": 2, "h": 2, "card": "c", "detail": "d"}}))
    assert set(photos.load_manifest(root)) == {"a.jpg"}, "inside the window: cached copy"

    photos.invalidate_manifest_cache()
    assert set(photos.load_manifest(root)) == {"b.jpg"}


def test_load_manifest_tolerates_corrupt_json(root: Path, caplog) -> None:
    (root / photos.MANIFEST_NAME).write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="src.site.photos"):
        assert photos.load_manifest(root) == {}


# --------------------------------------------------------------------------- publish hook (§P.1 а)


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


async def _seed_match(db, photo_paths: list[str]) -> int:
    """Minimal chain channel→listing→inbound→match (mirrors test_auto_publish._seed_match)."""
    async with db() as s:
        ch = SourceChannel(tg_id=-1042, username="chan42", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE, external_id="e1", url="https://x/1",
            price_eur=700, photo_urls=["u1"], photo_phashes=["00"],
        )
        ib = InboundPost(
            source_channel_id=ch.id, tg_message_id=42, raw_text="",
            photo_paths=photo_paths, photo_phashes=["00"], status=InboundStatus.MATCHED,
        )
        s.add_all([listing, ib])
        await s.flush()
        m = Match(
            inbound_post_id=ib.id, listing_id=listing.id, confidence=0.9,
            matched_photo_count=1, method="phash", status=MatchStatus.PENDING,
        )
        s.add(m)
        await s.flush()
        await s.commit()
        return m.id


async def test_publish_hook_generates_variants_on_create(db, root: Path) -> None:
    """CREATE path: variants + manifest appear for the listing's inbound photos."""
    from src.admin.publish import auto_publish_site_listing

    photo = _make_jpeg(root / "1" / "42" / "00.jpg")
    match_id = await _seed_match(db, [str(photo)])

    async with db() as s:
        _row, created = await auto_publish_site_listing(s, match_id)
        await s.commit()

    assert created is True
    assert (root / "1" / "42" / "00.jpg.card.webp").is_file()
    assert (root / "1" / "42" / "00.jpg.detail.webp").is_file()
    assert set(_manifest(root)) == {"1/42/00.jpg"}


async def test_publish_hook_skips_existing_overlay(db, root: Path, monkeypatch) -> None:
    """created=False → no generation at all (re-marking a posted match must stay cheap)."""
    from src.admin.publish import auto_publish_site_listing

    photo = _make_jpeg(root / "1" / "42" / "00.jpg")
    match_id = await _seed_match(db, [str(photo)])
    async with db() as s:
        await auto_publish_site_listing(s, match_id)
        await s.commit()

    calls: list = []
    monkeypatch.setattr(photos, "generate_for_stored_paths", lambda paths: calls.append(paths))
    async with db() as s:
        _row, created = await auto_publish_site_listing(s, match_id)
        await s.commit()

    assert created is False
    assert calls == []


async def test_publish_hook_failure_never_breaks_publication(db, root: Path, monkeypatch, caplog) -> None:
    """Fault isolation: generation blowing up is logged as a warning, the overlay is still
    created and publicly visible."""
    from src.admin.publish import auto_publish_site_listing

    photo = _make_jpeg(root / "1" / "42" / "00.jpg")
    match_id = await _seed_match(db, [str(photo)])

    def _boom(paths):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(photos, "generate_for_stored_paths", _boom)
    with caplog.at_level(logging.WARNING):
        async with db() as s:
            row, created = await auto_publish_site_listing(s, match_id)
            await s.commit()

    assert created is True
    async with db() as s:
        overlay = (
            await s.execute(select(SiteListing).where(SiteListing.match_id == match_id))
        ).scalar_one()
        assert overlay.is_public is True
    assert "variant" in caplog.text.lower() or "photo" in caplog.text.lower()


async def test_publish_hook_ignores_paths_outside_root(db, root: Path, tmp_path: Path) -> None:
    """Photos stored outside photos_root (e.g. legacy /tmp paths) are silently skipped —
    the site can't serve them anyway, and publish must still succeed."""
    from src.admin.publish import auto_publish_site_listing

    outside = _make_jpeg(tmp_path / "elsewhere" / "00.jpg")
    match_id = await _seed_match(db, [str(outside), "https://cdn/x.jpg"])

    async with db() as s:
        _row, created = await auto_publish_site_listing(s, match_id)
        await s.commit()

    assert created is True
    assert _derived_files(root) == []
