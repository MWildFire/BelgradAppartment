"""A complete directory restore uses a manifest hash, never a fabricated archive hash."""
from datetime import UTC, datetime

import pytest

from scripts.release import ReleaseError, validate_backup


def receipt():
    return {
        "result": "data_restore_verified",
        "created_at": datetime.now(UTC).isoformat(),
        "backup_format": "directory_generation",
        "generation_id": "20260908-103000-123abc45",
        "manifest_sha256": "a" * 64,
        "generation_manifest_sha256": "a" * 64,
        "offhost_verified": True,
        "protected_recovery_verified": True,
        "database": {"revisions": ["0011_keep_on_site"]},
        "application": {"catalog": 200, "apartment": 200, "photo": 200, "photo_decoded": True},
    }


def test_verified_directory_receipt_requires_no_archive():
    validate_backup(receipt())


@pytest.mark.parametrize("change", [
    {"backup_format": "unverified_media_seed"},
    {"generation_manifest_sha256": "b" * 64},
    {"manifest_sha256": ""},
    {"generation_id": "../pending"},
    {"protected_recovery_verified": False},
    {"result": "seed_only_unverified"},
    {"application": {"catalog": 200, "apartment": 200, "photo": 404}},
])
def test_partial_or_unverified_directory_cannot_unlock_release(change):
    candidate = receipt()
    candidate.update(change)
    with pytest.raises(ReleaseError):
        validate_backup(candidate)
