"""Release guardrails: exact artifact, real receipt gates and data-preserving rollback."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta

import pytest

from scripts import release


@pytest.fixture
def backup_receipt():
    return {
        "result": "data_restore_verified",
        "created_at": datetime.now(UTC).isoformat(),
        "archive_sha256": "a" * 64,
        "offhost_verified": True,
        "protected_recovery_verified": True,
        "database": {"revisions": ["0011"]},
        "application": {"catalog": 200, "apartment": 200, "photo": 200, "photo_decoded": True},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"result": "backup_created"},
        {"offhost_verified": False},
        {"protected_recovery_verified": False},
        {"archive_sha256": "invalid"},
        {"created_at": (datetime.now(UTC) - timedelta(days=2)).isoformat()},
        {"created_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        {"database": {}},
        {"application": {"catalog": 200}},
    ],
)
def test_release_rejects_unverified_stale_or_incomplete_backup(backup_receipt, mutation):
    release.validate_backup(backup_receipt)
    backup_receipt.update(mutation)
    with pytest.raises(release.ReleaseError):
        release.validate_backup(backup_receipt)


def test_test_gate_requires_exact_revision_and_all_required_checks():
    receipt = {"revision": "a" * 40, "result": "passed", "checks": {"unit": "passed"}}
    release.validate_tests(receipt, "a" * 40, ["unit"])
    with pytest.raises(release.ReleaseError):
        release.validate_tests(receipt, "b" * 40, ["unit"])
    with pytest.raises(release.ReleaseError):
        release.validate_tests(receipt, "a" * 40, ["unit", "browser"])


@pytest.fixture
def packet(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for name, content in {
        "pyproject.toml": "[project]\nname='fixture'\n",
        "uv.lock": "version = 1\n",
        "scripts/release.py": "# fixture release script",
        "src/app.py": "version=1",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repo / "scripts/release.py").chmod(0o755)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    receipt = tmp_path / "tests.json"
    receipt.write_text(
        json.dumps(
            {
                "revision": revision,
                "result": "passed",
                "checks": dict.fromkeys(release.CHECKS, "passed"),
            }
        )
    )
    monkeypatch.setattr(release, "ROOT", repo)
    target = tmp_path / "packet.tar.gz"
    # Uncommitted changes must not leak into the packet or be passed off as tested code.
    (repo / "src/app.py").write_text("version=2\n")
    (repo / ".env").write_text("fixture secret that must never be archived")
    result = release.prepare(
        argparse.Namespace(
            revision=revision, required_check=None, test_receipt=receipt, output=target
        )
    )
    assert result["revision"] == revision
    return target


def test_packet_is_exact_tracked_revision_without_local_secrets(packet, tmp_path):
    destination = tmp_path / "extract"
    manifest = release.unpack_packet(packet, destination)
    assert (destination / "code/src/app.py").read_text() == "version=1"
    assert not (destination / "code/.env").exists()
    assert (destination / "code/scripts/release.py").stat().st_mode & 0o777 == 0o755
    assert (destination / "code/src").stat().st_mode & 0o777 == 0o755
    assert manifest["tests"]["revision"] == manifest["revision"]


def test_tampered_packet_cannot_be_staged(packet, tmp_path):
    with tarfile.open(packet) as tar:
        files = {m.name: tar.extractfile(m).read() for m in tar.getmembers()}
    files["code/src/app.py"] = b"tampered"
    with tarfile.open(packet, "w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    with pytest.raises(release.ReleaseError, match="hash mismatch"):
        release.unpack_packet(packet, tmp_path / "extract")


def test_code_rollback_preserves_new_database_rows_and_photos(tmp_path, monkeypatch):
    root = tmp_path / "app"
    source = tmp_path / "release"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text("old")
    (root / "data").mkdir()
    (root / "data/otz.db").write_bytes(b"new leads after backup must survive")
    (root / "data/photo.jpg").write_bytes(b"new photo must survive")
    (source / "code/src").mkdir(parents=True)
    (source / "code/src/app.py").write_text("new")
    release.swap_path(source / "code/src", root / "src", source / "rollback/src")
    assert (root / "src/app.py").read_text() == "new"
    release.write_json(
        source / "activation.json", {"swapped": ["src"], "units": [], "services": [], "timers": []}
    )
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: None)
    result = release.restore_code(root, source)
    assert result["result"] == "rolled_back_code_only"
    assert (root / "src/app.py").read_text() == "old"
    assert (root / "data/otz.db").read_bytes() == b"new leads after backup must survive"
    assert (root / "data/photo.jpg").read_bytes() == b"new photo must survive"


def test_failed_swap_restores_original_immediately(tmp_path):
    target = tmp_path / "current"
    target.write_text("old")
    with pytest.raises(FileNotFoundError):
        release.swap_path(tmp_path / "missing", target, tmp_path / "previous")
    assert target.read_text() == "old"


def test_running_oneshot_blocks_release_even_when_not_active(monkeypatch):
    monkeypatch.setattr(release, "run", lambda *args, **kwargs: "activating\n")
    with pytest.raises(release.ReleaseError, match="running or starting"):
        release.maintenance_idle()


def test_wrapper_has_no_implicit_production_target():
    result = subprocess.run(
        ["bash", str(release.ROOT / "scripts/deploy_sync.sh")],
        env={**os.environ, "PYTHON_BIN": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "--target" in result.stderr and "required" in result.stderr
    assert "90.156.254.159" not in result.stdout + result.stderr


def test_canonical_timers_preserve_live_schedules():
    for name, calendar, random in [
        ("otz-nightly-refresh", "*-*-* 03:00:00 UTC", "15min"),
        ("otz-weekly-full-refresh", "Sun *-*-* 04:00:00 UTC", "15min"),
        ("otz-scrape-smoke", "*-*-* 00/6:30:00 UTC", "5min"),
    ]:
        text = (release.ROOT / "deploy" / f"{name}.timer").read_text()
        assert f"OnCalendar={calendar}" in text
        assert f"RandomizedDelaySec={random}" in text and "Persistent=true" in text
