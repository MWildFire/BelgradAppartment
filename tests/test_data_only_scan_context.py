"""Synthetic counterexamples: a key-shaped match alone cannot identify a secret.

These deliberately retain the fail-closed backup gate. No production URL, title,
identifier, credential, or image is included in these fixtures.
"""

import base64
import hashlib
import hmac
import json
import sqlite3

import pytest

from scripts import data_only_preflight as preflight


def assert_photo_url_blocked(tmp_path, url):
    database = tmp_path / "synthetic.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE listings (photo_urls TEXT)")
        db.execute("INSERT INTO listings VALUES (?)", (json.dumps([url]),))
    with pytest.raises(preflight.PreflightError) as error:
        preflight.inspect_database(database)
    assert str(error.value) == "credential_pattern_in_database"
    assert url not in str(error.value)


def test_public_test_hmac_signature_can_match_provider_key_pattern(tmp_path):
    # Intentionally public fixture material, never an operational signing key.
    path = "/rs:fit:1200:0:0/bG9jYWw6Ly8vc3ludGhldGljL2ltYWdl.webp"
    digest = hmac.new(
        b"public-test-fixture-key",
        b"public-test-salt-4630" + path.encode(),
        hashlib.sha256,
    ).digest()
    signature = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert len(digest) == 32 and len(signature) == 43
    assert signature.startswith("sk-")
    assert_photo_url_blocked(
        tmp_path, f"https://resizer2.4zida.rs/{signature}{path}"
    )


def test_ordinary_synthetic_filename_can_match_but_stays_blocked(tmp_path):
    assert_photo_url_blocked(
        tmp_path,
        "https://img.halooglasi.com/synthetic/"
        "example--sk-example-apartment-image-001.jpg",
    )
