"""R02 review tails: failed photos, output paths, and unsafe store visibility."""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from src.config import get_settings
from src.watermark import cli, store
from src.watermark.pipeline import NotAnImageError


@pytest.mark.parametrize("failure", [NotAnImageError("bad image"), OSError("unreadable")])
def test_clean_match_continues_after_bad_photo(monkeypatch, failure):
    monkeypatch.setattr(cli, "_match_photo_paths", AsyncMock(return_value=["bad.jpg", "good.jpg"]))
    visited = []

    def clean(stored, *, force=False):
        visited.append((stored, force))
        if stored == "bad.jpg":
            raise failure
        return store.StoredResult(Path(stored), None, {}, False)

    monkeypatch.setattr(store, "ensure_clean", clean)
    result = CliRunner().invoke(cli.wm_clean_match, ["42", "--force"])
    assert result.exit_code == 1
    assert visited == [("bad.jpg", True), ("good.jpg", True)]
    assert "bad.jpg: error" in result.output
    assert "good.jpg: no marks" in result.output
    assert "total=2" in result.output and "errors=1" in result.output


def test_clean_match_success_summary(monkeypatch):
    monkeypatch.setattr(cli, "_match_photo_paths", AsyncMock(return_value=["good.jpg"]))
    monkeypatch.setattr(store, "ensure_clean",
                        lambda *a, **k: store.StoredResult(Path("good.jpg"), None, {}, False))
    result = CliRunner().invoke(cli.wm_clean_match, ["42"])
    assert result.exit_code == 0
    assert "total=1" in result.output and "errors=0" in result.output


def test_clean_match_missing_file_is_an_error(monkeypatch):
    monkeypatch.setattr(cli, "_match_photo_paths", AsyncMock(return_value=["gone.jpg"]))
    monkeypatch.setattr(store, "ensure_clean", lambda *a, **k: None)
    result = CliRunner().invoke(cli.wm_clean_match, ["42"])
    assert result.exit_code == 1
    assert "gone.jpg: file not on disk" in result.output
    assert "total=1" in result.output and "errors=1" in result.output


@pytest.mark.parametrize("kind", ["same", "child", "symlink"])
def test_qa_rejects_output_within_input_before_writing(tmp_path, kind):
    folder = tmp_path / "samples"
    folder.mkdir()
    (folder / "invalid.jpg").write_bytes(b"not an image")
    if kind == "same":
        out = folder
    elif kind == "child":
        out = folder / "nested" / "output"
    else:
        out = tmp_path / "alias"
        out.symlink_to(folder, target_is_directory=True)
    before = sorted(folder.rglob("*"))
    result = CliRunner().invoke(cli.wm_qa, [str(folder), "--out-dir", str(out)])
    assert result.exit_code == 2
    assert "--out-dir must lie outside" in result.output
    assert sorted(folder.rglob("*")) == before


def test_is_current_warns_once_per_call_without_retrying(monkeypatch, tmp_path, caplog):
    inbound = tmp_path / "inbound"
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(get_settings(), "watermark_clean_dir", inbound / "clean")
    # Alembic migration tests disable existing loggers through fileConfig.
    # Restore this test logger explicitly so caplog exercises the warning itself.
    monkeypatch.setattr(store.logger, "disabled", False)
    with caplog.at_level(logging.WARNING, logger="src.watermark.store"):
        assert store.is_current(inbound / "photo.jpg") is True
    warnings = [r for r in caplog.records if r.name == "src.watermark.store"]
    assert len(warnings) == 1
    assert "WATERMARK_CLEAN_DIR" in warnings[0].message
