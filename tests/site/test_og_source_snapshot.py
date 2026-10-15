"""Conditional OG GETs follow the selected bytes, even across atomic replacement."""
from __future__ import annotations

import json
import os
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.config import get_settings
from src.site import og_image, routes
from src.site.app import create_app
from src.watermark import store
from tests.site.test_og_image import _view


def png(color):
    buf = BytesIO()
    Image.new("RGB", (800, 600), color).save(buf, "PNG", compress_level=0)
    return buf.getvalue()


@pytest.fixture
def scene(tmp_path, monkeypatch):
    root = tmp_path / "photos"
    root.mkdir()
    (root / "album").mkdir()
    original = root / "album" / "room.jpg"
    original.write_bytes(png((180, 70, 35)))
    settings = get_settings()
    monkeypatch.setattr(settings, "site_photos_root", str(root))
    monkeypatch.setattr(settings, "inbound_photos_dir", root)
    monkeypatch.setattr(settings, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(settings, "watermark_site_use_clean", False)
    monkeypatch.setattr(settings, "watermark_halo_corner_mode", "skip")
    clean = store.clean_path_for(original)
    clean.parent.mkdir(parents=True)
    clean.write_bytes(png((35, 120, 175)))
    store.sidecar_for(clean).write_text(json.dumps({
        "version": store.STORE_VERSION, "halo_corner_mode": "skip", "changed": True,
    }))
    view = _view(photos=["/photos/album/room.jpg"])

    async def public(*args, **kwargs):
        return view

    monkeypatch.setattr(routes.readmodel, "get_public", public)
    with TestClient(create_app()) as client:
        yield client, original, clean, settings, view


def get(client, etag=None):
    return client.get("/apt/1/og.png", headers={"If-None-Match": etag} if etag else {})


def test_original_to_clean_has_distinct_png_and_conditional_200(scene):
    client, _, _, settings, _ = scene
    before = get(client)
    settings.watermark_site_use_clean = True
    unconditional = get(client)
    assert before.content != unconditional.content  # real rendering, distinct PNGs first
    after = get(client, before.headers["etag"])
    assert after.status_code == 200
    assert after.content == unconditional.content
    assert after.headers["etag"] != before.headers["etag"]


@pytest.mark.parametrize("target", ["original", "clean"])
def test_same_path_same_size_same_mtime_replacement(scene, target):
    client, original, clean, settings, _ = scene
    settings.watermark_site_use_clean = target == "clean"
    path = clean if target == "clean" else original
    before = get(client)
    st = path.stat()
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(png((70, 165, 50)))
    assert replacement.stat().st_size == st.st_size
    os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
    replacement.replace(path)
    after = get(client, before.headers["etag"])
    assert after.status_code == 200
    assert after.content != before.content
    assert after.content == get(client).content


@pytest.mark.parametrize("action", ["missing", "discard", "flag_off", "invalid_mode", "invalid_version"])
def test_clean_to_original(scene, action):
    client, original, clean, settings, _ = scene
    before = get(client)
    settings.watermark_site_use_clean = True
    cleaned = get(client)
    if action == "missing":
        clean.unlink()
    elif action == "discard":
        assert store.discard(original)
    elif action == "flag_off":
        settings.watermark_site_use_clean = False
    else:
        meta = json.loads(store.sidecar_for(clean).read_text())
        meta["halo_corner_mode" if action == "invalid_mode" else "version"] = "obsolete"
        store.sidecar_for(clean).write_text(json.dumps(meta))
    reverted = get(client, cleaned.headers["etag"])
    assert reverted.status_code == 200
    assert reverted.content == before.content


@pytest.mark.parametrize("field", ["mode", "version"])
def test_result_policy_identity_even_when_pixels_unchanged(scene, monkeypatch, field):
    client, _, clean, settings, _ = scene
    settings.watermark_site_use_clean = True
    before = get(client)
    meta = json.loads(store.sidecar_for(clean).read_text())
    if field == "mode":
        settings.watermark_halo_corner_mode = "telea"
        meta["halo_corner_mode"] = "telea"
    else:
        monkeypatch.setattr(store, "STORE_VERSION", store.STORE_VERSION + 1)
        meta["version"] = store.STORE_VERSION
    store.sidecar_for(clean).write_text(json.dumps(meta))
    after = get(client, before.headers["etag"])
    assert after.status_code == 200
    assert after.content == before.content


def test_stable_304_does_not_render_or_decode(scene, monkeypatch):
    client, *_ = scene
    before = get(client)

    def forbidden(*args, **kwargs):
        pytest.fail("304 must not render/decode")

    monkeypatch.setattr(og_image, "render_og_png", forbidden)
    monkeypatch.setattr(Image, "open", forbidden)
    after = get(client, before.headers["etag"])
    assert after.status_code == 304 and not after.content


@pytest.mark.parametrize("listing_id", [4, 9999])
def test_public_gate_before_304(site_db, listing_id):
    from tests.site.test_og_image import _client

    with _client(site_db) as client:
        old = get(client).headers["etag"]
        assert client.get(f"/apt/{listing_id}/og.png", headers={"If-None-Match": old}).status_code == 404
        assert client.get(f"/apt/{listing_id}/og.png", headers={"If-None-Match": "*"}).status_code == 404


def test_listing_becomes_private_before_304(scene, monkeypatch):
    client, *_ = scene
    old = get(client).headers["etag"]

    async def private(*args, **kwargs):
        return None

    monkeypatch.setattr(routes.readmodel, "get_public", private)
    assert get(client, old).status_code == 404


def test_replacement_between_identity_and_render_uses_snapshot(scene, monkeypatch):
    client, original, _, _, _ = scene
    before = get(client)
    render = og_image.render_og_png

    def replace_then_render(*args):
        replacement = original.with_suffix(".new")
        replacement.write_bytes(png((20, 180, 50)))
        replacement.replace(original)
        return render(*args)

    monkeypatch.setattr(og_image, "render_og_png", replace_then_render)
    raced = get(client)
    assert raced.headers["etag"] == before.headers["etag"]
    assert raced.content == before.content  # no new bytes under the old validator
    monkeypatch.setattr(og_image, "render_og_png", render)
    after = get(client, raced.headers["etag"])
    assert after.status_code == 200 and after.content != raced.content


@pytest.mark.parametrize("unsafe", ["traversal", "absolute", "parent_link", "file_link", "dangling"])
def test_unsafe_original_falls_back_to_paper(scene, tmp_path, unsafe):
    _, original, _, _, view = scene
    outside = tmp_path / "external.jpg"
    outside.write_bytes(png((255, 0, 255)))
    if unsafe == "traversal":
        view.photos[:] = ["/photos/../external.jpg"]
    elif unsafe == "absolute":
        view.photos[:] = ["/photos/" + str(outside)]
    elif unsafe == "parent_link":
        (original.parent / "link").symlink_to(tmp_path, target_is_directory=True)
        view.photos[:] = ["/photos/album/link/external.jpg"]
    else:
        original.unlink()
        original.symlink_to(outside if unsafe == "file_link" else tmp_path / "absent")
    assert og_image.render_og_png(view) == og_image.render_og_png(_view(photos=[]))


@pytest.mark.parametrize("unsafe", ["clean_link", "parent_link", "missing", "corrupt"])
def test_unsafe_clean_original_fallback(scene, tmp_path, unsafe):
    client, original, clean, settings, _ = scene
    before = get(client)
    settings.watermark_site_use_clean = True
    if unsafe == "parent_link":
        moved = tmp_path / "moved"
        clean.parent.rename(moved)
        clean.parent.symlink_to(moved, target_is_directory=True)
    else:
        clean.unlink()
        if unsafe == "clean_link":
            clean.symlink_to(original)
        elif unsafe == "corrupt":
            clean.write_bytes(b"not a photo")
    after = get(client)
    if unsafe == "corrupt":
        # Decode failure is the established paper fallback, with no path disclosure.
        assert after.status_code == 200 and after.content.startswith(b"\x89PNG")
    else:
        assert after.content == before.content
    assert str(tmp_path).encode() not in after.content
