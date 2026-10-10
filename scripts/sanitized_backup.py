#!/usr/bin/env python3
"""Explicit sanitized-v1 snapshots, isolated verification and a separate receiver.

No deployment, protected backup, retention, environment loading or implicit URL loss.
The acknowledgement flag is an operator guard, not a replacement for user consent.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import data_only_backup as plain
from scripts import data_only_preflight as scan
from scripts import offhost_backup as transport
from scripts import recovery
from scripts import sanitized_contract as policy

FORMAT = policy.FORMAT
SCOPE = policy.SCOPE
# A manifest entry is about 170 to 230 bytes in the real format (measured for 60 to 120 character
# names). 415,000 files therefore need 67 to 91 MiB; 256 MiB leaves about 2.8x headroom at 120.
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
SNAPSHOT_TIMEOUT_SECONDS = 6 * 60 * 60
REVISIONS = ["0016_identity_edges"]
MANIFEST_KEYS = {
    "format",
    "scope",
    "created_at",
    "source_project_root",
    "source_revision",
    "database",
    "credential_inspection",
    "components",
    "files",
    "photo_references",
    "sanitization",
    "source_snapshot_immutable",
    "protected_recovery_verified",
    "protected_recovery",
    "personal_data_present",
    "affected_albums",
}
ALBUM_RESULT = {"listings": 5, "photos": 147, "markers": 43}
SOURCE_SNAPSHOT = re.compile(r"/var/tmp/otz-data-only-sanitized/[0-9]{8}-[0-9]{6}-[a-f0-9]{8}")

# Durable record states; receive() documents what each one allows.
REQUESTED = "requested"
RETAINED = "retained"
TRANSFERRING = "transferring"
VERIFYING = "verifying"
VERIFIED = "verified"
KEPT_FOR_OPERATOR = "kept_for_operator"
KNOWN_STATES = frozenset(
    {REQUESTED, RETAINED, TRANSFERRING, VERIFYING, VERIFIED, KEPT_FOR_OPERATOR}
)
RESUMABLE_STATES = frozenset({RETAINED, TRANSFERRING})
# The name of the call _flush issues. Returned by _flush; it says the call was issued, nothing more.
DURABILITY_MODE = "F_FULLFSYNC"
# The bulk copy: F_FULLFSYNC issued after close, through a read-only descriptor. The power-loss
# result was not measured, so the receipt records that explicitly.
BULK_DURABILITY_MODE = "F_FULLFSYNC_issued_after_close"
POWER_LOSS_GUARANTEE = "not_established"
require = scan.require


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def read_json(path):
    require(path.resolve() == path and path.is_file(), "invalid_json_file")
    require(path.stat().st_size <= MAX_MANIFEST_BYTES, "manifest_size_limit")
    return json.loads(path.read_text(), object_pairs_hook=_unique_object)


def _urls(raw):
    require(isinstance(raw, str) and len(raw.encode()) <= scan.MAX_CELL_BYTES, "invalid_photo_urls")
    urls = json.loads(raw)
    require(
        isinstance(urls, list) and len(urls) <= 1000 and all(isinstance(u, str) for u in urls),
        "invalid_photo_urls",
    )
    return urls


def _extension(url):
    return next((ext for ext in (".webp", ".jpg", ".jpeg", ".png") if ext in url.lower()), ".jpg")


def _sidecars(db):
    require(
        not any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")),
        "database_sidecars_present",
    )
    with closing(recovery.ro_db(db)) as connection:
        require(
            connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            and connection.execute("PRAGMA freelist_count").fetchone() == (0,),
            "database_not_compacted",
        )


def _freeze(source, target):
    with closing(recovery.ro_db(source)) as incoming, closing(sqlite3.connect(target)) as outgoing:
        target.chmod(0o600)
        incoming.backup(outgoing, pages=1024, sleep=0.1)
        outgoing.execute("PRAGMA journal_mode=DELETE")


def _selection(db):
    selected = []
    with closing(recovery.ro_db(db)) as connection:
        for identity, raw in connection.execute("SELECT id,photo_urls FROM listings ORDER BY id"):
            urls = _urls(raw)
            require(not any(u.startswith("restore-local:") for u in urls), "preexisting_marker")
            if any(scan.SECRET_PATTERNS[1].search(u.encode()) for u in urls):
                selected.append((identity, urls))
    after, summary = policy.transform(selected)
    policy.validate_exact_diff(selected, after)
    return dict(after), summary


def _compare_databases(before, after, updates):
    """Compare every SQL value, streaming ordered rows, with one exact column exception."""
    changed = 0
    with closing(recovery.ro_db(before)) as a, closing(recovery.ro_db(after)) as b:
        schema_sql = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        require(
            a.execute(schema_sql).fetchall() == b.execute(schema_sql).fetchall(),
            "database_schema_changed",
        )
        tables = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        require(len(tables) <= 256, "schema_scan_limit")
        sentinel = object()
        for table in tables:
            info = a.execute(f"PRAGMA table_info({scan.identifier(table)})").fetchall()
            names = [r[1] for r in info]
            keys = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
            order = ",".join(map(scan.identifier, keys)) if keys else "rowid"
            query = f"SELECT * FROM {scan.identifier(table)} ORDER BY {order}"
            for old, new in zip_longest(a.execute(query), b.execute(query), fillvalue=sentinel):
                require(old is not sentinel and new is not sentinel, "database_row_count_changed")
                if old == new:
                    continue
                require(table == "listings", "unapproved_database_change")
                identity = old[names.index("id")]
                column = names.index("photo_urls")
                require(
                    identity in updates
                    and old[:column] == new[:column]
                    and old[column + 1 :] == new[column + 1 :]
                    and _urls(new[column]) == updates[identity],
                    "unapproved_database_change",
                )
                changed += 1
    require(changed == policy.EXPECTED["affected_cells"], "redaction_diff_count_changed")


def _album(root, platform, identity):
    require(
        platform in {"NEKRETNINE", "CETIRI_ZIDA", "HALOOGLASI", "CITYEXPERT"}
        and isinstance(identity, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity),
        "unsafe_listing_identity",
    )
    path = root / "data/listings" / platform / identity
    require(path.resolve() == path, "symlink_media_path")
    return path


def verify_affected_albums(db, media_root):
    """Reconstruct canonical slots and check accepted hashes/embeddings, never an LLM."""
    import numpy as np

    from src.matching.evidence import _resolve
    from src.matching.phash import compute_phash_from_path

    counts = Counter()
    totals = Counter()
    with closing(recovery.ro_db(db)) as connection:
        query = (
            "SELECT platform,external_id,photo_urls,photo_phashes,photo_embeddings,"
            "photo_evidence FROM listings"
        )
        for platform, identity, raw, hashes_raw, embeddings, evidence_raw in connection.execute(
            query
        ):
            urls = _urls(raw)
            markers = [
                (i, policy.MARKER.fullmatch(url))
                for i, url in enumerate(urls)
                if url.startswith("restore-local:")
            ]
            if not markers:
                continue
            require(
                all(match and int(match[1]) == index for index, match in markers),
                "marker_index_mismatch",
            )
            category = "halo" if platform == "HALOOGLASI" else "resizer"
            require(
                platform in {"HALOOGLASI", "CETIRI_ZIDA"}
                and all(
                    match[2] == ("jpg" if category == "halo" else "webp") for _, match in markers
                ),
                "marker_source_mismatch",
            )
            counts[category + "_cells"] += 1
            counts[category + "_urls"] += len(markers)
            counts["affected_cells"] += 1
            counts["replaced_url_values"] += len(markers)
            counts["affected_photo_slots"] += len(urls)
            folder = _album(media_root, platform, identity)
            paths = [folder / f"{i:03d}{_extension(url)}" for i, url in enumerate(urls)]
            require(set(recovery.files_below(folder)) == set(paths), "affected_album_inventory")
            hashes = json.loads(hashes_raw or "[]")
            require(
                isinstance(hashes, list)
                and len(hashes) == len(paths)
                and all(isinstance(h, str) and re.fullmatch(r"[a-f0-9]{16}", h) for h in hashes),
                "affected_photo_hashes",
            )
            require(
                isinstance(embeddings, bytes)
                and len(embeddings) == 1024 * len(hashes)
                and np.isfinite(np.frombuffer(embeddings, dtype="<f2")).all(),
                "affected_embeddings",
            )
            evidence = json.loads(evidence_raw or "[]")
            require(
                isinstance(evidence, list) and (not evidence or len(evidence) == len(paths)),
                "affected_evidence_manifest",
            )
            by_hash = {}
            slot_checksums = []
            size = 0
            for index, (path, phash) in enumerate(zip(paths, hashes, strict=True)):
                metadata = scan.inspect_image(path)
                slot_checksums.append(metadata["sha256"])
                size += metadata["size"]
                require(compute_phash_from_path(path) == phash, "affected_photo_content_changed")
                by_hash.setdefault(phash, set()).add(metadata["sha256"])
                if evidence:
                    require(
                        evidence[index]
                        == {"path": path.name, "phash": phash, "sha256": metadata["sha256"]},
                        "affected_evidence_manifest",
                    )
            require(size <= 32 * 1024 * 1024 and len(paths) <= 64, "affected_evidence_budget")
            require(
                evidence or all(len(values) == 1 for values in by_hash.values()),
                "ambiguous_legacy_evidence",
            )
            resolved = _resolve(paths, hashes, evidence, folder)
            require(
                [p.sha256 for p in resolved] == slot_checksums,
                "restored_evidence_alignment_changed",
            )
            totals.update(listings=1, photos=len(paths), markers=len(markers))
    counts["remaining_original_urls"] = (
        counts["affected_photo_slots"] - counts["replaced_url_values"]
    )
    require(
        dict(counts) == policy.EXPECTED and dict(totals) == ALBUM_RESULT,
        "restored_redaction_scope_changed",
    )
    return dict(totals)


def _media_paths(project, db):
    selected = set()
    with closing(recovery.ro_db(db)) as connection:
        for (raw,) in connection.execute("SELECT photo_paths FROM inbound_posts"):
            paths = json.loads(raw or "[]")
            require(isinstance(paths, list), "invalid_photo_references")
            for stored in paths:
                require(isinstance(stored, str), "invalid_photo_references")
                path = Path(stored)
                path = path if path.is_absolute() else project / path
                require(path.is_relative_to(project / "data/inbound"), "unsupported_photo_root")
                selected.add(path)
        for platform, identity in connection.execute("SELECT platform,external_id FROM listings"):
            folder = _album(project, platform, identity)
            if folder.exists():
                selected.update(recovery.files_below(folder))
    for original in tuple(selected):
        for suffix in (".card.webp", ".detail.webp"):
            if Path(str(original) + suffix).exists():
                selected.add(Path(str(original) + suffix))
    require(selected, "no_referenced_media")
    for path in selected:
        require(
            path.resolve() == path and scan.allowed_image(path.relative_to(project)),
            "media_outside_allowlist",
        )
    return sorted(selected)


def _scope_record():
    return {
        "format": FORMAT,
        "scope": SCOPE,
        "source_revision": policy.SOURCE_REVISION,
        "revisions": REVISIONS,
        "redaction": dict(policy.EXPECTED),
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


def _valid_inspection(value):
    return (
        isinstance(value, dict)
        and set(value)
        == {"result", "cells_scanned", "bytes_scanned", "arbitrary_secrets_absence_proven"}
        and value["result"] == "heuristic_scan_passed"
        and value["arbitrary_secrets_absence_proven"] is False
        and all(type(value[k]) is int and value[k] >= 0 for k in ("cells_scanned", "bytes_scanned"))
    )


def check_manifest(manifest):
    require(
        isinstance(manifest, dict) and set(manifest) == MANIFEST_KEYS,
        "invalid_sanitized_manifest_fields",
    )
    require(
        manifest["format"] == FORMAT and manifest["scope"] == SCOPE, "unsupported_sanitized_scope"
    )
    policy.validate_scope_record(manifest["sanitization"])
    database = manifest["database"]
    require(
        isinstance(database, dict)
        and set(database) == {"revisions", "table_counts", "schema_sha256"}
        and isinstance(database["table_counts"], dict)
        and set(database["table_counts"]) == set(recovery.expected_tables())
        and all(type(n) is int and n >= 0 for n in database["table_counts"].values())
        and all(database["table_counts"][n] > 0 for n in recovery.NONEMPTY_TABLES)
        and isinstance(database["schema_sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", database["schema_sha256"]),
        "invalid_database_metadata",
    )
    require(
        manifest["source_revision"] == policy.SOURCE_REVISION
        and database["revisions"] == REVISIONS,
        "source_revision_or_schema_changed",
    )
    require(isinstance(manifest["source_project_root"], str), "invalid_source_root")
    source = Path(manifest["source_project_root"])
    require(
        source.is_absolute()
        and ".." not in source.parts
        and source.as_posix() == manifest["source_project_root"],
        "invalid_source_root",
    )
    expected_components = {
        role: {
            "source": str(source / ("data/inbound" if role == "published" else "data/" + role)),
            "archive": "data/inbound" if role == "published" else "data/" + role,
        }
        for role in ("inbound", "listings", "published")
    }
    require(manifest["components"] == expected_components, "invalid_components")
    require(
        manifest["source_snapshot_immutable"] is True
        and manifest["protected_recovery_verified"] is False
        and manifest["protected_recovery"] == {"config_and_sessions": "excluded_by_scope"}
        and manifest["personal_data_present"] is True,
        "invalid_recovery_claim",
    )
    require(_valid_inspection(manifest["credential_inspection"]), "full_scan_unconfirmed")
    require(
        manifest["affected_albums"] == ALBUM_RESULT
        and all(type(v) is int for v in manifest["affected_albums"].values()),
        "affected_albums_unconfirmed",
    )
    require(
        type(manifest["photo_references"]) is int and manifest["photo_references"] > 0,
        "invalid_photo_references",
    )
    files = manifest["files"]
    require(
        isinstance(files, dict)
        and {"data/otz.db", "data/photo_manifest.json"} <= files.keys()
        and all(isinstance(n, str) and plain.allowed_member(n) for n in files),
        "forbidden_snapshot_member",
    )
    for metadata in files.values():
        require(
            isinstance(metadata, dict)
            and set(metadata) == {"size", "sha256"}
            and type(metadata["size"]) is int
            and metadata["size"] >= 0
            and isinstance(metadata["sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"]),
            "invalid_file_metadata",
        )
    require(isinstance(manifest["created_at"], str), "invalid_snapshot_time")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except ValueError:
        raise scan.PreflightError("invalid_snapshot_time") from None
    require(
        created.tzinfo is not None
        and 0 <= (datetime.now(UTC) - created).total_seconds() <= 36 * 3600,
        "stale_snapshot",
    )


def check_manifest_size(manifest):
    """Refuse a manifest the receiver would refuse to read, before the source publishes it.

    Measured on the bytes atomic_json writes: the same json.dumps call, with ASCII-only output.
    Returns the size in bytes.
    """
    size = len(json.dumps(manifest, sort_keys=True).encode())
    require(size <= MAX_MANIFEST_BYTES, "manifest_size_limit")
    return size


def _photo_index(media):
    photos = {}
    for name, metadata in media.items():
        if not name.startswith("data/inbound/") or name.endswith((".card.webp", ".detail.webp")):
            continue
        relative = name.removeprefix("data/inbound/")
        entry = {"w": metadata["width"], "h": metadata["height"]}
        for kind in ("card", "detail"):
            if name + f".{kind}.webp" in media:
                entry[kind] = relative + f".{kind}.webp"
        photos[relative] = entry
    return photos


def _progress(work, phase, completed=0, total=0, bytes_done=0):
    # Aggregate metadata lives beside the temporary snapshot and is never exported.
    transport.atomic_json(
        work / "progress.json",
        {
            "phase": phase,
            "completed": completed,
            "total": total,
            "bytes_done": bytes_done,
            "at_utc": datetime.now(UTC).isoformat(),
        },
    )


def _snapshot_remote(args, command):
    # The full source scan and byte verification run on the existing small VPS.
    # Keep an explicit upper bound and the existing lease guard; do not relax scans.
    return transport.run(
        [*transport.ssh(args), shlex.join([*transport.prefix(args), *map(str, command)])],
        timeout=SNAPSHOT_TIMEOUT_SECONDS,
    )


BUILD_LOCK = ".sanitized-build.lock"


@contextmanager
def _build_lock(parent):
    """Hold the build lock for one whole build; the kernel frees it if the build dies."""
    descriptor = os.open(parent / BUILD_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise scan.PreflightError("snapshot_build_running") from None
        yield
    finally:
        os.close(descriptor)


def _build_running(parent):
    """Read-only probe for a build that holds the lock. It never creates the lock file."""
    try:
        descriptor = os.open(parent / BUILD_LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


def create_snapshot(args):
    """Build one generation while holding the namespace's build lock; a second build is refused."""
    parent = args.output_dir.absolute().parent
    recovery.private_dir(parent)
    with _build_lock(parent):
        _remove_orphaned_builds(parent)
        return _create_snapshot(args)


def _remove_orphaned_builds(parent):
    """Remove build directories that a killed build left. Only called while holding the lock.

    Holding the lock proves that no other build of this namespace is running, so every
    .sanitized-build-* directory here is an orphan. Links and files of that name are left alone.
    """
    for path in parent.glob(".sanitized-build-*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)


def _create_snapshot(args):
    require(getattr(args, "acknowledge_url_loss", False) is True, "url_loss_not_acknowledged")
    require(math.isfinite(args.min_free_gib) and args.min_free_gib >= 0, "invalid_space_limit")
    project, output = args.project_root.absolute(), args.output_dir.absolute()
    require(
        project.resolve() == project
        and output.resolve() == output
        and not output.exists()
        and not output.is_relative_to(project),
        "invalid_snapshot_paths",
    )
    source_db = project / "data/otz.db"
    require(source_db.resolve() == source_db and source_db.is_file(), "invalid_database_path")
    require(
        read_json(project / "release.json")["revision"] == policy.SOURCE_REVISION,
        "source_revision_changed",
    )
    recovery.private_dir(output.parent)
    reserve = args.min_free_gib * 1024**3
    require(
        shutil.disk_usage(output.parent).free >= reserve + 3 * source_db.stat().st_size,
        "insufficient_snapshot_space",
    )
    with tempfile.TemporaryDirectory(prefix=".sanitized-build-", dir=output.parent) as work:
        work = Path(work)
        baseline, stage = work / "baseline.db", work / "snapshot"
        stage.mkdir(mode=0o700)
        (stage / "data").mkdir(mode=0o700)
        db = stage / "data/otz.db"
        _progress(work, "freeze_database")
        _freeze(source_db, baseline)
        original = recovery.database_manifest(baseline, REVISIONS)
        updates, _ = _selection(baseline)
        _freeze(baseline, db)
        with closing(sqlite3.connect(db)) as connection:
            connection.executemany(
                "UPDATE listings SET photo_urls=? WHERE id=?",
                [(json.dumps(urls), identity) for identity, urls in updates.items()],
            )
            connection.commit()
            connection.execute("VACUUM")
        _sidecars(db)
        _compare_databases(baseline, db, updates)
        require(recovery.database_manifest(db, REVISIONS) == original, "database_manifest_changed")
        baseline.unlink()  # Exact comparison is complete; discard original URLs immediately.
        inspection = scan.inspect_database(db)  # All fields; no URL/column exemptions.
        affected = verify_affected_albums(db, project)
        _progress(work, "select_media")
        selected = _media_paths(project, db)
        media = {}
        scanned_bytes = 0
        _progress(work, "inspect_source_media", total=len(selected))
        for index, path in enumerate(selected, 1):
            inspected = scan.inspect_image(path)
            media[path.relative_to(project).as_posix()] = inspected
            scanned_bytes += inspected["size"]
            if index % 250 == 0 or index == len(selected):
                _progress(work, "inspect_source_media", index, len(selected), scanned_bytes)
        require(
            shutil.disk_usage(stage).free >= reserve + sum(m["size"] for m in media.values()),
            "insufficient_snapshot_space",
        )
        files = {"data/otz.db": {"size": db.stat().st_size, "sha256": recovery.digest(db)}}
        copied_bytes = 0
        _progress(work, "copy_frozen_media", total=len(media))
        for index, (name, metadata) in enumerate(media.items(), 1):
            source = project / name
            require(source.resolve() == source, "symlink_media_path")
            with source.open("rb") as stream:
                raw = stream.read(scan.MAX_IMAGE_BYTES + 1)
            require(
                len(raw) == metadata["size"]
                and hashlib.sha256(raw).hexdigest() == metadata["sha256"],
                "media_changed",
            )
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                target.chmod(0o600)
                stream.write(raw)
            files[name] = {k: metadata[k] for k in ("size", "sha256")}
            copied_bytes += len(raw)
            if index % 250 == 0 or index == len(media):
                _progress(work, "copy_frozen_media", index, len(media), copied_bytes)
        index = stage / "data/photo_manifest.json"
        transport.atomic_json(index, _photo_index(media))
        files["data/photo_manifest.json"] = {
            "size": index.stat().st_size,
            "sha256": recovery.digest(index),
        }
        manifest = {
            "format": FORMAT,
            "scope": SCOPE,
            "created_at": datetime.now(UTC).isoformat(),
            "source_project_root": str(project),
            "source_revision": policy.SOURCE_REVISION,
            "database": original,
            "credential_inspection": inspection,
            "components": {
                role: {
                    "source": str(
                        project / ("data/inbound" if role == "published" else "data/" + role)
                    ),
                    "archive": "data/inbound" if role == "published" else "data/" + role,
                }
                for role in ("inbound", "listings", "published")
            },
            "files": files,
            "sanitization": _scope_record(),
            "affected_albums": affected,
            "source_snapshot_immutable": True,
            "protected_recovery_verified": False,
            "protected_recovery": {"config_and_sessions": "excluded_by_scope"},
            "personal_data_present": True,
        }
        manifest["photo_references"] = recovery.check_media_references(db, manifest, files)
        check_manifest(manifest)
        check_manifest_size(manifest)
        transport.atomic_json(stage / "manifest.json", manifest)
        verify_snapshot(
            stage,
            _scanned_media=media,
            progress=lambda done, total, size: _progress(
                work, "verify_frozen_snapshot", done, total, size
            ),
        )  # Recheck every frozen byte before publishing any snapshot.
        stage.rename(output)
    return {
        "result": "sanitized_source_snapshot_created",
        "files": len(files),
        "scope": SCOPE,
        "redaction": dict(policy.EXPECTED),
        "manifest_sha256": recovery.digest(output / "manifest.json"),
    }


def verify_snapshot(
    folder, *, application=False, progress=None, _scanned_media=None, scratch_parent=None
):
    require(not application or _scanned_media is None, "scan_reuse_for_restore_forbidden")
    folder = folder.absolute()
    require(folder.resolve() == folder and folder.is_dir(), "invalid_snapshot_directory")
    manifest = read_json(folder / "manifest.json")
    check_manifest(manifest)
    actual = {p.relative_to(folder).as_posix() for p in recovery.files_below(folder)}
    require(actual == set(manifest["files"]) | {"manifest.json"}, "snapshot_inventory_differs")
    media = {}
    if _scanned_media is not None:
        # Only create_snapshot supplies its in-memory results from the unchanged scanner.
        # The CLI and receiver always perform an independent complete scan.
        require(
            set(_scanned_media)
            == set(manifest["files"]) - {"data/otz.db", "data/photo_manifest.json"},
            "scanned_media_inventory_differs",
        )
    checked_bytes = 0
    if progress is not None:
        progress(0, len(manifest["files"]), 0)
    for index, (name, expected) in enumerate(manifest["files"].items(), 1):
        path = folder / name
        require(
            path.resolve() == path
            and path.stat().st_size == expected["size"]
            and recovery.digest(path) == expected["sha256"],
            "snapshot_checksum_differs",
        )
        if name not in {"data/otz.db", "data/photo_manifest.json"}:
            if _scanned_media is None:
                media[name] = scan.inspect_image(path)
            else:
                inspected = _scanned_media[name]
                require(
                    set(inspected) == {"size", "sha256", "width", "height"}
                    and all(inspected[k] == expected[k] for k in ("size", "sha256"))
                    and all(
                        type(inspected[k]) is int and inspected[k] > 0 for k in ("width", "height")
                    )
                    and inspected["width"] * inspected["height"] <= scan.MAX_IMAGE_PIXELS,
                    "scanned_media_record_differs",
                )
                # digest(path) above proved byte identity with the fully scanned input.
                # Reuse decode/credential results for those bytes, not a second decode.
                media[name] = inspected
        checked_bytes += expected["size"]
        if progress is not None and (index % 250 == 0 or index == len(manifest["files"])):
            progress(index, len(manifest["files"]), checked_bytes)
    db = folder / "data/otz.db"
    _sidecars(db)
    require(
        recovery.database_manifest(db, REVISIONS) == manifest["database"],
        "snapshot_database_differs",
    )
    require(
        scan.inspect_database(db) == manifest["credential_inspection"],
        "credential_inspection_differs",
    )
    require(
        verify_affected_albums(db, folder) == manifest["affected_albums"], "affected_albums_differ"
    )
    require(
        read_json(folder / "data/photo_manifest.json") == _photo_index(media), "photo_index_differs"
    )
    require(
        recovery.check_media_references(db, manifest, manifest["files"])
        == manifest["photo_references"],
        "snapshot_references_differ",
    )
    result = {
        "result": "sanitized_snapshot_verified",
        "backup_scope": SCOPE,
        "source_revision": policy.SOURCE_REVISION,
        "created_at": manifest["created_at"],
        "database": manifest["database"],
        "files": len(manifest["files"]),
        "credential_inspection": manifest["credential_inspection"],
        "generation_manifest_sha256": recovery.digest(folder / "manifest.json"),
        "sanitization": manifest["sanitization"],
        "affected_albums": manifest["affected_albums"],
        "media_files_verified": len(media),
        "media_decode_verified": True,
        "source_snapshot_immutable": True,
        "protected_recovery_verified": False,
        "health_receipt_published": False,
        "personal_data_present": True,
        "offhost_verified": False,
    }
    if application:
        # This process creates the scratch directory and removes it, also when the child is killed
        # at the timeout. A killed child therefore cannot leave a copy of the database behind. The
        # scratch sits outside the snapshot namespace unless the receiver names its copy as parent,
        # so a killed CLI process cannot leave a .sanitized-restore-* directory inside it.
        with tempfile.TemporaryDirectory(
            prefix=".sanitized-restore-", dir=scratch_parent or tempfile.gettempdir()
        ) as scratch:
            child = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "_smoke",
                    str(folder),
                    scratch,
                    "--application-project-root",
                    str(recovery.ROOT),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        require(child.returncode == 0, "offline_application_restore_failed")
        smoke = json.loads(child.stdout)
        require(
            smoke
            == {
                "application": {
                    "catalog": 200,
                    "apartment": 200,
                    "photo": 200,
                    "photo_decoded": True,
                    "network": "disabled",
                },
                "listing_albums": ALBUM_RESULT | {"network": "disabled"},
            },
            "offline_application_restore_failed",
        )
        result.update(result="data_restore_verified", **smoke)
        require(
            recovery.digest(folder / "manifest.json") == result["generation_manifest_sha256"],
            "snapshot_changed_during_restore",
        )
        require(
            actual == {p.relative_to(folder).as_posix() for p in recovery.files_below(folder)},
            "snapshot_inventory_differs",
        )
        for name, expected in manifest["files"].items():
            require(
                recovery.digest(folder / name) == expected["sha256"],
                "snapshot_changed_during_restore",
            )
    return result


def discard_snapshot(args):
    """Remove only the explicitly pinned, completed temporary source generation."""
    folder = args.directory.absolute()
    require(
        SOURCE_SNAPSHOT.fullmatch(str(folder)) and folder.resolve() == folder,
        "invalid_source_snapshot_path",
    )
    require(
        isinstance(args.manifest_sha256, str)
        and re.fullmatch(r"[a-f0-9]{64}", args.manifest_sha256),
        "invalid_manifest_checksum",
    )
    require(
        recovery.digest(folder / "manifest.json") == args.manifest_sha256,
        "source_manifest_changed",
    )
    manifest = read_json(folder / "manifest.json")
    check_manifest(manifest)
    require(manifest["source_project_root"] == "/opt/otz", "wrong_snapshot_source")
    actual = {p.relative_to(folder).as_posix() for p in recovery.files_below(folder)}
    require(actual == set(manifest["files"]) | {"manifest.json"}, "snapshot_inventory_differs")
    shutil.rmtree(folder)
    return {"result": "sanitized_source_snapshot_removed", "scope": SCOPE}


def _discard_remote(args, checksum):
    response = transport.remote(
        args,
        _helper_command(
            args,
            "discard-snapshot",
            args.source_snapshot,
            "--manifest-sha256",
            checksum,
            "--application-project-root",
            args.remote_root,
        ),
    )
    require(
        json.loads(response.stdout)
        == {
            "result": "sanitized_source_snapshot_removed",
            "scope": SCOPE,
        },
        "source_snapshot_cleanup_unconfirmed",
    )


def _valid_request_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def _resume_record(args, manifest_sha256, request_id, state=RETAINED):
    # The request id ties every state of one request to the operator's decision; the helper set
    # digest ties the record to the pinned helper that wrote it.
    from scripts import sanitized_helper_pin as pin  # receiver only; the source never imports it

    return {
        "format": FORMAT,
        "scope": SCOPE,
        "generation": args.source_snapshot.name,
        "source_snapshot": str(args.source_snapshot),
        "manifest_sha256": manifest_sha256,
        "request_id": request_id,
        "helper_set_sha256": pin.HELPER_SET_SHA256,
        "state": state,
    }


def _json_or_none(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _flush(descriptor):
    """Issue F_FULLFSYNC on the descriptor, or refuse. Never falls back to a plain fsync.

    fsync alone does not reach the platter on macOS, so a volume that declines F_FULLFSYNC is
    refused and the caller stops before the source is discarded. Issuing the call is not a
    power-loss measurement; no test in this repository makes one.
    """
    require(hasattr(fcntl, "F_FULLFSYNC"), "full_flush_unavailable")
    try:
        fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
    except OSError:
        raise scan.PreflightError("full_flush_refused") from None
    return DURABILITY_MODE


def _fsync_path(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        return _flush(descriptor)
    finally:
        os.close(descriptor)


def _durable_write(path, value):
    """Replace path so that a power loss keeps the old record or the new one, never a torn file.

    Order: the temporary file is written and flushed through the descriptor that wrote it, then
    closed, renamed over the record, and the parent directory is flushed. F_FULLFSYNC is issued at
    each flush; no power-loss test measures it. Leftover temporaries never became records. Only
    resume/ is swept, under the lease, because every resume write happens under the lease; other
    leftovers are ignored.
    """
    temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(value, sort_keys=True).encode())
            handle.flush()  # from Python's buffer to the file, so the flush below covers the bytes
            _flush(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_path(path.parent)


def _records(directory):
    """Entries that are records. A temporary, in progress or left over, is never a record."""
    return [path for path in directory.iterdir() if path.suffix != ".tmp"]


def _discard_temporaries(directory):
    """Remove leftover temporaries. Only call where no other process can be writing them."""
    for path in directory.glob("*.tmp"):
        path.unlink()


def _fsync_tree(folder):
    """Issue F_FULLFSYNC on every file, then every directory, deepest first.

    The files were written by rsync, so each call is issued after its writer closed the file,
    through a read-only descriptor. The result is BULK_DURABILITY_MODE: the call was issued, and no
    power-loss guarantee is claimed for it.
    """
    for path in sorted(folder.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if not path.is_symlink() and (path.is_file() or path.is_dir()):
            _fsync_path(path)
    _fsync_path(folder)
    return BULK_DURABILITY_MODE


def _publish_generation(pending, destination):
    pending.rename(destination)
    _fsync_path(pending.parent)
    _fsync_path(destination.parent)


def _helper_command(args, *helper_args):
    """The only command the receiver runs on the source helper.

    The bootstrap verifies the whole pinned helper set and runs exactly those bytes from memory,
    so the check and the run are one process and no helper module is imported from disk.
    """
    from scripts import sanitized_helper_pin as pin  # receiver only; the source never imports it

    return [
        args.remote_python,
        "-I",
        "-c",
        pin.HELPER_BOOT,
        str(args.remote_helper.parent.parent),
        pin.HELPER_SET_SHA256,
        json.dumps(sorted(pin.HELPER_FILES)),
        "scripts.sanitized_backup",
        *[str(part) for part in helper_args],
    ]


def _require_pinned_helper(args):
    """First helper run under the lease. The bootstrap refuses before the inspection can answer.

    A source build that still holds its lock refuses here, before any record is written.
    """
    require(_inspect_remote(args)["result"] != "build_running", "remote_build_running")


def _receiver_root(args):
    # Every local and remote input is checked against the reviewed namespace before any transport.
    require(args.remote_user == "otz", "unsupported_source_operator")
    root = args.private_root.absolute()
    require(
        root.resolve() == root and root.parts[-2:] == ("data-only", "sanitized-v1"),
        "separate_sanitized_namespace_required",
    )
    require(args.remote_root == Path("/opt/otz"), "unsupported_source_root")
    require(args.remote_python in transport.REMOTE_PYTHON_ALLOWLIST, "unsupported_remote_python")
    require(SOURCE_SNAPSHOT.fullmatch(str(args.source_snapshot)), "invalid_source_snapshot_path")
    require(
        re.fullmatch(
            r"/tmp/otz-sanitized-tools-[a-f0-9]{8}/scripts/sanitized_backup.py",
            str(args.remote_helper),
        ),
        "invalid_remote_helper_path",
    )
    return root


def _bind_namespace(root):
    for name in ("pending", "generations", "receipts", "resume"):
        recovery.private_dir(root / name)
    binding = {"format": FORMAT, "scope": SCOPE}
    if (root / "scope.json").exists():
        require(read_json(root / "scope.json") == binding, "namespace_scope_mismatch")
    else:
        require(
            not any(
                _records(root / name) for name in ("pending", "generations", "receipts", "resume")
            ),
            "unbound_generation_namespace",
        )
        _durable_write(root / "scope.json", binding)


def _require_resumable(args, resume_record):
    """Only a record whose copy was never verified may resume; every other state needs an operator.

    Returns the request id the record carries, so the resumed run keeps the same identity.
    """
    record = read_json(resume_record) if resume_record.exists() else None
    require(isinstance(record, dict), "resume_record_mismatch")
    state = record.get("state") if isinstance(record.get("state"), str) else None
    require(state not in KNOWN_STATES - RESUMABLE_STATES, "resume_requires_operator")
    request_id = record.get("request_id")
    require(
        state in RESUMABLE_STATES
        and _valid_request_id(request_id)
        and record == _resume_record(args, args.resume_manifest_sha256, request_id, state=state),
        "resume_record_mismatch",
    )
    return request_id


def _inspect_remote(args):
    """Read-only remote answer for one generation: absent, or present with its manifest digest."""
    response = transport.remote(
        args,
        _helper_command(
            args,
            "inspect-snapshot",
            args.source_snapshot,
            "--application-project-root",
            args.remote_root,
        ),
    )
    status = _json_or_none(response.stdout)
    if status == {"result": "snapshot_absent", "scope": SCOPE}:
        return {"result": "snapshot_absent"}
    if status == {"result": "snapshot_build_running", "scope": SCOPE}:
        return {"result": "build_running"}
    require(
        isinstance(status, dict)
        and set(status) == {"result", "scope", "manifest_sha256"}
        and status["result"] == "snapshot_present"
        and status["scope"] == SCOPE
        and isinstance(status["manifest_sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", status["manifest_sha256"]),
        "remote_state_ambiguous",
    )
    return {"result": "snapshot_present", "manifest_sha256": status["manifest_sha256"]}


def receive(args):
    """Explicit receiver for one generation: bounded batches, no seed, hardlinks or retention.

    Each step writes its durable record before it runs, so a restart can tell where it stopped.
    States in resume/<generation>.json:
      requested          snapshot request sent, no digest yet (operator: resolve_requested)
      retained           remote snapshot digest known, nothing copied locally yet
      transferring       local copy is incomplete or unverified; a resume may replace it
      verifying          transfer finished; the verification result is not recorded yet
      verified           verification passed and the result is recorded
      kept_for_operator  the source may be discarded or already is; the copy is kept
    A local copy is deleted only in retained and transferring, where no verification has
    succeeded. A verification that raises is written down as transferring before its copy is
    removed. Once verification has succeeded, or a restart finds verifying, the copy is kept and
    the record blocks resume and fresh runs until an operator resolves it. The record is removed
    last, after the generation and its receipt are durable.

    Record checks run twice: before the lease, as refusals that contact nothing, and again under
    the lease after the helper is verified. Only the second set decides, because another receiver
    may write a record while this one waits for the lease.
    """
    # Local receiver only: the remote helper never imports the batching module.
    from scripts import bounded_transfer as bounded

    require(getattr(args, "acknowledge_url_loss", False) is True, "url_loss_not_acknowledged")
    root = _receiver_root(args)
    require(math.isfinite(args.min_free_gib) and args.min_free_gib >= 0, "invalid_space_limit")
    recovery.private_dir(root)
    _bind_namespace(root)
    generation = args.source_snapshot.name
    pending, destination = root / "pending" / generation, root / "generations" / generation
    resume_record = root / "resume" / (generation + ".json")
    receipt = root / "receipts" / (generation + ".json")
    listing = root / "pending" / (generation + ".files")
    resuming = getattr(args, "resume_manifest_sha256", None) is not None
    if resuming:
        require(
            re.fullmatch(r"[a-f0-9]{64}", args.resume_manifest_sha256),
            "invalid_resume_manifest_sha256",
        )
        _require_resumable(args, resume_record)
    require(not destination.exists() and not receipt.exists(), "generation_exists")
    if not resuming:
        require(not pending.exists(), "generation_exists")
        # An open record names a requested, retained or kept generation; a fresh run adds none.
        require(not _records(root / "resume"), "unresolved_resume_record")
    rsync = [
        "/usr/bin/rsync",
        "-rt",
        "--rsync-path",
        shlex.join([*transport.prefix(args), "/usr/bin/rsync"]),
        "-e",
        shlex.join(transport.ssh(args)[:-1]),
    ]
    source_checksum = None
    verify_may_have_started = False
    created_copy = False  # True once this run made the copy; only such a copy is ever removed
    try:
        with transport.remote_lease(args):
            _require_pinned_helper(args)
            # Holding the lease, no other receiver is writing resume/: a temporary there was left
            # by a receiver that was killed while it wrote a record.
            _discard_temporaries(root / "resume")
            # The same checks again, now that this process holds the lease: another receiver may
            # have written a record or finished the generation while this one waited for it.
            require(not destination.exists() and not receipt.exists(), "generation_exists")
            if resuming:
                request_id = _require_resumable(args, resume_record)
                source_checksum = args.resume_manifest_sha256
                # Retained or transferring: no verification succeeded; the copy is not evidence.
                if pending.exists():
                    shutil.rmtree(pending)
            else:
                require(not _records(root / "resume"), "unresolved_resume_record")
                require(not pending.exists(), "generation_exists")
                # Written before the call: a lost answer leaves a record that blocks fresh runs.
                request_id = uuid.uuid4().hex
                _durable_write(
                    resume_record, _resume_record(args, None, request_id, state=REQUESTED)
                )
                source = _snapshot_remote(
                    args,
                    _helper_command(
                        args,
                        "snapshot",
                        "--project-root",
                        args.remote_root,
                        "--output-dir",
                        args.source_snapshot,
                        "--application-project-root",
                        args.remote_root,
                        "--min-free-gib",
                        args.min_free_gib,
                        "--acknowledge-url-loss",
                    ),
                )
                status = json.loads(source.stdout)
                require(
                    status.get("result") == "sanitized_source_snapshot_created"
                    and status.get("scope") == SCOPE
                    and re.fullmatch(r"[a-f0-9]{64}", status.get("manifest_sha256", "")),
                    "source_snapshot_unconfirmed",
                )
                source_checksum = status["manifest_sha256"]
                _durable_write(
                    resume_record, _resume_record(args, source_checksum, request_id, state=RETAINED)
                )
            pending.mkdir(mode=0o700)
            created_copy = True
            _durable_write(
                resume_record, _resume_record(args, source_checksum, request_id, state=TRANSFERRING)
            )
            transport.run(
                [
                    *rsync,
                    f"{args.ssh_target}:{args.source_snapshot}/manifest.json",
                    str(pending / "manifest.json"),
                ]
            )
            require(
                recovery.digest(pending / "manifest.json") == source_checksum,
                "source_manifest_changed",
            )
            manifest = read_json(pending / "manifest.json")
            check_manifest(manifest)
            require(
                manifest["source_project_root"] == str(args.remote_root), "wrong_snapshot_source"
            )
            require(
                shutil.disk_usage(root).free
                >= args.min_free_gib * 1024**3 + sum(m["size"] for m in manifest["files"].values()),
                "insufficient_receiver_space",
            )
            # Materialized first: every name is validated before the first transfer.
            groups = list(
                bounded.batches(
                    manifest["files"],
                    sizes={name: meta["size"] for name, meta in manifest["files"].items()},
                )
            )
            for group in groups:
                listing.write_bytes(bounded.encode_batch(group))
                listing.chmod(0o600)
                transport.run(
                    [
                        *rsync,
                        "-I",
                        "-0",
                        "--files-from",
                        str(listing),
                        f"{args.ssh_target}:{args.source_snapshot}/",
                        str(pending) + "/",
                    ]
                )
                transport.assert_lease()
            listing.unlink(missing_ok=True)
            # Recorded before verification starts: a result that is never written down still
            # leaves a record that refuses the deletion a resume would otherwise perform.
            verify_may_have_started = True
            _durable_write(
                resume_record, _resume_record(args, source_checksum, request_id, state=VERIFYING)
            )
            try:
                result = verify_snapshot(pending, application=True, scratch_parent=pending)
            except BaseException:
                # The copy is not evidence: write that down, then let the handler remove it. The
                # source snapshot is still on the server, because the discard has not run yet.
                _durable_write(
                    resume_record,
                    _resume_record(args, source_checksum, request_id, state=TRANSFERRING),
                )
                verify_may_have_started = False
                raise
            _durable_write(
                resume_record, _resume_record(args, source_checksum, request_id, state=VERIFIED)
            )
            transport.assert_lease()
            require(
                recovery.digest(pending / "manifest.json") == source_checksum,
                "source_manifest_changed",
            )
            # A volume that refuses F_FULLFSYNC raises here, before the source can be discarded.
            durability_mode = _fsync_tree(pending)
            # The copy's own entry lives in pending/: its directory is flushed the same way, after
            # close, and that is not measured as a power-loss guarantee either.
            _fsync_path(pending.parent)
            # Recorded before the call: a crash or a lost answer during discard keeps the copy.
            _durable_write(
                resume_record,
                _resume_record(args, source_checksum, request_id, state=KEPT_FOR_OPERATOR),
            )
            _discard_remote(args, source_checksum)
            transport.assert_lease()
            result.update(
                backup_format="sanitized_directory_generation",
                generation_id=generation,
                manifest_sha256=result["generation_manifest_sha256"],
                offhost_verified=True,
                files_transferred=len(manifest["files"]),
                media_reused_from_verified_seed=0,
                source_temporary_snapshot_removed=True,
                durability_mode=durability_mode,
                power_loss_guarantee=POWER_LOSS_GUARANTEE,
            )
        # Lease release can itself fail. Publish proof only after the context exits cleanly.
        _publish_generation(pending, destination)
        _durable_write(receipt, result)
        resume_record.unlink(missing_ok=True)
        _fsync_path(resume_record.parent)
        return result
    except BaseException:
        # A copy is removed here only if this run made it and no verification has started. Its
        # record says so (requested, retained or transferring). Anything else is kept for the
        # operator, including a copy another receiver left behind.
        if created_copy and not verify_may_have_started and pending.exists():
            shutil.rmtree(pending)
        raise
    finally:
        listing.unlink(missing_ok=True)


def _require_requested(args, record_path):
    """The open request the operator names, written by the pinned helper set and unchanged."""
    from scripts import sanitized_helper_pin as pin  # receiver only; the source never imports it

    record = read_json(record_path)
    require(
        isinstance(record, dict) and record.get("request_id") == args.request_id,
        "request_id_mismatch",
    )
    require(record.get("helper_set_sha256") == pin.HELPER_SET_SHA256, "helper_pin_mismatch")
    require(
        record == _resume_record(args, None, args.request_id, state=REQUESTED),
        "requested_record_mismatch",
    )


def resolve_requested(args):
    """Operator decision for a requested record whose snapshot answer was lost.

    The decision must echo the request id the record carries, and the record must come from the
    pinned helper set. keep adopts a remote snapshot that exists: its manifest digest becomes a
    retained record under the same request id, so resume can use it. abandon removes the record
    only when the remote proves that no snapshot exists and no source build is running. Every
    mismatch, ambiguous or unexpected answer refuses and leaves every file unchanged.
    """
    require(args.decision in ("keep", "abandon"), "invalid_decision")
    require(_valid_request_id(args.request_id), "invalid_request_id")
    root = _receiver_root(args)
    require(
        (root / "scope.json").exists()
        and read_json(root / "scope.json") == {"format": FORMAT, "scope": SCOPE},
        "namespace_scope_mismatch",
    )
    generation = args.source_snapshot.name
    record_path = root / "resume" / (generation + ".json")
    require(record_path.exists(), "requested_record_missing")
    _require_requested(args, record_path)
    require(not (root / "pending" / generation).exists(), "pending_copy_present")
    with transport.remote_lease(args):
        _require_requested(args, record_path)  # read again: the lease is what makes it current
        remote = _inspect_remote(args)
        require(remote["result"] != "build_running", "remote_build_running")
        if args.decision == "abandon":
            require(remote["result"] == "snapshot_absent", "remote_snapshot_present")
            record_path.unlink()
            _fsync_path(record_path.parent)
            return {"result": "requested_record_abandoned", "generation": generation}
        require(remote["result"] == "snapshot_present", "remote_snapshot_absent")
        _durable_write(
            record_path,
            _resume_record(args, remote["manifest_sha256"], args.request_id, state=RETAINED),
        )
    return {
        "result": "requested_record_kept",
        "generation": generation,
        "manifest_sha256": remote["manifest_sha256"],
    }


def inspect_snapshot(args):
    """Read-only answer for an operator resolution: never creates, changes or removes anything."""
    folder = args.directory.absolute()
    require(
        SOURCE_SNAPSHOT.fullmatch(str(folder)) and folder.resolve() == folder,
        "invalid_source_snapshot_path",
    )
    if _build_running(folder.parent):
        return {"result": "snapshot_build_running", "scope": SCOPE}
    if not os.path.lexists(folder):
        return {"result": "snapshot_absent", "scope": SCOPE}
    manifest = folder / "manifest.json"
    require(
        folder.is_dir()
        and not folder.is_symlink()
        and manifest.is_file()
        and not manifest.is_symlink(),
        "snapshot_state_ambiguous",
    )
    return {
        "result": "snapshot_present",
        "scope": SCOPE,
        "manifest_sha256": recovery.digest(manifest),
    }


def _smoke(folder, scratch):
    """Child process: disposable restore databases go only into the scratch its parent removes."""
    require(
        scratch.is_dir() and scratch.name.startswith(".sanitized-restore-"),
        "invalid_restore_scratch",
    )
    previous = tempfile.tempdir
    try:
        tempfile.tempdir = str(scratch)
        return _offline_smoke(folder)
    finally:
        tempfile.tempdir = previous


def _offline_smoke(folder):
    """Child process only: clean environment, no DNS/connect, disposable DB for app writes."""
    os.environ.clear()
    import socket

    def deny(*_args, **_kwargs):
        raise RuntimeError("offline_restore_network_disabled")

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, deny)
    for name in (
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "create_connection",
    ):
        setattr(socket, name, deny)
    application = recovery.application_smoke(folder)
    with tempfile.TemporaryDirectory(prefix="sanitized-admin-db-") as work:
        db = Path(work) / "otz.db"
        shutil.copyfile(folder / "data/otz.db", db)
        import asyncio

        import httpx

        import src.config as config

        settings = config.Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{db}",
            data_dir=folder / "data",
            listings_photos_dir=folder / "data/listings",
            tg_bot_token="",
            tg_bot_recipients="",
            site_subscription_telegram_bot_token="",
        )
        config.get_settings = lambda: settings
        from PIL import Image

        from src.admin.app import create_app
        from src.admin.auth import require_admin
        from src.db.session import dispose_engine, init_engine

        async def albums():
            await dispose_engine()
            init_engine(settings.database_url)
            app = create_app()
            app.dependency_overrides[require_admin] = lambda: "offline-restore"
            totals = Counter()
            # Finish the read cursor before the real application enables WAL on its copy.
            selected = []
            with closing(recovery.ro_db(db)) as connection:
                for identity, raw in connection.execute("SELECT id,photo_urls FROM listings"):
                    urls = _urls(raw)
                    markers = sum(u.startswith("restore-local:") for u in urls)
                    if markers:
                        selected.append((identity, len(urls), markers))
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://restore.invalid"
                ) as client:
                    for identity, length, markers in selected:
                        for index in range(length):
                            response = await client.get(f"/photo/listing/{identity}/{index}")
                            require(response.status_code == 200, "restored_listing_photo_failed")
                            with Image.open(io.BytesIO(response.content)) as image:
                                image.load()
                        totals.update(listings=1, photos=length, markers=markers)
                require(dict(totals) == ALBUM_RESULT, "restored_listing_scope_changed")
                return dict(totals) | {"network": "disabled"}
            finally:
                await dispose_engine()

        return {"application": application, "listing_albums": asyncio.run(albums())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--project-root", required=True, type=Path)
    snapshot.add_argument("--output-dir", required=True, type=Path)
    snapshot.add_argument("--min-free-gib", type=float, default=20)
    snapshot.add_argument("--acknowledge-url-loss", action="store_true")
    verify = commands.add_parser("verify")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--receipt", type=Path, required=True)
    receiver = commands.add_parser("receive")
    for name in ("private-root", "source-snapshot", "remote-root", "remote-helper", "ssh-key"):
        receiver.add_argument("--" + name, type=Path, required=True)
    for name in ("ssh-target", "remote-python"):
        receiver.add_argument("--" + name, required=True)
    receiver.add_argument("--remote-user", default="otz", choices=["otz"])
    receiver.add_argument("--min-free-gib", type=float, default=20)
    receiver.add_argument("--acknowledge-url-loss", action="store_true")
    receiver.add_argument("--resume-manifest-sha256")
    smoke = commands.add_parser("_smoke")
    smoke.add_argument("directory", type=Path)
    smoke.add_argument("scratch", type=Path)
    discard = commands.add_parser("discard-snapshot")
    discard.add_argument("directory", type=Path)
    discard.add_argument("--manifest-sha256", required=True)
    inspect = commands.add_parser("inspect-snapshot")
    inspect.add_argument("directory", type=Path)
    resolver = commands.add_parser("resolve-requested")
    for name in ("private-root", "source-snapshot", "remote-root", "remote-helper", "ssh-key"):
        resolver.add_argument("--" + name, type=Path, required=True)
    for name in ("ssh-target", "remote-python"):
        resolver.add_argument("--" + name, required=True)
    resolver.add_argument("--remote-user", default="otz", choices=["otz"])
    resolver.add_argument("--decision", required=True, choices=["keep", "abandon"])
    resolver.add_argument("--request-id", required=True)
    for subparser in (snapshot, verify, receiver, smoke, discard, inspect, resolver):
        subparser.add_argument("--application-project-root", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.application_project_root:
        recovery.ROOT = args.application_project_root.resolve()
        sys.path.insert(0, str(recovery.ROOT))
    try:
        if args.command == "snapshot":
            result = create_snapshot(args)
        elif args.command == "receive":
            result = receive(args)
        elif args.command == "resolve-requested":
            result = resolve_requested(args)
        elif args.command == "_smoke":
            result = _smoke(args.directory.absolute(), args.scratch.absolute())
        elif args.command == "discard-snapshot":
            result = discard_snapshot(args)
        elif args.command == "inspect-snapshot":
            result = inspect_snapshot(args)
        else:
            require(
                not args.receipt.absolute().is_relative_to(args.directory.absolute()),
                "receipt_inside_immutable_snapshot",
            )
            require(
                args.receipt.absolute().resolve() == args.receipt.absolute()
                and args.receipt.suffix == ".json"
                and not args.receipt.exists(),
                "invalid_or_existing_receipt",
            )
            result = verify_snapshot(args.directory, application=True)
            recovery.private_dir(args.receipt.parent)
            _durable_write(args.receipt, result)
    except Exception as error:
        reason = str(error) if isinstance(error, scan.PreflightError) else "sanitized_backup_failed"
        print(json.dumps({"result": "blocked", "reason": reason}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
