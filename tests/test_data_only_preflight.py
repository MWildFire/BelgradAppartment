"""No real credentials or network: synthetic DB/media and scope boundary tests."""

import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from scripts import data_only_preflight as preflight
from scripts import recovery
from tests.test_recovery import source as recovery_source


def test_empty_text_and_blob_are_scanned_without_skipping_nonempty_credentials(tmp_path):
    path = tmp_path / "empty-values.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE entries (content TEXT, binary_value BLOB, api_key TEXT)")
        db.execute("INSERT INTO entries VALUES ('', ?, '')", (b"",))
    result = preflight.inspect_database(path)
    assert result["result"] == "heuristic_scan_passed"
    assert result["cells_scanned"] == 3
    assert result["bytes_scanned"] == 0
    with sqlite3.connect(path) as db:
        db.execute("UPDATE entries SET api_key='synthetic opaque value'")
    with pytest.raises(preflight.PreflightError, match="populated_credential_field"):
        preflight.inspect_database(path)


@pytest.fixture
def source(tmp_path):
    return recovery_source.__wrapped__(tmp_path)


def test_wal_snapshot_scope_excludes_state_credentials_and_unreferenced_media(source, monkeypatch):
    (source.project_root / ".env").write_text("FAKE_SECRET=not read")
    (source.state_dir / "telegram.session").write_text("synthetic not read")
    (source.inbound_dir / "unreferenced.jpg").write_text("not an image and never read")
    album = source.listings_dir / "NEKRETNINE/fixture"
    album.mkdir(parents=True)
    Image.new("RGB", (8, 8), "blue").save(album / "000.jpg")
    Image.new("RGB", (8, 8), "blue").save(source.inbound_dir / "photo.jpg.card.webp")
    (source.inbound_dir.parent / "photo_manifest.json").write_text("untrusted index never read")
    writer = sqlite3.connect(source.db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE listings SET price_eur=777")
    writer.commit()
    before = recovery.digest(source.db)
    original_read = Path.read_bytes

    def read_allowed(path):
        assert path.name not in {".env", "telegram.session", "unreferenced.jpg", "photo_manifest.json"}
        assert not path.is_relative_to(source.state_dir)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_allowed)
    result = preflight.prepare(source)
    manifest = json.loads((source.output_dir / "manifest.json").read_text())
    assert result["result"] == "local_preflight_only_not_restore_verified"
    assert set(manifest["media"]) == {
        "data/inbound/photo.jpg", "data/inbound/photo.jpg.card.webp",
        "data/listings/NEKRETNINE/fixture/000.jpg",
    }
    assert manifest["format"] == preflight.FORMAT
    assert manifest["personal_data_present"] and not manifest["authorization_to_transfer"]
    assert not manifest["protected_recovery_verified"] and not manifest["offhost_verified"]
    assert not manifest["credential_inspection"]["arbitrary_secrets_absence_proven"]
    assert {p.relative_to(source.output_dir).as_posix()
            for p in source.output_dir.rglob("*") if p.is_file()} == {"data/otz.db", "manifest.json"}
    assert recovery.digest(source.db) == before
    with sqlite3.connect(source.output_dir / "data/otz.db") as snapshot:
        assert snapshot.execute("SELECT price_eur FROM listings").fetchone()[0] == 777
        assert snapshot.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert source.output_dir.stat().st_mode & 0o777 == 0o700
    assert (source.output_dir / "data/otz.db").stat().st_mode & 0o777 == 0o600
    with pytest.raises(recovery.RecoveryError):
        recovery.verify_directory(source.output_dir)
    writer.close()


@pytest.mark.parametrize("column", ["token", "api_key", "password", "auth_key", "lease_token"])
def test_populated_credential_field_blocks_before_accepted_packet(source, column):
    with sqlite3.connect(source.db) as db:
        db.execute(f'CREATE TABLE extra ("{column}" TEXT)')
        db.execute("INSERT INTO extra VALUES (?)", ("synthetic opaque value",))
    with pytest.raises(preflight.PreflightError, match=r"^populated_credential_field$"):
        preflight.prepare(source)
    assert not source.output_dir.exists()
    assert not list(source.output_dir.parent.glob(".db-media-preflight-*"))


@pytest.mark.parametrize("payload", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic",
    "sk-ant-" + "x" * 30,
    "123456789:" + "x" * 35,
    "https://fixture:synthetic@proxy.invalid",
    '"api_key": "synthetic_value"',
    "Bearer synthetic_value",
])
def test_credentials_in_free_text_are_blocked_without_echo(source, payload):
    with sqlite3.connect(source.db) as db:
        db.execute("UPDATE inbound_posts SET raw_text=?", (payload,))
    with pytest.raises(preflight.PreflightError) as error:
        preflight.prepare(source)
    assert str(error.value) == "credential_pattern_in_database"
    assert payload not in str(error.value) and not source.output_dir.exists()


def test_blob_and_scan_limit_fail_closed(source, monkeypatch):
    with sqlite3.connect(source.db) as db:
        db.execute("UPDATE listings SET photo_embeddings=?", (b"password=synthetic_value",))
    with pytest.raises(preflight.PreflightError, match="credential_pattern_in_database"):
        preflight.prepare(source)
    with sqlite3.connect(source.db) as db:
        db.execute("UPDATE listings SET photo_embeddings=NULL")
    monkeypatch.setattr(preflight, "MAX_CELL_BYTES", 3)
    with pytest.raises(preflight.PreflightError, match="cell_scan_limit"):
        preflight.prepare(source)


@pytest.mark.parametrize("path", [
    ".env", "Caddyfile", "telegram.session", "telegram.session.jpg", "cookies.json",
    "storage_state.json", "id_ed25519", "secret.jpg", "state/photo.jpg", "logs/photo.jpg",
    "../photo.jpg", "/photo.jpg", "photo.txt", ".hidden/photo.jpg",
])
def test_image_allowlist_excludes_protected_and_non_image_paths(path):
    assert not preflight.allowed_image(Path(path))


@pytest.mark.parametrize("kind", ["symlink", "disguised_key", "appended_key", "missing"])
def test_invalid_referenced_media_blocks_packet(source, kind):
    photo = source.inbound_dir / "photo.jpg"
    if kind == "symlink":
        photo.unlink()
        photo.symlink_to(source.listings_dir / "photo.jpg")
    elif kind == "disguised_key":
        photo.write_text("synthetic plain text disguised as JPEG")
    elif kind == "appended_key":
        with photo.open("ab") as stream:
            stream.write(b"-----BEGIN OPENSSH PRIVATE KEY----- synthetic")
    else:
        photo.unlink()
    with pytest.raises(preflight.PreflightError):
        preflight.prepare(source)
    assert not source.output_dir.exists()


def test_reference_to_state_is_rejected_without_reading_state(source):
    with sqlite3.connect(source.db) as db:
        db.execute("UPDATE inbound_posts SET photo_paths=?",
                   (json.dumps(["data/state/last_scrape.txt"]),))
    with pytest.raises(preflight.PreflightError, match="unsupported_photo_root"):
        preflight.prepare(source)


def test_existing_output_is_never_overwritten(source):
    source.output_dir.mkdir(mode=0o700)
    sentinel = source.output_dir / "existing"
    sentinel.write_text("preserve")
    with pytest.raises(preflight.PreflightError, match="invalid_output_path"):
        preflight.prepare(source)
    assert sentinel.read_text() == "preserve"


def test_pixel_limit_and_truncated_jpeg_fail_closed(source, monkeypatch):
    photo = source.inbound_dir / "photo.jpg"
    monkeypatch.setattr(preflight, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(preflight.PreflightError, match="image_scan_limit"):
        preflight.inspect_image(photo)
    monkeypatch.setattr(preflight, "MAX_IMAGE_PIXELS", 40_000_000)
    photo.write_bytes(photo.read_bytes()[:-20])
    with pytest.raises(preflight.PreflightError, match="invalid_image"):
        preflight.inspect_image(photo)


def test_cli_sanitizes_unexpected_error_and_returns_failure(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["preflight", "--project-root", "/synthetic",
                                   "--output-dir", "/unused"])

    def fail(args):
        raise RuntimeError("sensitive path and credential value")

    monkeypatch.setattr(preflight, "prepare", fail)
    assert preflight.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"result": "blocked", "reason": "preflight_failed"}
    assert captured.err == ""
