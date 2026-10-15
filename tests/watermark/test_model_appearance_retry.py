"""Unavailable corner fallback can recover without overriding individual reverts."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from src.config import get_settings
from src.watermark import pipeline, store


@pytest.fixture
def saved_fallback(tmp_path, monkeypatch):
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    original = inbound / "photo.jpg"
    original.write_bytes(b"frozen original fixture")
    settings = get_settings()
    monkeypatch.setattr(settings, "inbound_photos_dir", inbound)
    monkeypatch.setattr(settings, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(settings, "watermark_halo_corner_mode", "lama")
    monkeypatch.setattr(settings, "watermark_lama_enabled", True)
    model = tmp_path / "appearing-model.pt"
    monkeypatch.setattr(settings, "watermark_lama_model_path", model)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    clean = store.clean_path_for(original)
    assert clean is not None
    report = {"marks": [{"template_id": "halo_corner", "method": "skip"}]}
    store._write_sidecar(clean, {
        "version": store.STORE_VERSION, "halo_corner_mode": "lama",
        "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "changed": False, "report": report,
    })
    calls = []

    def clean_file(source, destination, **kwargs):
        calls.append((source, destination))
        destination.write_bytes(b"new derived bytes")
        result = {"marks": [{"template_id": "halo_corner", "method": "inpaint:lama"}]}
        return SimpleNamespace(changed=True, to_dict=lambda: result)

    monkeypatch.setattr(pipeline, "clean_file", clean_file)
    return original, model, calls


def test_new_model_invalidates_fallback_for_manual_and_scheduled_retry(saved_fallback):
    original, model, calls = saved_fallback
    before = original.read_bytes()
    assert store.is_current(original)
    assert store.ensure_clean(str(original)).reused
    model.write_bytes(b"availability fixture; inference is replaced by a fake")
    assert not store.is_current(original)
    result = store.ensure_clean(str(original))
    assert result is not None and not result.reused and result.clean is not None
    assert result.report["marks"][0]["method"] == "inpaint:lama"
    assert store.is_current(original)
    assert store.ensure_clean(str(original)).reused
    assert len(calls) == 1
    assert original.read_bytes() == before


def test_appearance_preserves_explicit_individual_discard(saved_fallback):
    original, model, calls = saved_fallback
    assert store.discard(original)
    model.write_bytes(b"available")
    assert store.is_current(original)
    result = store.ensure_clean(str(original))
    assert result is not None and result.reused and result.clean is None
    assert result.report == {"discarded": True}
    assert calls == []


@pytest.mark.parametrize("reason", ["disabled", "no_torch", "default_skip", "no_corner"])
def test_retry_is_limited_to_available_explicit_lama_corner_fallback(
    saved_fallback, monkeypatch, reason,
):
    original, model, calls = saved_fallback
    model.write_bytes(b"available")
    settings = get_settings()
    clean = store.clean_path_for(original)
    meta = store.read_sidecar(clean)
    if reason == "disabled":
        monkeypatch.setattr(settings, "watermark_lama_enabled", False)
    elif reason == "no_torch":
        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    elif reason == "default_skip":
        monkeypatch.setattr(settings, "watermark_halo_corner_mode", "skip")
        meta["halo_corner_mode"] = "skip"
    else:
        meta["report"] = {"marks": []}
    store._write_sidecar(clean, meta)
    assert store.is_current(original)
    assert store.ensure_clean(str(original)).reused
    assert calls == []
