"""Exact sanitized-v1 URL replacement contract; no I/O or credential exemptions."""

import base64
import re
from collections import Counter
from urllib.parse import urlsplit

from scripts import data_only_preflight as preflight

FORMAT = "renfinder-db-media-sanitized-v1"
SCOPE = "db_media_sanitized"
SOURCE_REVISION = "f477601b43593b35596e561d7b9a6d4a4a86ef23"
EXPECTED = {
    "affected_cells": 5,
    "replaced_url_values": 43,
    "halo_cells": 2,
    "halo_urls": 40,
    "resizer_cells": 3,
    "resizer_urls": 3,
    "affected_photo_slots": 148,
    "remaining_original_urls": 105,
}
MARKER = re.compile(r"restore-local:///([0-9]{3})\.(jpg|webp)")


def require(condition, code):
    preflight.require(condition, code)


def decode_canonical(value):
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise preflight.PreflightError("noncanonical_image_encoding") from None
    require(
        base64.urlsafe_b64encode(raw).decode().rstrip("=") == value, "noncanonical_image_encoding"
    )
    return raw


def selected_context(url, matches):
    """Bound removal to observed categories, without declaring any matching token safe."""
    require(
        all(not p.search(url.encode()) for i, p in enumerate(preflight.SECRET_PATTERNS) if i != 1),
        "additional_secret_category",
    )
    try:
        parts = urlsplit(url)
    except ValueError:
        raise preflight.PreflightError("invalid_photo_url") from None
    require(
        parts.scheme == "https" and not (parts.query or parts.fragment),
        "unreviewed_photo_url_context",
    )
    require(
        all(
            m.group().startswith(b"sk-") and not m.group().startswith((b"sk-ant-", b"sk-proj-"))
            for m in matches
        ),
        "unreviewed_provider_shape",
    )
    require(len(matches) == 1, "match_multiplicity_changed")
    token = matches[0].group().decode()
    segments = parts.path.split("/")[1:]
    require(segments and all(segments), "unreviewed_photo_path")
    if parts.netloc == "img.halooglasi.com":
        filename = segments[-1]
        require(
            re.fullmatch(r"[a-z0-9-]+\.jpg", filename)
            and "--" in filename
            and token in filename
            and all(len(word) <= 24 for word in filename[:-4].split("-")),
            "unreviewed_halo_filename",
        )
        return "halo", ".jpg"
    require(parts.netloc == "resizer2.4zida.rs", "unreviewed_photo_host")
    require(
        len(segments) >= 3
        and len(segments[0]) == 43
        and token in segments[0]
        and all(re.fullmatch(r"(?:rs|resize):[A-Za-z0-9:_-]+", p) for p in segments[1:-1])
        and segments[-1].endswith(".webp"),
        "unreviewed_resizer_path",
    )
    require(len(decode_canonical(segments[0])) == 32, "unreviewed_signature_length")
    source = decode_canonical(segments[-1][:-5])
    require(
        source.startswith(b"local:///") and not preflight.secret_like(source),
        "unreviewed_encoded_source",
    )
    return "resizer", ".webp"


def transform(rows):
    """Return all rows in the original order. No source values appear in the summary."""
    require(isinstance(rows, list), "invalid_row_collection")
    seen = set()
    after = []
    counts = Counter()
    for identity, urls in rows:
        require(
            type(identity) is int and identity > 0 and identity not in seen,
            "duplicate_or_invalid_row_identity",
        )
        seen.add(identity)
        require(
            isinstance(urls, list) and len(urls) <= 1000 and all(isinstance(u, str) for u in urls),
            "invalid_photo_urls",
        )
        changed = list(urls)
        categories = set()
        for index, url in enumerate(urls):
            require(not url.startswith("restore-local:"), "preexisting_marker")
            matches = list(preflight.SECRET_PATTERNS[1].finditer(url.encode()))
            if not matches:
                continue
            category, ext = selected_context(url, matches)
            changed[index] = f"restore-local:///{index:03d}{ext}"
            categories.add(category)
            counts[category + "_urls"] += 1
            counts["replaced_url_values"] += 1
        require(len(categories) <= 1, "mixed_source_row")
        if categories:
            counts["affected_cells"] += 1
            counts["affected_photo_slots"] += len(urls)
            counts[next(iter(categories)) + "_cells"] += 1
        after.append((identity, changed))
    counts["remaining_original_urls"] = (
        counts["affected_photo_slots"] - counts["replaced_url_values"]
    )
    require(dict(counts) == EXPECTED, "redaction_scope_changed")
    return after, dict(counts)


def validate_exact_diff(before, after):
    """Recompute expected values and compare every row/slot, including unchanged rows."""
    expected, summary = transform(before)
    require(after == expected, "unexpected_photo_array_diff")
    for (_, old), (_, new) in zip(before, after, strict=True):
        for index, (a, b) in enumerate(zip(old, new, strict=True)):
            if a != b:
                match = MARKER.fullmatch(b)
                require(match and int(match[1]) == index, "marker_index_mismatch")
    return summary


def validate_scope_record(record):
    """Supplemental format contract. Never substitutes for file/DB/restore verification."""
    require(isinstance(record, dict), "invalid_scope_record")
    require(
        record.get("format") == FORMAT and record.get("scope") == SCOPE, "wrong_sanitized_scope"
    )
    require(record.get("source_revision") == SOURCE_REVISION, "source_revision_changed")
    require(record.get("revisions") == ["0016_identity_edges"], "schema_changed")
    require(
        record.get("redaction") == EXPECTED
        and all(type(v) is int for v in record["redaction"].values()),
        "redaction_summary_changed",
    )
    require(
        record.get("marker_scheme") == "restore-local"
        and record.get("array_positions_preserved") is True
        and record.get("other_database_values_unchanged") is True,
        "preservation_unconfirmed",
    )
    require(
        record.get("protected_recovery_verified") is False
        and record.get("source_database_modified") is False
        and record.get("original_urls_retained") is False,
        "unsafe_scope_claim",
    )
    require(
        record.get("credential_inspection")
        == {
            "result": "heuristic_scan_passed",
            "scanner_exemptions": [],
        },
        "full_scan_unconfirmed",
    )
    require(
        record.get("sqlite_journal_mode") == "delete"
        and record.get("sqlite_freelist_count") == 0
        and type(record.get("sqlite_freelist_count")) is int
        and record.get("database_sidecars") == [],
        "deleted_values_may_remain",
    )
    require(
        set(record)
        == {
            "format",
            "scope",
            "source_revision",
            "revisions",
            "redaction",
            "marker_scheme",
            "array_positions_preserved",
            "other_database_values_unchanged",
            "protected_recovery_verified",
            "source_database_modified",
            "original_urls_retained",
            "credential_inspection",
            "sqlite_journal_mode",
            "sqlite_freelist_count",
            "database_sidecars",
        },
        "unexpected_scope_metadata",
    )
    # No accepted/offhost/deployment flag is produced by a metadata-only validator.
    return {
        "result": "scope_record_valid_only",
        "transfer_authorized": False,
        "restore_verified": False,
        "deployment_ready": False,
    }
