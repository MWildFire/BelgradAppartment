"""Failure receipts identify an operation without exposing private subprocess data."""

import json
import subprocess
import sys
from contextlib import contextmanager

import pytest

from scripts import offhost_backup as job
from scripts import recovery
from tests.test_offhost_backup import environment
from tests.test_recovery import source as recovery_source


@pytest.fixture
def source(tmp_path):
    return recovery_source.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "error,expected_type,returncode",
    [
        (
            subprocess.CalledProcessError(
                23, ["private-command"], output="private-stdout", stderr="private-stderr"
            ),
            "CalledProcessError",
            23,
        ),
        (
            subprocess.TimeoutExpired(
                ["private-command"], 10, output="private-stdout", stderr="private-stderr"
            ),
            "TimeoutExpired",
            None,
        ),
        (OSError("private-path-or-secret"), "OSError", None),
    ],
)
def test_cli_failure_only_emits_static_phase_type_and_exit_code(
    monkeypatch, capsys, error, expected_type, returncode
):
    def fail(_args):
        with job.backup_phase("create_source_snapshot"):
            raise error

    monkeypatch.setattr(job, "backup", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "offhost_backup.py",
            "run",
            "--ssh-target",
            "fixture",
            "--ssh-key",
            "/fake-key",
            "--private-root",
            "/fake-private",
            "--remote-root",
            "/opt/otz",
            "--remote-python",
            "/opt/otz/.venv/bin/python",
            "--remote-published-dir",
            "/opt/otz/data/public",
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        job.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1 and output.out == ""
    assert json.loads(output.err) == {
        "result": "backup_failed",
        "accepted": False,
        "phase": "create_source_snapshot",
        "error_type": expected_type,
        "returncode": returncode,
    }
    assert "private-" not in output.err


def test_child_failure_does_not_capture_private_output_in_exception():
    with pytest.raises(subprocess.CalledProcessError) as failed:
        job.run(
            [
                sys.executable,
                "-c",
                "import sys; print('fixture-private'); print('fixture-stderr',file=sys.stderr); sys.exit(7)",
            ]
        )
    assert failed.value.returncode == 7
    assert failed.value.output is failed.value.stderr is None
    assert "fixture" not in json.dumps(job.failure_details(failed.value))


def test_cleanup_does_not_replace_the_original_failure_phase():
    original = subprocess.CalledProcessError(23, ["fixture-private-command"])
    with (
        pytest.raises(subprocess.CalledProcessError) as failed,
        job.backup_phase("backup_preflight"),
    ):
        try:
            with job.backup_phase("transfer_data_snapshot"):
                raise original
        finally:
            try:
                with job.backup_phase("remote_cleanup"):
                    raise OSError("fixture-private-cleanup-error")
            except OSError:
                pass
    assert failed.value is original
    assert job.failure_details(failed.value)["phase"] == "transfer_data_snapshot"


def test_failure_context_cycle_is_bounded_and_private_context_is_not_reported():
    original = subprocess.CalledProcessError(23, ["fixture-private-command"])
    original._offhost_phase = "transfer_data_snapshot"
    cleanup = BrokenPipeError("fixture-private-cleanup")
    cleanup.__context__ = original
    original.__context__ = cleanup
    assert job.failure_details(cleanup) == {
        "phase": "transfer_data_snapshot",
        "error_type": "CalledProcessError",
        "returncode": 23,
    }


def test_untagged_context_does_not_replace_a_known_operation():
    error = subprocess.CalledProcessError(23, ["fixture-private-command"])
    error._offhost_phase = "transfer_data_snapshot"
    error.__context__ = OSError("fixture-private-earlier-handled-error")
    assert job.failure_details(error)["error_type"] == "CalledProcessError"


@pytest.mark.parametrize(
    "phase,cleanup_failure",
    [
        ("remote_source_lease", False),
        ("create_source_snapshot", False),
        ("transfer_data_snapshot", False),
        ("transfer_data_snapshot", True),
    ],
)
def test_real_backup_failure_reports_the_exact_early_boundary(
    source, tmp_path, monkeypatch, phase, cleanup_failure
):
    args = environment(source, tmp_path, monkeypatch)
    previous_remote = job.remote

    @contextmanager
    def failed_lease(_args):
        if phase == "remote_source_lease":
            raise recovery.RecoveryError("fixture-private-lock")
        try:
            yield
        finally:
            if cleanup_failure:
                raise BrokenPipeError("fixture-private-cleanup")

    def failed_remote(remote_args, command):
        if "snapshot" in command:
            raise subprocess.CalledProcessError(24, ["fixture-private-command"])
        return previous_remote(remote_args, command)

    def failed_transfer(_command, **_kwargs):
        raise subprocess.CalledProcessError(23, ["fixture-private-command"])

    if phase == "remote_source_lease" or cleanup_failure:
        monkeypatch.setattr(job, "remote_lease", failed_lease)
    if phase == "create_source_snapshot":
        monkeypatch.setattr(job, "remote", failed_remote)
    elif phase == "transfer_data_snapshot":
        monkeypatch.setattr(job, "run", failed_transfer)
    with pytest.raises(
        (recovery.RecoveryError, subprocess.CalledProcessError, BrokenPipeError)
    ) as failed:
        job.backup(args)
    assert job.failure_details(failed.value)["phase"] == phase
    if phase == "transfer_data_snapshot":
        assert job.failure_details(failed.value)["returncode"] == 23
        assert job.failure_details(failed.value)["error_type"] == "CalledProcessError"
    assert job.completed(args.private_root) == []
    pending = list((args.private_root / "pending").iterdir())
    assert len(pending) == 1 and not list(pending[0].iterdir())
