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
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(release, "active", lambda unit: False)
    monkeypatch.setattr(release, "drain_maintenance", lambda: None, raising=False)
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


def test_canonical_timers_separate_refresh_and_smoke_windows():
    for name, calendar, random in [
        ("otz-nightly-refresh", "Mon..Sat *-*-* 03:00:00 UTC", "15min"),
        ("otz-weekly-full-refresh", "Sun *-*-* 02:00:00 UTC", "15min"),
        ("otz-scrape-smoke", "*-*-* 00/12:30:00 UTC", "5min"),
        ("otz-public-detail-refresh", "*-*-* 11,17,23:30:00 UTC", "5min"),
    ]:
        text = (release.ROOT / "deploy" / f"{name}.timer").read_text()
        assert f"OnCalendar={calendar}" in text
        assert f"RandomizedDelaySec={random}" in text and "Persistent=true" in text


@pytest.mark.parametrize("failure", [None, "browser", "health", "hash"])
def test_activation_cannot_start_timer_on_unverified_or_rejected_code(
    tmp_path, monkeypatch, backup_receipt, failure
):
    """Exercise actual activation/rollback path with isolated files and fake host commands."""
    root = tmp_path / "app"
    (root / "data/state").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/app.py").write_text("old")
    (root / "data/otz.db").write_bytes(b"keep new data")
    uv = tmp_path / "uv"
    uv.touch()
    receipt = tmp_path / "backup.json"
    release.write_json(receipt, backup_receipt)
    revision = "b" * 40
    timer = "otz-nightly-refresh.timer"
    service = "otz-site"
    active_units = {timer, service}
    events = []
    browser_commands = []
    real_require = release.require

    def isolated_require(condition, message):
        # The real root-only guard is irrelevant in this fake-host fixture; no host commands
        # execute and every application path remains below pytest's temporary directory.
        if message.startswith("canonical units target"):
            return
        real_require(condition, message)

    def packet(_packet, destination):
        code = destination / "code"
        for name in release.RUNTIME:
            if "." not in name:
                (code / name).mkdir(parents=True)
            else:
                (code / name).write_text("fixture")
        (code / "src/app.py").write_text("new")
        (code / "data").mkdir()
        (code / "data/districts.yaml").write_text("fixture")
        return {
            "revision": revision,
            "files": {"src/app.py": release.hash_bytes(b"bad" if failure == "hash" else b"new")},
        }

    def command(args, **kwargs):
        version = (root / "src/app.py").read_text()
        events.append((args, version))
        if args[0] == "runuser":
            browser_commands.append((args, kwargs))
            if "-c" in args and failure == "browser":
                raise release.ReleaseError("fixture Chromium launch failed")
        if args[0] == str(uv):
            Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]).mkdir()
        if args[:2] == ["systemctl", "stop"]:
            active_units.difference_update(args[2:])
        if args[:2] == ["systemctl", "start"]:
            active_units.update(args[2:])
        if args[0] == "curl" and failure == "health":
            raise release.ReleaseError("fixture health failed")
        return ""

    monkeypatch.setattr(release, "require", isolated_require)
    monkeypatch.setattr(release.os, "geteuid", lambda: 0)
    monkeypatch.setattr(release, "unpack_packet", packet)
    monkeypatch.setattr(release, "current_revisions", lambda db: ["0011"])
    monkeypatch.setattr(release, "run", command)
    monkeypatch.setattr(release, "active", active_units.__contains__)
    monkeypatch.setattr(
        release, "service_state", lambda: {"services": [service], "timers": [timer]}
    )
    monkeypatch.setattr(release, "maintenance_idle", lambda: None)
    monkeypatch.setattr(release, "drain_maintenance", lambda: None, raising=False)
    monkeypatch.setattr(release, "effective_units", lambda deploy: {})
    import pwd

    monkeypatch.setattr(pwd, "getpwnam", lambda user: SimpleNamespace(pw_dir=str(tmp_path)))
    args = argparse.Namespace(remote_root=root, uv_bin=uv, backup_receipt=receipt, packet=None)
    if failure:
        with pytest.raises(release.ReleaseError):
            release.activate(args)
    else:
        assert release.activate(args)["result"] == "activated_manual_verification_pending"
    assert len(browser_commands) == 2
    install, launch = browser_commands
    assert install[0][-2:] == ["install", "chromium"]
    assert launch[0][:5] == [
        "runuser",
        "-u",
        "otz",
        "--",
        str(root / ".releases" / revision / "venv/bin/python"),
    ]
    assert "await playwright.chromium.launch(" in launch[0][-1]
    assert "await browser.close()" in launch[0][-1]
    assert install[1]["env"] == launch[1]["env"]
    assert launch[1]["env"]["HOME"] == str(tmp_path)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in launch[1]["env"]
    assert launch[1]["timeout"] == 45
    if failure == "browser":
        assert not any(args[0] == "systemctl" for args, _ in events)
        assert (root / "src/app.py").read_text() == "old"
        assert active_units == {timer, service}
        return
    launch_index = next(
        index for index, (args, _) in enumerate(events) if args[0] == "runuser" and "-c" in args
    )
    stop_index = next(
        index for index, (args, _) in enumerate(events) if args[:2] == ["systemctl", "stop"]
    )
    assert launch_index < stop_index
    timer_starts = [
        (index, version)
        for index, (args, version) in enumerate(events)
        if args[:2] == ["systemctl", "start"] and timer in args
    ]
    assert len(timer_starts) == 1
    assert timer_starts[0][1] == ("old" if failure else "new")
    health_index = next(index for index, (args, _) in enumerate(events) if args[0] == "curl")
    assert health_index < timer_starts[0][0]
    assert (root / "data/otz.db").read_bytes() == b"keep new data"


def test_rollback_timeout_preserves_runtime_until_maintenance_exits(tmp_path, monkeypatch):
    root = tmp_path / "app"
    staged = tmp_path / "release"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text("new running code")
    (staged / "rollback/src").mkdir(parents=True)
    (staged / "rollback/src/app.py").write_text("old code")
    release.write_json(
        staged / "activation.json",
        {
            "swapped": ["src"],
            "units": [],
            "services": ["otz-site"],
            "timers": ["otz-nightly-refresh.timer"],
        },
    )
    commands = []

    def command(args, **kwargs):
        commands.append(args)
        return "activating" if args[:2] == ["systemctl", "show"] else ""

    monkeypatch.setattr(release, "run", command)
    monkeypatch.setattr(release, "active", lambda unit: unit == "otz-nightly-refresh.timer")
    monkeypatch.setattr(release, "maintenance_jobs", lambda **kwargs: list(release.JOBS))
    monkeypatch.setattr(release, "manual_maintenance", lambda **kwargs: [])
    monkeypatch.setattr(release.subprocess, "run", lambda args, **kwargs: commands.append(args))
    ticks = iter([0, 0, 61])
    monkeypatch.setattr(release.time, "monotonic", lambda: next(ticks))
    with pytest.raises(release.ReleaseError, match="did not stop cooperatively"):
        release.restore_code(root, staged)
    assert commands[0] == ["systemctl", "stop", "otz-nightly-refresh.timer"]
    assert any("--signal=SIGINT" in command for command in commands)
    assert not any("SIGKILL" in " ".join(command) for command in commands)
    assert (root / "src/app.py").read_text() == "new running code"
    assert (staged / "rollback/src/app.py").read_text() == "old code"
    assert release.read_json(staged / "activation.json")["result"] == "rollback_blocked_maintenance"


def test_cooperative_stop_does_not_interrupt_same_jobs_cleanup_twice(tmp_path, monkeypatch):
    job = "otz-nightly-refresh"
    commands, signals = [], []
    monkeypatch.setattr(release, "maintenance_jobs", lambda **kwargs: [job])
    monkeypatch.setattr(release, "manual_maintenance", lambda **kwargs: [501, 502])
    original_read = Path.read_text

    def read_cgroup(path, *args, **kwargs):
        if str(path) == "/proc/501/cgroup":
            return f"0::/system.slice/{job}.service"
        if str(path) == "/proc/502/cgroup":
            return "0::/user.slice/session-1.scope"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_cgroup)
    monkeypatch.setattr(release.subprocess, "run", lambda args, **kwargs: commands.append(args))
    monkeypatch.setattr(release.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(release, "maintenance_idle", lambda **kwargs: None)
    release.drain_maintenance()
    assert commands == [["systemctl", "kill", "--kill-whom=all", "--signal=SIGINT", job]]
    assert signals == [(502, release.signal.SIGINT)]


@pytest.mark.parametrize("later_release", [False, True])
def test_blocked_rollback_can_resume_only_while_its_previous_release_is_recorded(
    tmp_path, monkeypatch, later_release
):
    root = tmp_path / "app"
    revision = "b" * 40
    previous = "a" * 40
    staged = root / ".releases" / revision
    staged.mkdir(parents=True)
    (root / "data/state").mkdir(parents=True)
    release.write_json(root / "release.json", {"revision": "c" * 40 if later_release else previous})
    release.write_json(
        staged / "activation.json",
        {
            "result": "rollback_blocked_maintenance",
            "swapped": ["src"],
            "previous_revision": previous,
        },
    )
    restored = []
    real_require = release.require

    def isolated_require(condition, message):
        if message != "unsupported rollback target":
            real_require(condition, message)

    monkeypatch.setattr(release, "require", isolated_require)
    monkeypatch.setattr(release, "restore_code", lambda *args: restored.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "release.py",
            "rollback",
            "--remote-root",
            str(root),
            "--revision",
            revision,
        ],
    )
    if later_release:
        with pytest.raises(SystemExit, match="1"):
            release.main()
        assert restored == []
    else:
        release.main()
        assert restored == [(root, staged)]
