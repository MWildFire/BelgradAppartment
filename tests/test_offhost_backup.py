"""Real local rsync generations and restore; SSH/processes are explicit fakes."""

from __future__ import annotations

import argparse
import fcntl
import json
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import offhost_backup as job
from scripts import recovery
from tests.test_recovery import source as recovery_source


@pytest.fixture
def source(tmp_path):
    return recovery_source.__wrapped__(tmp_path)


def seed(source, tmp_path):
    archive = Path(recovery.create_backup(source)["archive"])
    root = tmp_path / "seed"
    receipt = recovery.verify_backup(
        argparse.Namespace(
            archive=archive, restore_dir=root, max_age_hours=36, expected_schema=None
        )
    )
    return root, receipt


def test_retained_extraction_stays_immutable_after_real_application_restore(source, tmp_path):
    root, receipt = seed(source, tmp_path)
    before = recovery.digest(root / "data/otz.db")
    manifest = recovery.verify_directory(root)
    again = recovery.verify_application(root, manifest)
    assert before == recovery.digest(root / "data/otz.db")
    assert receipt["application"] == again["application"]
    assert receipt["application"]["photo_decoded"]


def test_small_staged_snapshot_has_full_manifest_but_only_db_state_bytes(source, tmp_path):
    source.output_dir = tmp_path / "snapshot"
    recovery.create_snapshot(source)
    manifest = json.loads((source.output_dir / "manifest.json").read_text())
    assert "data/inbound/photo.jpg" in manifest["files"]
    assert not (source.output_dir / "data/inbound").exists()
    assert (source.output_dir / "data/state/last_scrape.txt").exists()
    assert (source.output_dir / "data/otz.db").exists()
    with pytest.raises(recovery.RecoveryError, match="inventory"):
        recovery.verify_directory(source.output_dir)


def environment(source, tmp_path, monkeypatch):
    seed_dir, _receipt = seed(source, tmp_path)
    key = tmp_path / "fake-key"
    key.write_text("fixture only; SSH never invoked")
    args = argparse.Namespace(
        private_root=tmp_path / "offhost",
        seed_dir=seed_dir,
        remote_root=source.project_root,
        remote_published_dir=source.published_dir,
        remote_python=sys.executable,
        remote_user="otz",
        ssh_target="fixture",
        ssh_key=key,
        interval_hours=0.00000001,
        min_free_gib=0.000001,
    )

    @contextmanager
    def lease(_args):
        yield

    def remote(_args, command):
        if "snapshot" in command:
            destination = Path(command[command.index("--output-dir") + 1])
            source.output_dir = destination
            recovery.create_snapshot(source)
        else:
            shutil.rmtree(command[-1], ignore_errors=True)

    def rsync(command, **kwargs):
        if command[0] == sys.executable:
            return subprocess.run(command, capture_output=True, text=True, check=True, **kwargs)
        assert command[0] == "/usr/bin/rsync", "a test attempted a non-rsync subprocess"
        assert "--inplace" not in command and not any(arg.startswith("--delete") for arg in command)
        local = []
        skip = False
        for arg in command:
            if skip:
                skip = False
                continue
            if arg in ("-e", "--rsync-path"):
                skip = True
                continue
            local.append(arg.removeprefix("fixture:"))
        return subprocess.run(local, capture_output=True, text=True, check=True)

    monkeypatch.setattr(job, "remote_lease", lease)
    monkeypatch.setattr(job, "remote", remote)
    monkeypatch.setattr(job, "run", rsync)
    return args


@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_real_rsync_generations_share_unchanged_media_keep_two_and_restore(
    source, tmp_path, monkeypatch
):
    args = environment(source, tmp_path, monkeypatch)
    for price in (700, 800, 900):
        with sqlite3.connect(source.db) as db:
            db.execute("UPDATE listings SET price_eur=?", (price,))
        receipt = job.backup(args)
        assert receipt["backup_format"] == "directory_generation"
        assert receipt["offhost_verified"] and not receipt["protected_recovery_verified"]
        assert "archive_sha256" not in receipt
    accepted = job.completed(args.private_root)
    assert len(accepted) == 2
    one, two = (entry[1] for entry in accepted)
    assert (one / "data/inbound/photo.jpg").stat().st_ino == (
        two / "data/inbound/photo.jpg"
    ).stat().st_ino
    assert (one / "data/inbound/photo.jpg").stat().st_ino == (
        args.seed_dir / "data/inbound/photo.jpg"
    ).stat().st_ino
    for generation, price in ((one, 800), (two, 900)):
        with sqlite3.connect(generation / "data/otz.db") as db:
            assert db.execute("SELECT price_eur FROM listings").fetchone()[0] == price
        recovery.verify_directory(generation)
    args.interval_hours = 24
    assert job.backup(args)["result"] == "skipped_not_due"


@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_failed_new_generation_never_prunes_or_promotes_previous(source, tmp_path, monkeypatch):
    args = environment(source, tmp_path, monkeypatch)
    job.backup(args)
    accepted = job.completed(args.private_root)
    before = recovery.digest(accepted[0][1] / "data/inbound/photo.jpg")
    transfer = job.transfer

    def corrupted(*arguments):
        manifest = transfer(*arguments)
        photo = arguments[2] / "data/inbound/photo.jpg"
        photo.unlink()  # never write in place through the deliberately shared old inode
        photo.write_bytes(b"broken fixture")
        return manifest

    monkeypatch.setattr(job, "transfer", corrupted)
    with pytest.raises(recovery.RecoveryError, match="checksum"):
        job.backup(args)
    assert len(job.completed(args.private_root)) == 1
    assert recovery.digest(accepted[0][1] / "data/inbound/photo.jpg") == before
    assert len(list((args.private_root / "pending").iterdir())) == 1


def test_lock_low_disk_and_missing_seed_fail_before_any_ssh(source, tmp_path, monkeypatch):
    args = environment(source, tmp_path, monkeypatch)
    monkeypatch.setattr(job, "remote", lambda *_args: pytest.fail("unexpected remote call"))
    recovery.private_dir(args.private_root)
    with (args.private_root / "backup.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert job.backup(args)["result"] == "skipped_locked"
    args.min_free_gib = 10**9
    with pytest.raises(recovery.RecoveryError, match="free space"):
        job.backup(args)
    args.seed_dir = None
    with pytest.raises(recovery.RecoveryError, match="seed-dir"):
        job.backup(args)


def test_directory_verifier_rejects_stale_extra_files_and_symlinks(source, tmp_path):
    root, _receipt = seed(source, tmp_path)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(recovery.RecoveryError, match="stale"):
        recovery.verify_directory(root)
    manifest["created_at"] = datetime.now(UTC).isoformat()
    manifest_path.write_text(json.dumps(manifest))
    (root / "extra").write_text("not inventoried")
    with pytest.raises(recovery.RecoveryError, match="inventory"):
        recovery.verify_directory(root)
    (root / "extra").unlink()
    (root / "link").symlink_to(root / "data/otz.db")
    with pytest.raises(recovery.RecoveryError, match="symlink"):
        recovery.verify_directory(root)


def test_launchagent_prints_explicit_daily_due_job_without_installing(tmp_path):
    command = [
        sys.executable,
        "scripts/offhost_backup.py",
        "launch-agent",
        "--ssh-target",
        "fixture",
        "--ssh-key",
        str(tmp_path / "key"),
        "--private-root",
        str(tmp_path / "private"),
        "--remote-root",
        "/opt/otz",
        "--remote-python",
        "/opt/otz/.venv/bin/python",
        "--remote-published-dir",
        "/opt/otz/data/inbound",
    ]
    result = subprocess.run(command, capture_output=True, check=True)
    payload = plistlib.loads(result.stdout)
    assert payload["StartInterval"] == 21600 and payload["RunAtLoad"]
    assert payload["ProgramArguments"][2] == "run"
    assert "fixture" in payload["ProgramArguments"]
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_changed_media_transfers_despite_same_size_mtime_and_seed_timestamp_drift(
    source, tmp_path, monkeypatch
):
    import os

    from PIL import Image

    args = environment(source, tmp_path, monkeypatch)
    old = source.listings_dir / "photo.jpg"
    old_time = old.stat().st_mtime
    # Full-archive extraction dates differ from remote originals in production.
    os.utime(args.seed_dir / "data/inbound/photo.jpg", (1, 1))
    Image.new("RGB", (30, 20), "red").save(old)
    os.utime(old, (old_time, old_time))
    receipt = job.backup(args)
    generation = args.private_root / "generations" / receipt["generation_id"]
    assert recovery.digest(generation / "data/listings/photo.jpg") == recovery.digest(old)
    assert (generation / "data/listings/photo.jpg").stat().st_ino != (
        args.seed_dir / "data/listings/photo.jpg"
    ).stat().st_ino
    assert (generation / "data/inbound/photo.jpg").stat().st_ino == (
        args.seed_dir / "data/inbound/photo.jpg"
    ).stat().st_ino


def test_full_archive_and_incremental_runner_share_source_lock(source):
    folder = source.db.parent / "backups"
    folder.mkdir()
    with (folder / "offhost.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(recovery.RecoveryError, match="source lock"):
            recovery.create_backup(source)
    assert not source.output_dir.exists()


@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_unverified_media_seed_is_cache_only_and_new_generation_gets_fresh_db(
    source, tmp_path, monkeypatch
):
    args = environment(source, tmp_path, monkeypatch)
    seed_manifest = args.seed_dir / "manifest.json"
    value = json.loads(seed_manifest.read_text())
    value["format"] = "unverified_media_seed"
    value["result"] = "seed_only_unverified"
    seed_manifest.write_text(json.dumps(value))
    (args.seed_dir / "data/otz.db").unlink()
    args.remote_recovery_script = Path("/opt/otz/tools/recovery/recovery.py")
    result = job.backup(args)
    assert result["result"] == "data_restore_verified"
    assert (args.private_root / "generations" / result["generation_id"] / "data/otz.db").is_file()
    with pytest.raises(recovery.RecoveryError, match="format"):
        recovery.verify_directory(args.seed_dir)


def test_child_smoke_receives_explicit_compatible_application_root(monkeypatch, tmp_path):
    from types import SimpleNamespace

    application_root = tmp_path / "old-app"
    monkeypatch.setattr(recovery, "ROOT", application_root)
    (tmp_path / "manifest.json").write_text("{}")
    seen = []

    def child(command, **_kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=0, stderr="", stdout='{"photo_decoded":true}')

    monkeypatch.setattr(recovery.subprocess, "run", child)
    recovery.verify_application(
        tmp_path,
        {
            "created_at": "date",
            "database": {},
            "files": {},
            "photo_references": 1,
            "protected_recovery": {},
        },
    )
    assert seen[0][-2:] == ["--application-project-root", str(application_root)]


def test_remote_lease_owner_exit_aborts_inflight_work(monkeypatch):
    import time

    args = argparse.Namespace(
        remote_root=Path("/opt/otz"), remote_python=sys.executable, remote_user=None
    )
    monkeypatch.setattr(
        job,
        "ssh",
        lambda _args: [
            sys.executable,
            "-c",
            "import time; print('ready',flush=True); time.sleep(.2)",
        ],
    )
    started = time.monotonic()
    with pytest.raises(recovery.RecoveryError, match="lease was lost"), job.remote_lease(args):
        job.run([sys.executable, "-c", "import time; time.sleep(15)"])
    assert time.monotonic() - started < 3


@pytest.mark.parametrize(
    "path",
    [
        "/opt/otz/data/space name",
        "/opt/otz/data/x;echo AUDIT",
        "/opt/otz/data/../../outside",
        "/opt/otz/data/$(anything)",
        "/other/data/inbound",
    ],
)
def test_protocol29_remote_sources_reject_shell_and_traversal_paths(path):
    with pytest.raises(recovery.RecoveryError, match="unsafe remote"):
        job.safe_remote_path(path, Path("/opt/otz/data"))


def test_completed_ignores_future_and_mismatched_receipt_dates_without_deleting(tmp_path):
    root = tmp_path
    (root / "generations").mkdir()
    (root / "receipts").mkdir()
    now = datetime.now(UTC)
    for name, recorded, claimed in (
        ("valid", now - timedelta(hours=1), now - timedelta(hours=1)),
        ("future1", now + timedelta(days=1), now + timedelta(days=1)),
        ("future2", now + timedelta(days=2), now + timedelta(days=2)),
        ("mismatch", now - timedelta(days=2), now - timedelta(hours=1)),
    ):
        generation = root / "generations" / name
        generation.mkdir()
        manifest = generation / "manifest.json"
        manifest.write_text(json.dumps({"created_at": recorded.isoformat()}))
        (root / "receipts" / f"{name}.json").write_text(
            json.dumps(
                {
                    "result": "data_restore_verified",
                    "created_at": claimed.isoformat(),
                    "generation_manifest_sha256": recovery.digest(manifest),
                }
            )
        )
    assert [entry[1].name for entry in job.completed(root)] == ["valid"]
    assert len(list((root / "generations").iterdir())) == 4


@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_future_old_receipts_cannot_prune_current_accepted_generation(
    source, tmp_path, monkeypatch
):
    args = environment(source, tmp_path, monkeypatch)
    job.backup(args)
    for number in (1, 2):
        generation = args.private_root / "generations" / f"invalid-future-{number}"
        generation.mkdir()
        stamp = (datetime.now(UTC) + timedelta(days=number)).isoformat()
        manifest = generation / "manifest.json"
        manifest.write_text(json.dumps({"created_at": stamp}))
        job.atomic_json(
            args.private_root / "receipts" / f"{generation.name}.json",
            {
                "result": "data_restore_verified",
                "created_at": stamp,
                "generation_manifest_sha256": recovery.digest(manifest),
            },
        )
    receipt = job.backup(args)
    destination = args.private_root / "generations" / receipt["generation_id"]
    assert destination.is_dir()
    assert len(job.completed(args.private_root)) == 2
    assert len(list((args.private_root / "generations").iterdir())) == 4


@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_lease_loss_during_validation_never_promotes_or_prunes(source, tmp_path, monkeypatch):
    args = environment(source, tmp_path, monkeypatch)
    job.backup(args)
    prior = job.completed(args.private_root)[0][1]
    actual = recovery.verify_directory

    def lose_lease(*arguments, **kwargs):
        def lost():
            raise recovery.RecoveryError("source backup lease was lost")

        return actual(*arguments, **{**kwargs, "progress_check": lost})

    monkeypatch.setattr(recovery, "verify_directory", lose_lease)
    with pytest.raises(recovery.RecoveryError, match="lease was lost"):
        job.backup(args)
    assert prior.exists() and len(job.completed(args.private_root)) == 1


@pytest.mark.parametrize("failed_step", ["promotion", "receipt"])
@pytest.mark.skipif(not Path("/usr/bin/rsync").exists(), reason="system rsync not installed")
def test_cleanup_failure_cannot_publish_receipt_after_failed_acceptance(
    source, tmp_path, monkeypatch, failed_step
):
    args = environment(source, tmp_path, monkeypatch)
    remote = job.remote

    def cleanup_fails(parameters, command):
        if "-c" in command:
            raise OSError("fixture cleanup failure")
        return remote(parameters, command)

    monkeypatch.setattr(job, "remote", cleanup_fails)
    attempts = []
    if failed_step == "promotion":
        rename = Path.rename

        def failed_rename(path, target):
            if path.parent.name == "pending":
                raise OSError("fixture promotion failure")
            return rename(path, target)

        monkeypatch.setattr(Path, "rename", failed_rename)
    else:

        def failed_receipt(*_arguments):
            attempts.append(True)
            raise OSError("fixture receipt failure")

        monkeypatch.setattr(job, "atomic_json", failed_receipt)
    with pytest.raises(OSError, match=f"fixture {failed_step} failure"):
        job.backup(args)
    assert not list((args.private_root / "receipts").iterdir())
    assert not job.completed(args.private_root)
    if failed_step == "receipt":
        assert len(attempts) == 1
