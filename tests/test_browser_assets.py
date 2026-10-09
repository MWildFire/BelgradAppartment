"""Offline assets must remain the actual SRI-pinned application dependencies."""

import base64
import hashlib
import re
import shutil
from pathlib import Path

import pytest

from tests.e2e.offline_assets import VENDOR, load_assets


def test_vendored_assets_match_all_leaflet_integrity_pins():
    assets = load_assets()
    root = Path(__file__).resolve().parents[1]
    checked = set()
    for name in ("map.html", "apt.html"):
        text = (root / "src/site/templates" / name).read_text()
        for url, integrity in re.findall(
            r'(?:src|href)="(https://cdn\.jsdelivr\.net/[^\"]+)"\s+integrity="sha256-([^\"]+)"', text,
        ):
            assert url in assets
            raw, _ = assets[url]
            assert base64.b64encode(hashlib.sha256(raw).digest()).decode() == integrity
            checked.add(url)
    assert len(checked) == 5


def test_modified_vendor_file_is_rejected_before_browser_requests(tmp_path):
    shutil.copytree(VENDOR, tmp_path / "vendor")
    (tmp_path / "vendor/leaflet.js").write_bytes(b"unexpected content")
    with pytest.raises(ValueError, match="checksum differs"):
        load_assets(tmp_path / "vendor")
