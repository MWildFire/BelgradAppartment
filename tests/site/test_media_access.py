"""F16: knowing an inbound path is not permission to download its photo."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import get_settings
from src.site.app import create_app


@pytest.fixture
def media_client(site_db, tmp_path, monkeypatch):
    root = tmp_path / "inbound"
    root.mkdir()
    for name in ("public.jpg", "public.jpg.card.webp", "public.jpg.detail.webp", "private.jpg", "private.jpg.card.webp", "photo_manifest.json"):
        (root / name).write_bytes(name.encode())
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside-private")
    (root / "escape.jpg").symlink_to(outside)
    with sqlite3.connect(site_db.split(":///", 1)[1]) as db:
        db.execute("UPDATE inbound_posts SET photo_paths=? WHERE id=1", (json.dumps([str(root / "public.jpg")]),))
        db.execute("UPDATE inbound_posts SET photo_paths=? WHERE id=4", (json.dumps([str(root / "private.jpg")]),))
        # Public direct card with an inactive original still authorizes its media.
        db.execute("UPDATE listings SET is_active=0, is_primary=0 WHERE id=1")
    monkeypatch.setattr(get_settings(), "site_photos_root", str(root))
    monkeypatch.setattr(get_settings(), "database_url", site_db)
    with TestClient(create_app()) as client:
        yield client, site_db


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_public_original_and_variants_keep_existing_urls(media_client, method):
    client, _ = media_client
    for path in ("public.jpg", "public.jpg.card.webp", "public.jpg.detail.webp"):
        response = client.request(method, f"/photos/{path}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.content == (path.encode() if method == "GET" else b"")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_private_paths_and_escapes_are_denied(media_client, method):
    client, _ = media_client
    for path in ("private.jpg", "private.jpg.card.webp", "photo_manifest.json", "escape.jpg", "%2e%2e/outside.jpg"):
        assert client.request(method, f"/photos/{path}").status_code == 404


def test_manual_unpublish_revokes_next_media_request(media_client):
    client, url = media_client
    assert client.get("/photos/public.jpg").status_code == 200
    with sqlite3.connect(url.split(":///", 1)[1]) as db:
        db.execute("UPDATE site_listings SET is_public=0 WHERE id=1")
    assert client.get("/photos/public.jpg").status_code == 404


def test_caddy_cannot_bypass_publication_authorization():
    config = (Path(__file__).resolve().parents[2] / "deploy/Caddyfile").read_text()
    stanza = config.split("handle /photos/* {", 1)[1].split("}", 1)[0]
    assert "reverse_proxy 127.0.0.1:8001" in stanza
    assert "file_server" not in stanza and "root *" not in stanza
    assert 'Cache-Control "no-store"' in stanza


def test_photo_of_match_not_published_is_refused_even_with_public_overlay(media_client):
    # Overlay 1 stays is_public=1, but its match is back to PENDING: the photo must not be served.
    client, site_db = media_client
    assert client.get("/photos/public.jpg").status_code == 200  # control: PUBLISHED match
    with sqlite3.connect(site_db.split(":///", 1)[1]) as db:
        db.execute("UPDATE matches SET status='PENDING' WHERE id=1")
    assert client.get("/photos/public.jpg").status_code == 404
