"""F07: real WAL snapshot, fail-closed archives and offline application/photo restore."""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts import recovery
from src.db.models import (
    Base,
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)


@pytest.fixture
def source(tmp_path):
    project = tmp_path / "source"
    data = project / "data"
    for name in ("state", "inbound", "listings"):
        (data / name).mkdir(parents=True)
    (data / "state" / "last_scrape.txt").write_text("fixture checkpoint")
    for name in ("inbound", "listings"):
        Image.new("RGB", (30, 20), "blue").save(data / name / "photo.jpg")
    db = data / "otz.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(SourceChannel(id=1, tg_id=1))
        session.add(Listing(id=1, platform=Platform.NEKRETNINE, external_id="fixture",
                            url="https://example.invalid/1", price_eur=600, rooms=2,
                            area_m2=42, district="Vracar", lat=44.8, lon=20.47))
        session.flush()
        session.add(InboundPost(id=1, source_channel_id=1, tg_message_id=1,
                                raw_text="fixture", photo_paths=["data/inbound/photo.jpg"]))
        session.flush()
        session.add(Match(id=1, inbound_post_id=1, listing_id=1, status=MatchStatus.PUBLISHED))
        session.flush()
        session.add(SiteListing(id=1, match_id=1, is_public=True))
        session.commit()
    engine.dispose()
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        connection.executemany("INSERT INTO alembic_version VALUES (?)",
                               [(head,) for head in recovery.schema_heads()])
    return argparse.Namespace(db=db, state_dir=data / "state", inbound_dir=data / "inbound",
                              listings_dir=data / "listings", published_dir=data / "inbound",
                              project_root=project, output_dir=tmp_path / "private-backups",
                              expected_schema=None, max_age_hours=36, stream=False)


def backup(source):
    result = recovery.create_backup(source)
    return Path(result["archive"])


def extract(archive, tmp_path):
    destination = tmp_path / "restored"
    destination.mkdir(mode=0o700)
    return recovery.extract_verify(archive, destination, 36)


def rewrite(archive, mutation):
    with tarfile.open(archive) as tar:
        files = {member.name: tar.extractfile(member).read() for member in tar.getmembers()}
    mutation(files)
    with tarfile.open(archive, "w:gz") as tar:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))


def change_manifest(files, mutation):
    manifest = json.loads(files["manifest.json"])
    mutation(manifest)
    files["manifest.json"] = json.dumps(manifest).encode()


def test_online_backup_reads_uncheckpointed_wal_and_restores_real_app(source, tmp_path):
    writer = sqlite3.connect(source.db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE listings SET price_eur=777 WHERE id=1")
    writer.commit()
    assert Path(str(source.db) + "-wal").stat().st_size > 0
    archive = backup(source)
    original_hash = recovery.digest(source.db)
    manifest = extract(archive, tmp_path)
    with sqlite3.connect(tmp_path / "restored/data/otz.db") as restored:
        assert restored.execute("SELECT price_eur FROM listings").fetchone()[0] == 777
    result = recovery.verify_backup(argparse.Namespace(archive=archive, max_age_hours=36,
                                                       expected_schema=None))
    assert result["result"] == "data_restore_verified"
    assert result["application"] == {"catalog": 200, "apartment": 200, "photo": 200,
                                      "photo_decoded": True, "network": "disabled"}
    assert manifest["photo_references"] == 1
    assert source.db.exists() and recovery.digest(source.db) == original_hash
    assert archive.stat().st_mode & 0o777 == 0o600
    assert source.output_dir.stat().st_mode & 0o777 == 0o700
    writer.close()


def test_stream_roundtrip_uses_only_small_sqlite_staging(source, tmp_path):
    archive = tmp_path / "stream.tar.gz"
    args = [sys.executable, str(recovery.ROOT / "scripts/recovery.py"), "backup", "--stream"]
    for key in ("db", "state_dir", "inbound_dir", "listings_dir", "published_dir", "project_root"):
        args += ["--" + key.replace("_", "-"), str(getattr(source, key))]
    with archive.open("wb") as destination:
        process = subprocess.run(args, stdout=destination, stderr=subprocess.PIPE, check=False)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stderr)["result"] == "stream_created_verify_destination"
    assert not source.output_dir.exists()
    assert extract(archive, tmp_path)["photo_references"] == 1


@pytest.mark.parametrize("failure", ["missing_manifest", "stale", "future", "file_removed",
                                      "hash_changed", "table_counts", "schema", "media_role"])
def test_manifest_failures_are_rejected(source, tmp_path, failure):
    archive = backup(source)

    def mutate(files):
        if failure == "missing_manifest":
            del files["manifest.json"]
        elif failure in {"stale", "future"}:
            stamp = datetime.now(UTC) + timedelta(days=-14 if failure == "stale" else 1)
            change_manifest(files, lambda m: m.update(created_at=stamp.isoformat()))
        elif failure == "file_removed":
            del files["data/inbound/photo.jpg"]
        elif failure == "hash_changed":
            files["data/inbound/photo.jpg"] += b"damage"
        elif failure == "table_counts":
            change_manifest(files, lambda m: m["database"]["table_counts"].update(listings=2))
        elif failure == "schema":
            change_manifest(files, lambda m: m["database"].update(schema_sha256="wrong"))
        elif failure == "media_role":
            change_manifest(files, lambda m: m["components"].pop("published"))
    rewrite(archive, mutate)
    with pytest.raises(recovery.RecoveryError):
        extract(archive, tmp_path)


def test_empty_legacy_single_table_archive_fails(source, tmp_path):
    db = tmp_path / "empty.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE listings (id INTEGER)")
    archive = tmp_path / "legacy.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(db, arcname="otz-20260101.db")
    with pytest.raises(recovery.RecoveryError, match="manifest is missing"):
        extract(archive, tmp_path)
    with pytest.raises(recovery.RecoveryError, match="missing key tables"):
        recovery.database_manifest(db)


@pytest.mark.parametrize("problem", ["foreign_key", "empty_table", "old_revision", "missing_photo",
                                     "missing_source_media", "secret", "symlink", "public_output"])
def test_invalid_source_never_publishes_backup(source, tmp_path, problem):
    if problem in {"foreign_key", "empty_table", "old_revision"}:
        with sqlite3.connect(source.db) as connection:
            if problem == "foreign_key":
                connection.execute("UPDATE matches SET listing_id=999")
            elif problem == "empty_table":
                connection.execute("DELETE FROM site_listings")
            else:
                connection.execute("UPDATE alembic_version SET version_num='obsolete'")
    elif problem == "missing_photo":
        (source.inbound_dir / "photo.jpg").unlink()
    elif problem == "missing_source_media":
        (source.listings_dir / "photo.jpg").unlink()
    elif problem == "secret":
        (source.state_dir / "user.session").write_bytes(b"not credentials, must still reject")
    elif problem == "symlink":
        (source.inbound_dir / "link.jpg").symlink_to(source.inbound_dir / "photo.jpg")
    elif problem == "public_output":
        source.output_dir.mkdir(mode=0o755)
        os.chmod(source.output_dir, 0o755)
    with pytest.raises(recovery.RecoveryError):
        backup(source)
    assert not list(source.output_dir.glob("otz-*.tar.gz"))


def test_broken_photo_decoding_fails_application_drill(source):
    (source.inbound_dir / "photo.jpg").write_bytes(b"broken photo")
    archive = backup(source)
    with pytest.raises(recovery.RecoveryError, match="application/photo smoke failed"):
        recovery.verify_backup(argparse.Namespace(archive=archive, max_age_hours=36,
                                                  expected_schema=None))


def test_incomplete_derivative_manifest_rejected(source):
    (source.inbound_dir.parent / "photo_manifest.json").write_text(json.dumps({
        "photo.jpg": {"w": 30, "h": 20, "card": "missing.card.webp", "detail": ""}}))
    with pytest.raises(recovery.RecoveryError, match="derivative is missing"):
        backup(source)


def test_archive_traversal_rejected_before_any_extraction(source, tmp_path):
    archive = backup(source)
    rewrite(archive, lambda files: files.update({"../outside": b"bad"}))
    with pytest.raises(recovery.RecoveryError, match="unsafe archive member"):
        extract(archive, tmp_path)
    assert not (tmp_path / "outside").exists()


def test_restore_requires_new_schema_tables_as_models_evolve(source, tmp_path, monkeypatch):
    revisions = recovery.schema_heads()
    checkout = tmp_path / "new-checkout"
    models = checkout / "src/db/models.py"
    models.parent.mkdir(parents=True)
    models.write_text('class NewOutbox:\n    __tablename__ = "new_delivery_outbox"\n')
    monkeypatch.setattr(recovery, "ROOT", checkout)
    with pytest.raises(recovery.RecoveryError, match="new_delivery_outbox"):
        recovery.database_manifest(source.db, revisions)
