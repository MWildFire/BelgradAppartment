"""Clean copies for public site photos: the sweep does each photo once; wm-revert sticks."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

import src.watermark.cli as wm_cli
from src.cli import cli
from src.config import get_settings
from src.db import get_session
from src.db.models import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)
from src.watermark import published, store
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def inbound(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "inbound"
    root.mkdir()
    s = get_settings()
    monkeypatch.setattr(s, "inbound_photos_dir", root)
    monkeypatch.setattr(s, "site_photos_root", root)
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "watermark_site_use_clean", False)
    return root


def _write_photos(root: Path, msg: int) -> list[str]:
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    paths = []
    for i in range(2):
        path = root / "1" / str(msg) / f"{i:02d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        img = apply_mark(make_background(1440, 1080, seed=40 + msg + i), tpl)
        Image.fromarray(img).save(path, "JPEG", quality=92)
        paths.append(str(path))
    return paths


async def _apartment(root: Path, msg: int, *, public: bool) -> list[str]:
    paths = _write_photos(root, msg)
    async with get_session() as s:
        if await s.get(SourceChannel, 1) is None:
            s.add(SourceChannel(id=1, tg_id=-100, username="src"))
        listing = Listing(
            platform=Platform.CETIRI_ZIDA, external_id=f"z{msg}", url=f"https://www.4zida.rs/{msg}"
        )
        post = InboundPost(source_channel_id=1, tg_message_id=msg, raw_text="", photo_paths=paths)
        s.add_all([listing, post])
        await s.flush()
        match = Match(
            listing_id=listing.id, inbound_post_id=post.id, confidence=1.0,
            matched_photo_count=2, method="phash", status=MatchStatus.PUBLISHED,
        )
        s.add(match)
        await s.flush()
        s.add(SiteListing(match_id=match.id, is_public=public))
    return paths


def _cleaned(paths: list[str]) -> bool:
    return all(store.clean_or_original(Path(p)) != Path(p).resolve() for p in paths)


def _corrupt(stored: str) -> None:
    Path(stored).write_bytes(b"not a photo")


async def test_sweep_cleans_public_photos_once(db, inbound):
    public = await _apartment(inbound, 31, public=True)
    hidden = await _apartment(inbound, 32, public=False)
    assert await published.sweep_once() == 2
    assert _cleaned(public)
    assert published.pending(hidden) == hidden  # apartments not on the site are left alone
    assert await published.sweep_once() == 0


async def test_sweep_skips_undecodable_photo(db, inbound):
    broken = await _apartment(inbound, 36, public=True)
    _corrupt(broken[0])
    valid = await _apartment(inbound, 37, public=True)
    await published.sweep_once()  # one broken file must not stop the sweep
    assert _cleaned(valid)


def test_wm_revert_keeps_the_original(inbound, monkeypatch):
    paths = _write_photos(inbound, 33)
    for stored in paths:
        store.ensure_clean(stored)
    assert _cleaned(paths)

    async def fake_paths(_match_id: int) -> list[str]:
        return paths

    monkeypatch.setattr(wm_cli, "_match_photo_paths", fake_paths)
    result = CliRunner().invoke(cli, ["wm-revert", "7"])
    assert result.exit_code == 0, result.output
    assert not any(store.clean_or_original(Path(p)) != Path(p).resolve() for p in paths)
    assert published.pending(paths) == []  # the decision sticks: no automatic re-cleaning
