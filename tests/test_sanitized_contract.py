"""Only synthetic DBs/images. No production data or network calls."""

import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image

from scripts import data_only_preflight as preflight
from scripts.sanitized_contract import (
    EXPECTED,
    FORMAT,
    SCOPE,
    SOURCE_REVISION,
    transform,
    validate_exact_diff,
    validate_scope_record,
)
from src.admin.routes import listing_photo
from src.db.models import Listing, Platform
from src.matching.evidence import _resolve
from src.matching.phash import compute_phash_from_path
from src.scrapers.orchestrator import _guess_extension
from tests.conftest import fresh_db


def synthetic_rows():
    sizes = (20, 20, 36, 35, 37)
    rows = []
    for row, size in enumerate(sizes):
        flagged = set(range(size)) if row < 2 else {size - 20}
        urls = []
        for i in range(size):
            marker = "sk-example-apartment-image" if i in flagged else "ordinary-photo"
            if row < 2:
                url = f"https://img.halooglasi.com/synthetic/example--{marker}-{i}.jpg"
            else:
                # Public synthetic HMAC fixture, unrelated to production signatures/keys.
                signature = (
                    "sk-RWdwACsAdoP5O7p5TZQETsbOHN_ozartcUViKy50"
                    if i in flagged
                    else "ordinary-signature"
                )
                url = (
                    f"https://resizer2.4zida.rs/{signature}/rs:fit:1200:0:0/"
                    "bG9jYWw6Ly8vc3ludGhldGljL2ltYWdl.webp"
                )
            urls.append(url)
        rows.append((row + 1, urls))
    return rows


def test_exact_sql_diff_preserves_other_fields_and_source(tmp_path):
    source = tmp_path / "source.db"
    copy_db = tmp_path / "copy.db"
    rows = synthetic_rows()
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, photo_urls TEXT, "
            "photo_phashes TEXT, photo_embeddings BLOB, photo_evidence TEXT, title TEXT)"
        )
        db.executemany(
            "INSERT INTO listings VALUES (?, ?, ?, ?, ?, ?)",
            [
                (identity, json.dumps(urls), '["unchanged"]', b"unchanged", "[]", "synthetic")
                for identity, urls in rows
            ],
        )
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('unchanged')")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    untouched = copy.deepcopy(rows)
    updates, receipt = transform(rows)
    assert rows == untouched and len(updates) == 5
    assert receipt["replaced_url_values"] == 43
    assert sum(len(urls) for _, urls in updates) == 148
    with sqlite3.connect(source) as original, sqlite3.connect(copy_db) as target:
        original.backup(target)
        target.executemany(
            "UPDATE listings SET photo_urls=? WHERE id=?",
            [(json.dumps(urls), identity) for identity, urls in updates],
        )
        target.commit()
        target.execute("VACUUM")
        for old, new in zip(
            original.execute("SELECT * FROM listings ORDER BY id"),
            target.execute("SELECT * FROM listings ORDER BY id"),
            strict=True,
        ):
            assert old[0] == new[0] and old[2:] == new[2:]
            old_urls, new_urls = json.loads(old[1]), json.loads(new[1])
            assert len(old_urls) == len(new_urls)
            for i, (a, b) in enumerate(zip(old_urls, new_urls, strict=True)):
                if a != b:
                    assert preflight.SECRET_PATTERNS[1].search(a.encode())
                    assert b == f"restore-local:///{i:03d}{_guess_extension(a)}"
                else:
                    assert not preflight.SECRET_PATTERNS[1].search(a.encode())
        assert list(original.execute("SELECT * FROM unrelated")) == list(
            target.execute("SELECT * FROM unrelated")
        )
        assert not target.execute("PRAGMA foreign_key_check").fetchall()
        assert target.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert preflight.inspect_database(copy_db)["result"] == "heuristic_scan_passed"
    with pytest.raises(preflight.PreflightError, match="credential_pattern_in_database"):
        preflight.inspect_database(source)


@pytest.mark.asyncio
async def test_current_admin_and_legacy_evidence_keep_all_148_photos(tmp_path, monkeypatch):
    rows = synthetic_rows()
    updates, _ = transform(rows)
    monkeypatch.setattr(
        "src.config.get_settings", lambda: SimpleNamespace(listings_photos_dir=tmp_path)
    )
    async with fresh_db() as (_, factory):
        for identity, urls in updates:
            platform = Platform.HALOOGLASI if identity < 3 else Platform.CETIRI_ZIDA
            folder = tmp_path / platform.value / f"synthetic-{identity}"
            folder.mkdir(parents=True)
            hashes = []
            for i in range(len(urls)):
                path = folder / f"{i:03d}{_guess_extension(urls[i])}"
                pixels = np.random.default_rng(identity * 100 + i).integers(
                    0, 256, size=(32, 32, 3), dtype=np.uint8
                )
                Image.fromarray(pixels).save(path)
                hashes.append(compute_phash_from_path(path))
            async with factory() as session:
                session.add(
                    Listing(
                        id=identity,
                        platform=platform,
                        external_id=f"synthetic-{identity}",
                        url="https://example.invalid/synthetic",
                        photo_urls=urls,
                        photo_phashes=hashes,
                        photo_evidence=[],
                    )
                )
                await session.commit()
            # Real runtime route; no HTTP, authentication material, or external model.
            for i, url in enumerate(urls):
                response = await listing_photo(identity, i, _="synthetic")
                assert Path(response.path) == folder / f"{i:03d}{_guess_extension(url)}"
            evidence = _resolve(list(folder.glob("*.*")), hashes)
            assert len(evidence) == len(urls)


@pytest.mark.asyncio
async def test_naive_removal_hides_a_whole_existing_album(tmp_path, monkeypatch):
    folder = tmp_path / Platform.HALOOGLASI.value / "synthetic"
    folder.mkdir(parents=True)
    Image.new("RGB", (16, 16), "red").save(folder / "000.jpg")
    monkeypatch.setattr(
        "src.config.get_settings", lambda: SimpleNamespace(listings_photos_dir=tmp_path)
    )
    async with fresh_db() as (_, factory):
        async with factory() as session:
            session.add(
                Listing(
                    id=1,
                    platform=Platform.HALOOGLASI,
                    external_id="synthetic",
                    url="https://example.invalid/synthetic",
                    photo_urls=[],
                )
            )
            await session.commit()
        with pytest.raises(HTTPException) as error:
            await listing_photo(1, 0, _="synthetic")
        assert error.value.status_code == 404 and (folder / "000.jpg").is_file()


@pytest.mark.parametrize("change", ["count", "host", "userinfo", "query", "ant", "malformed"])
def test_scope_drift_and_unreviewed_context_fail_closed(change):
    rows = synthetic_rows()
    if change == "count":
        rows[0][1].pop()
    elif change == "host":
        rows[0][1][0] = rows[0][1][0].replace("img.halooglasi.com", "other.invalid")
    elif change == "userinfo":
        rows[0][1][0] = rows[0][1][0].replace("https://", "https://fake:fake@")
    elif change == "query":
        rows[0][1][0] += "?extra=synthetic"
    elif change == "ant":
        rows[0][1][0] = rows[0][1][0].replace("sk-", "sk-ant-")
    else:
        rows[0][1][0] = 123
    with pytest.raises(preflight.PreflightError):
        transform(rows)


def test_sanitization_never_authorizes_other_secret_shaped_content(tmp_path):
    updates, _ = transform(synthetic_rows())
    database = tmp_path / "copy.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE listings (id INTEGER, photo_urls TEXT, description TEXT)")
        db.executemany(
            "INSERT INTO listings VALUES (?, ?, ?)",
            [
                (identity, json.dumps(urls), "Bearer synthetic_other_credential")
                for identity, urls in updates
            ],
        )
    with pytest.raises(preflight.PreflightError, match="credential_pattern_in_database"):
        preflight.inspect_database(database)


def scope_record():
    return {
        "format": FORMAT,
        "scope": SCOPE,
        "source_revision": SOURCE_REVISION,
        "revisions": ["0016_identity_edges"],
        "redaction": dict(EXPECTED),
        "marker_scheme": "restore-local",
        "array_positions_preserved": True,
        "other_database_values_unchanged": True,
        "protected_recovery_verified": False,
        "source_database_modified": False,
        "original_urls_retained": False,
        "credential_inspection": {"result": "heuristic_scan_passed", "scanner_exemptions": []},
        "sqlite_journal_mode": "delete",
        "sqlite_freelist_count": 0,
        "database_sidecars": [],
    }


def test_exact_43_markers_and_105_untouched_values_with_extra_unaffected_row():
    before = [*synthetic_rows(), (99, ["https://example.invalid/unchanged.jpg"])]
    after, summary = transform(before)
    assert summary == EXPECTED
    assert validate_exact_diff(before, after) == EXPECTED
    assert after[-1] == before[-1]
    assert sum(u.startswith("restore-local:") for _, urls in after for u in urls) == 43
    assert sum(not u.startswith("restore-local:") for _, urls in after for u in urls) == 106
    assert sum(len(urls) for _, urls in after) == 149


def test_one_photo_fewer_than_the_pinned_scope_is_refused():
    rows = synthetic_rows()
    identity, urls = rows[-1]
    with pytest.raises(preflight.PreflightError, match="redaction_scope_changed"):
        transform([*rows[:-1], (identity, urls[:-1])])


@pytest.mark.parametrize(
    "drift",
    [
        "duplicate_id",
        "source_distribution",
        "preexisting_marker",
        "signature_length",
        "source_encoding",
        "other_token_position",
        "wrong_marker_index",
        "array_compaction",
        "other_value",
    ],
)
def test_extra_drift_and_index_mutations_are_rejected(drift):
    before = synthetic_rows()
    if drift == "duplicate_id":
        before[1] = (before[0][0], before[1][1])
    elif drift == "source_distribution":
        before[0][1][-1] = "https://img.halooglasi.com/synthetic/ordinary.jpg"
        before[2][1][0] = before[2][1][16]
    elif drift == "preexisting_marker":
        before[2][1][0] = "restore-local:///000.webp"
    elif drift == "signature_length":
        before[2][1][16] = before[2][1][16].replace("sk-R", "sk-RR")
    elif drift == "source_encoding":
        before[2][1][16] = before[2][1][16].replace(
            "bG9jYWw6Ly8vc3ludGhldGljL2ltYWdl", "bm90LWxvY2Fs"
        )
    elif drift == "other_token_position":
        before[2][1][16] = (
            "https://resizer2.4zida.rs/ordinary/rs:fit:1200:0:0/sk-example-apartment-image.webp"
        )
    else:
        after, _ = transform(before)
        if drift == "wrong_marker_index":
            after[0][1][0] = "restore-local:///001.jpg"
        elif drift == "array_compaction":
            after[0][1].pop(0)
        else:
            after[2][1][0] = "https://example.invalid/changed.webp"
        with pytest.raises(preflight.PreflightError):
            validate_exact_diff(before, after)
        return
    with pytest.raises(preflight.PreflightError):
        transform(before)


def test_metadata_contract_does_not_claim_restore_transfer_or_deploy_permission():
    assert validate_scope_record(scope_record()) == {
        "result": "scope_record_valid_only",
        "transfer_authorized": False,
        "restore_verified": False,
        "deployment_ready": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"format": "renfinder-db-media-v1"},
        {"scope": "db_media"},
        {"source_revision": "a" * 40},
        {"revisions": ["other_schema"]},
        {"redaction": EXPECTED | {"replaced_url_values": 42}},
        {"array_positions_preserved": False},
        {"other_database_values_unchanged": False},
        {"protected_recovery_verified": True},
        {"source_database_modified": True},
        {"original_urls_retained": True},
        {
            "credential_inspection": {
                "result": "heuristic_scan_passed",
                "scanner_exemptions": ["URL"],
            }
        },
        {"sqlite_journal_mode": "wal"},
        {"sqlite_freelist_count": 1},
        {"database_sidecars": ["otz.db-wal"]},
        {"original_urls": ["synthetic_private_url"]},
        {"offhost_verified": True},
    ],
)
def test_sanitized_scope_metadata_rejects_unsafe_or_unproven_claims(change):
    with pytest.raises(preflight.PreflightError):
        validate_scope_record(scope_record() | change)


def test_candidate_gate_and_receiver_reject_sanitized_scope():
    from scripts import data_only_backup, release
    from tests.test_release_data_only import receipt

    with pytest.raises(release.ReleaseError, match="incomplete data-only recovery proof"):
        release.validate_backup(receipt() | {"backup_scope": SCOPE}, allow_data_only=True)
    with pytest.raises(preflight.PreflightError, match="unsupported_snapshot_scope"):
        data_only_backup.check_manifest(scope_record())


def test_additional_secret_category_in_selected_url_is_not_silently_removed():
    rows = synthetic_rows()
    rows[0][1][0] = rows[0][1][0].replace("/synthetic/", "/password=synthetic-private/")
    with pytest.raises(preflight.PreflightError, match="additional_secret_category"):
        transform(rows)


@pytest.mark.parametrize(
    "change",
    [
        {"redaction": EXPECTED | {"affected_cells": 5.0}},
        {
            "credential_inspection": {
                "result": "heuristic_scan_passed",
                "scanner_exemptions": [],
                "raw_url": "synthetic",
            }
        },
    ],
)
def test_metadata_has_exact_count_types_and_no_extra_scan_values(change):
    with pytest.raises(preflight.PreflightError):
        validate_scope_record(scope_record() | change)
