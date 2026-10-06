#!/usr/bin/env python3
"""Exact-revision, receipt-gated staged releases. Never restores/downgrades live data."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ("src", "alembic", "scripts", "deploy", "pyproject.toml", "uv.lock", "alembic.ini")
SYSTEMD_UNITS = Path("/etc/systemd/system")
SERVICES = ("otz-monitor", "otz-site", "otz-admin", "otz-subscription-bot")
JOBS = (
    "otz-nightly-refresh", "otz-weekly-full-refresh", "otz-scrape-smoke",
    "otz-daily-summary", "otz-public-detail-refresh",
)
TIMERS = tuple(f"{name}.timer" for name in JOBS)
CHECKS = ("unit", "changed_lint", "shell", "browser")
MAINTENANCE_PATTERN = (
    r"(src\.cli |/otz |^otz )(nightly-refresh|weekly-refresh|scrape-smoke|scrape|"
    r"refresh-broken|rolling-refresh|verify-links|daily-summary|refetch-details|"
    r"check-links|heal-urls|site-liveness|mark-stale|refresh|nekretnine-shadow-audit)( |$)"
)


class ReleaseError(Exception):
    """Release gate failed before completion."""


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def run(args, *, cwd=None, env=None, capture=False, timeout=None):
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout,
    ).stdout


def hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def wait_for_http_health(*urls, timeout=90, poll_interval=1, request_timeout=3):
    """Require real HTTP 200 from every endpoint within one startup deadline."""
    require(
        urls and all(math.isfinite(value) and value > 0
                     for value in (timeout, poll_interval, request_timeout)),
        "positive bounded HTTP readiness parameters required",
    )
    started = time.monotonic()
    deadline = started + timeout
    attempts, last_status = 0, "unavailable"
    while time.monotonic() < deadline:
        statuses = {}
        for url in urls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            budget = min(request_timeout, remaining)
            attempts += 1
            try:
                status = run(
                    [
                        "curl", "--disable", "--silent", "--noproxy", "*", "--output", "/dev/null",
                        "--write-out", "%{http_code}",
                        "--connect-timeout", str(min(2, max(0.001, budget))),
                        "--max-time", str(max(0.001, budget)), url,
                    ],
                    capture=True, timeout=budget,
                ).strip()
            except subprocess.CalledProcessError as error:
                last_status = f"curl_exit_{error.returncode}"
                break
            except subprocess.TimeoutExpired:
                last_status = "request_timeout"
                break
            last_status = status if re.fullmatch(r"[0-9]{3}", status) else "invalid_http_status"
            if status != "200":
                break
            statuses[urlsplit(url).path] = 200
        else:
            if time.monotonic() <= deadline:
                return {
                    "statuses": statuses, "attempts": attempts,
                    "elapsed_sec": round(time.monotonic() - started, 3),
                }
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval, remaining))
    raise ReleaseError(
        f"local readiness requires HTTP 200 within {timeout:g}s; "
        f"attempts={attempts}, last_status={last_status}"
    )


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_tests(receipt, revision, checks):
    require(receipt.get("revision") == revision, "tests do not cover the exact release revision")
    require(receipt.get("result") == "passed", "test receipt is not passed")
    require(
        all(receipt.get("checks", {}).get(check) == "passed" for check in checks),
        "required checks missing or not passed",
    )


def validate_backup(receipt, now=None, *, allow_data_only=False):
    require(receipt.get("result") == "data_restore_verified", "backup restore is not verified")
    created = datetime.fromisoformat(receipt["created_at"])
    require(created.tzinfo is not None, "backup timestamp missing timezone")
    age = ((now or datetime.now(UTC)) - created).total_seconds() / 3600
    require(-0.1 <= age <= 36, "release requires a backup no older than 36 hours")
    backup_format = receipt.get("backup_format", "tar_archive")
    data_only = backup_format == "data_only_directory_generation"
    if backup_format == "directory_generation" or data_only:
        digest = receipt.get("manifest_sha256", "")
        require(
            re.fullmatch(r"[0-9a-f]{64}", digest)
            and digest == receipt.get("generation_manifest_sha256"),
            "verified generation manifest SHA-256 missing or inconsistent",
        )
        require(
            re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", receipt.get("generation_id", "")),
            "verified generation identity missing",
        )
    else:
        require(backup_format == "tar_archive", "unsupported backup receipt format")
        require(
            re.fullmatch(r"[0-9a-f]{64}", receipt.get("archive_sha256", "")),
            "verified archive SHA-256 missing",
        )
    require(receipt.get("offhost_verified") is True, "off-host backup is not verified")
    if data_only:
        require(allow_data_only, "data-only backup requires explicit code-only release mode")
        require(receipt.get("protected_recovery_verified") is False
                and receipt.get("backup_scope") == "db_media"
                and (receipt.get("credential_inspection", {}).get("result")
                     == "heuristic_scan_passed")
                and receipt.get("source_snapshot_immutable") is True
                and receipt.get("media_decode_verified") is True
                and type(receipt.get("media_files_verified")) is int
                and receipt["media_files_verified"] > 0
                and receipt.get("application", {}).get("network") == "disabled"
                and re.fullmatch(r"[a-f0-9]{40}", receipt.get("source_revision", "")),
                "incomplete data-only recovery proof")
    else:
        require(receipt.get("protected_recovery_verified") is True,
                "protected configuration/session recovery is not verified")
    application = receipt.get("application", {})
    require(
        all(application.get(name) == 200 for name in ("catalog", "apartment", "photo"))
        and application.get("photo_decoded") is True,
        "application/photo restore gate missing",
    )
    require(receipt.get("database", {}).get("revisions"), "backup schema revision missing")


def validate_data_only_release(root, manifest, backup):
    """Narrow opt-in: same schema/dependencies/config/units, with the current runtime as rollback.

    This does not assert protected disaster recovery and never permits a schema transition.
    Default deployments retain the existing protected-backup gate.
    """
    require(backup.get("backup_format") == "data_only_directory_generation",
            "code-only mode requires a data-only receipt")
    previous = read_json(root / "release.json")
    require(previous["revision"] == backup.get("source_revision"),
            "data-only snapshot belongs to a different source release")
    before, after = previous["files"], manifest["files"]
    fixed = {"pyproject.toml", "uv.lock", "alembic.ini", "src/config.py", "src/db/models.py",
             "scripts/runtime_contract.json", "data/districts.yaml"}
    names = fixed | {name for name in set(before) | set(after)
                     if name.startswith(("alembic/", "deploy/"))}
    require(all(name in before and name in after and before[name] == after[name] for name in names),
            "data-only mode forbids schema/dependency/config/unit changes")
    require(all(allowed_path(name) and (root / name).is_file()
                and hash_bytes((root / name).read_bytes()) == before[name] for name in names),
            "live schema/dependency/config/unit files differ from source release")


def allowed_path(name):
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and str(path) == name


def prepare(args):
    revision = run(
        ["git", "rev-parse", f"{args.revision}^{{commit}}"], cwd=ROOT, capture=True
    ).strip()
    checks = args.required_check or list(CHECKS)
    receipt = read_json(args.test_receipt)
    validate_tests(receipt, revision, checks)
    raw = subprocess.run(
        ["git", "archive", "--format=tar", revision], cwd=ROOT, check=True, stdout=subprocess.PIPE
    ).stdout
    contents, modes = {}, {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            require(member.isfile() and allowed_path(member.name), "unsafe tracked release file")
            path = PurePosixPath(member.name)
            require(
                not any(
                    p == ".env"
                    or (p.startswith(".env.") and p != ".env.example")
                    or ".session" in p
                    for p in path.parts
                ),
                "credential/session tracked in release",
            )
            require(
                path.parts[0] != "data" or member.name in {"data/districts.yaml", "data/.gitkeep"},
                "private data tracked in release",
            )
            contents[member.name] = tar.extractfile(member).read()
            modes[member.name] = 0o755 if member.mode & 0o111 else 0o644
    require(
        all(name in contents for name in ("pyproject.toml", "uv.lock", "scripts/release.py")),
        "release requires tracked lockfile and release helper",
    )
    manifest = {
        "format": 1,
        "revision": revision,
        "created_at": datetime.now(UTC).isoformat(),
        "files": {name: hash_bytes(value) for name, value in sorted(contents.items())},
        "modes": modes,
        "required_checks": checks,
        "tests": receipt,
    }
    require(not args.output.exists(), "release packet already exists")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "w:gz") as tar:
        for name, content in sorted(contents.items()):
            info = tarfile.TarInfo(f"code/{name}")
            info.size, info.mode = len(content), modes[name]
            tar.addfile(info, io.BytesIO(content))
        payload = json.dumps(manifest, sort_keys=True).encode()
        info = tarfile.TarInfo("release.json")
        info.size, info.mode = len(payload), 0o600
        tar.addfile(info, io.BytesIO(payload))
    args.output.chmod(0o600)
    return {
        "result": "packet_prepared",
        "revision": revision,
        "packet": str(args.output),
        "files": len(contents),
        "sha256": hash_bytes(args.output.read_bytes()),
    }


def unpack_packet(packet, destination):
    with tarfile.open(packet, "r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        require(len(names) == len(set(names)), "duplicate packet members")
        require("release.json" in names, "packet release manifest missing")
        for member in members:
            require(member.isfile() and allowed_path(member.name), "unsafe packet member")
        manifest = json.load(tar.extractfile("release.json"))
        require(
            manifest["format"] == 1 and re.fullmatch(r"[0-9a-f]{40}", manifest["revision"]),
            "invalid release manifest",
        )
        require(
            set(names) == {"release.json", *("code/" + name for name in manifest["files"])},
            "packet inventory mismatch",
        )
        validate_tests(manifest["tests"], manifest["revision"], manifest["required_checks"])
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        for member in members:
            content = tar.extractfile(member).read()
            if member.name != "release.json":
                require(
                    hash_bytes(content) == manifest["files"][member.name.removeprefix("code/")],
                    "packet hash mismatch",
                )
            target = destination / member.name
            # Runtime folders must remain traversable after a root-private operator
            # invocation. Correct only directories created for validated packet paths;
            # the private outer staging directory and existing folders keep their modes.
            directory = destination
            for part in Path(member.name).parts[:-1]:
                directory = directory / part
                try:
                    directory.mkdir(mode=0o755)
                except FileExistsError:
                    require(
                        directory.is_dir() and not directory.is_symlink(),
                        "unsafe packet directory",
                    )
                else:
                    directory.chmod(0o755)
            target.write_bytes(content)
            target.chmod(
                0o600
                if member.name == "release.json"
                else manifest["modes"][member.name.removeprefix("code/")]
            )
    return manifest


def active(unit):
    status = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False, timeout=5)
    return status.returncode == 0


def maintenance_timeout(deadline, limit):
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    require(
        remaining > 0, "maintenance did not stop cooperatively; runtime rollback has not started"
    )
    return min(limit, remaining)


def maintenance_jobs(*, deadline=None):
    # Oneshot services are "activating" while ExecStart runs, not "active".
    running = []
    for job in JOBS:
        status = run(
            ["systemctl", "show", job, "--property=ActiveState", "--value"],
            capture=True,
            timeout=maintenance_timeout(deadline, 5),
        ).strip()
        if status not in {"", "inactive", "failed"}:
            running.append(job)
    return running


def manual_maintenance(*, deadline=None):
    process = subprocess.run(
        [
            "pgrep",
            "-u",
            "otz",
            "-f",
            MAINTENANCE_PATTERN,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=maintenance_timeout(deadline, 5),
    )
    require(process.returncode in {0, 1}, "manual maintenance cannot be checked")
    return [int(pid) for pid in process.stdout.split()]


def maintenance_idle(*, deadline=None):
    require(not maintenance_jobs(deadline=deadline), "maintenance job is running or starting")
    require(not manual_maintenance(deadline=deadline), "manual maintenance process is active")


def pause_timers(state):
    timers = sorted(set(state["timers"]) | {unit for unit in TIMERS if active(unit)})
    if timers:
        run(["systemctl", "stop", *timers], timeout=30)


def drain_maintenance(timeout=60):
    """Request cooperative cancellation once; never escalate to SIGKILL or swap under a job."""
    deadline = time.monotonic() + timeout
    jobs = maintenance_jobs(deadline=deadline)
    manual = []
    for pid in manual_maintenance(deadline=deadline):
        # The same CLI also appears in pgrep when run by systemd. A second SIGINT would
        # interrupt asyncio.Runner's cancellation cleanup, so signal each process only once.
        try:
            cgroup = Path(f"/proc/{pid}/cgroup").read_text()
        except FileNotFoundError:
            continue
        if not any(f"/{job}.service" in cgroup for job in jobs):
            manual.append(pid)
    if jobs:
        # kill only sends the requested signal; unlike systemctl stop it does not schedule
        # systemd's later forced kill. An already exited unit is handled by the idle check.
        subprocess.run(
            ["systemctl", "kill", "--kill-whom=all", "--signal=SIGINT", *jobs],
            check=False,
            timeout=maintenance_timeout(deadline, 10),
        )
    for pid in manual:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
    while True:
        try:
            maintenance_idle(deadline=deadline)
            return
        except ReleaseError:
            require(
                time.monotonic() < deadline,
                "maintenance did not stop cooperatively; runtime rollback has not started",
            )
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def service_state():
    maintenance_idle()
    return {
        "services": [name for name in SERVICES if active(name)],
        "timers": [name for name in TIMERS if active(name)],
    }


def stop_units(state):
    if state["timers"]:
        run(["systemctl", "stop", *state["timers"]])
    # Close the race between the initial snapshot and pausing timers.
    maintenance_idle()
    if state["services"]:
        run(["systemctl", "stop", *state["services"]])


def restart_services(state):
    if state["services"]:
        run(["systemctl", "start", *state["services"]])
        require(
            all(active(name) for name in state["services"]), "application service did not start"
        )


def restart_timers(state):
    if state["timers"]:
        run(["systemctl", "start", *state["timers"]])


def restart_units(state):
    restart_services(state)
    restart_timers(state)


def current_revisions(db):
    with sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        require(
            connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "live database integrity check failed",
        )
        require(
            not connection.execute("PRAGMA foreign_key_check").fetchall(), "live FK check failed"
        )
        return sorted(
            row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
        )


def effective_units(deploy):
    """Check the effective command/type/user/timeout; retain safe scheduler evidence."""
    evidence = {}
    for unit in sorted(deploy.glob("otz-*")):
        if unit.suffix not in {".service", ".timer"}:
            continue
        configured = {}
        for line in unit.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                configured[key] = value
        properties = (
            [
                "ExecStart",
                "ExecStartPre",
                "User",
                "Type",
                "WorkingDirectory",
                "TimeoutStartUSec",
                "DropInPaths",
            ]
            if unit.suffix == ".service"
            else [
                "TimersCalendar",
                "RandomizedDelayUSec",
                "Persistent",
                "NextElapseUSecRealtime",
                "DropInPaths",
            ]
        )
        actual = {
            key: run(
                ["systemctl", "show", unit.name, f"--property={key}", "--value"], capture=True
            ).strip()
            for key in properties
        }
        if unit.suffix == ".service":
            require(
                configured["ExecStart"] in actual["ExecStart"],
                f"effective {unit.name} command differs from release",
            )
            # Replacing a base unit leaves local drop-ins active. Canonical services
            # without a pre-start command must not inherit one (for example a legacy
            # browser download outside the application lease). Other overrides remain
            # operator-owned; do not reject or delete a drop-in merely because it exists.
            if not configured.get("ExecStartPre"):
                require(
                    not actual["ExecStartPre"],
                    f"effective {unit.name} has unexpected ExecStartPre",
                )
            for key in ("User", "Type", "WorkingDirectory"):
                require(actual[key] == configured[key], f"effective {unit.name} {key} differs")
            if "TimeoutStartSec" in configured:
                require(
                    actual["TimeoutStartUSec"] == configured["TimeoutStartSec"],
                    f"effective {unit.name} timeout differs",
                )
        else:
            calendar = re.search(r"OnCalendar=([^;}}]+)", actual["TimersCalendar"])
            require(
                calendar and calendar.group(1).strip() == configured["OnCalendar"],
                f"effective {unit.name} calendar differs",
            )
            require(
                actual["RandomizedDelayUSec"] == configured["RandomizedDelaySec"]
                and actual["Persistent"] == "yes",
                f"effective {unit.name} timing differs",
            )
        evidence[unit.name] = actual
    return evidence


def swap_path(source, target, previous):
    if target.exists() or target.is_symlink():
        previous.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, previous)
    try:
        os.replace(source, target)
    except Exception:
        if previous.exists() or previous.is_symlink():
            os.replace(previous, target)
        raise


def journal_swap(root, release, state, relative, source):
    target = root / relative
    state["swap_intent"] = {
        "path": relative, "had_previous": target.exists() or target.is_symlink(),
    }
    write_json(release / "activation.json", state)
    swap_path(source, target, release / "rollback" / relative)
    state["swapped"].append(relative)
    state.pop("swap_intent")
    write_json(release / "activation.json", state)


def reconcile_swap_intent(root, release, state):
    intent = state.get("swap_intent")
    if not intent:
        return
    relative = intent["path"]
    require(relative in (*RUNTIME, "data/districts.yaml", ".venv", "release.json"),
            "invalid interrupted swap path")
    target, prior = root / relative, release / "rollback" / relative
    source = release / ({".venv": "new-venv-link", "release.json": "new-release.json"}
                        .get(relative, "code/" + relative))
    prior_exists = prior.exists() or prior.is_symlink()
    target_exists = target.exists() or target.is_symlink()
    source_exists = source.exists() or source.is_symlink()
    moved = prior_exists or (not intent["had_previous"] and not source_exists and target_exists)
    require(not intent["had_previous"] or prior_exists or target_exists,
            "interrupted swap lost its previous runtime path")
    if moved and relative not in state["swapped"]:
        state["swapped"].append(relative)
    state.pop("swap_intent")
    write_json(release / "activation.json", state)


def restore_code(root, release):
    """Only application code/venv/units. New database rows and media are untouched."""
    state = read_json(release / "activation.json")
    try:
        pause_timers(state)
        drain_maintenance()
    except Exception:
        state["result"] = "rollback_blocked_maintenance"
        write_json(release / "activation.json", state)
        raise
    services = sorted(set(state["services"]) | {unit for unit in SERVICES if active(unit)})
    if services:
        run(["systemctl", "stop", *services], timeout=60)
    try:
        reconcile_swap_intent(root, release, state)
        revisions = current_revisions(root / "data/otz.db")
        if state.get("fallback_revision") and not legacy_schema(revisions):
            fallback = release / "fallback"
            manifest = read_json(fallback / "release.json")
            require(manifest["revision"] == state["fallback_revision"]
                    and hash_bytes((fallback / "release.json").read_bytes())
                    == state["fallback_manifest_sha256"], "staged fallback identity changed")
            validate_tests(manifest["tests"], manifest["revision"], CHECKS)
            verify_rollback_compatibility(
                fallback / "code", fallback, {"swapped": []}, revisions, manifest=manifest,
            )
            return restore_fallback(root, release, state, manifest, revisions, services)
        verify_rollback_compatibility(root, release, state, revisions)
    except Exception:
        if state.get("result") != "rollback_fallback_failed":
            state["result"] = "rollback_blocked_incompatible"
        write_json(release / "activation.json", state)
        raise
    rejected = release / "rejected"
    rejected.mkdir(exist_ok=True)
    state.setdefault("legacy_restored", [])
    for relative in reversed(state["swapped"]):
        if relative in state["legacy_restored"]:
            continue
        target = root / relative
        prior = release / "rollback" / relative
        destination = rejected / relative
        prior_exists = prior.exists() or prior.is_symlink()
        target_exists = target.exists() or target.is_symlink()
        intent = state.get("legacy_restore_intent")
        if intent and intent["path"] == relative and intent["prior_present"] and not prior_exists:
            # The previous path moved back before the completion journal. A rejected
            # candidate need not exist if activation stopped after its first rename.
            require(target_exists, "interrupted legacy restore lost its previous path")
            state["legacy_restored"].append(relative)
            state.pop("legacy_restore_intent")
            write_json(release / "activation.json", state)
            continue
        state["legacy_restore_intent"] = {"path": relative, "prior_present": prior_exists}
        write_json(release / "activation.json", state)
        destination_exists = destination.exists() or destination.is_symlink()
        # A prior retry may already have restored this path before its journal write.
        if not destination_exists and (target.exists() or target.is_symlink()):
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, destination)
        if prior.exists() or prior.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prior, target)
        state["legacy_restored"].append(relative)
        state.pop("legacy_restore_intent")
        write_json(release / "activation.json", state)
    for name in state["units"]:
        prior = release / "rollback-units" / name
        target = SYSTEMD_UNITS / name
        if prior.exists():
            shutil.copy2(prior, target)
        elif target.exists():
            target.unlink()
    run(["systemctl", "daemon-reload"])
    state["result"] = "rolled_back_code_only"
    write_json(release / "activation.json", state)
    restart_units(state)
    return state


def legacy_schema(revisions):
    return bool(revisions) and all(
        re.fullmatch(r"00(?:0[1-9]|1[0-4])(?:_[a-z0-9_]+)?", r) for r in revisions
    )


def restore_fallback(root, release, state, manifest, revisions, services):
    """Resume a copy/swap from the immutable queue-aware fallback; never restore data."""
    fallback = release / "fallback"
    rejected = release / "fallback-rejected"
    staged = release / "fallback-install"
    staged.mkdir(exist_ok=True)
    state["result"] = "rollback_fallback_in_progress"
    state.setdefault("fallback_swapped", [])
    write_json(release / "activation.json", state)

    def matches(relative):
        target = root / relative
        if relative == ".venv":
            return target.is_symlink() and target.resolve() == (fallback / "venv").resolve()
        if relative == "release.json":
            return target.is_file() and target.read_bytes() == (fallback / relative).read_bytes()
        files = {name: digest for name, digest in manifest["files"].items()
                 if name == relative or name.startswith(relative + "/")}
        if target.is_dir():
            actual = {str(p.relative_to(root)) for p in target.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts}
            if actual != set(files):
                return False
        return bool(files) and all((root / name).is_file()
            and hash_bytes((root / name).read_bytes()) == digest for name, digest in files.items())

    try:
        # Independently exercise the fallback environment, not the rejected candidate's.
        run([str(fallback / "venv/bin/python"), "-c",
             "import sqlalchemy,playwright,torch,open_clip,fastapi"], timeout=60)
        for relative in (*RUNTIME, "data/districts.yaml", ".venv", "release.json"):
            if matches(relative):
                # Also handles process death after rename but before journal write.
                if relative not in state["fallback_swapped"]:
                    state["fallback_swapped"].append(relative)
                    write_json(release / "activation.json", state)
                continue
            source = staged / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink() or source.is_file():
                source.unlink()
            elif source.is_dir():
                shutil.rmtree(source)
            if relative == ".venv":
                source.symlink_to(fallback / "venv", target_is_directory=True)
            else:
                origin = fallback / (relative if relative == "release.json" else "code/" + relative)
                require(origin.exists(), f"fallback runtime path missing: {relative}")
                if origin.is_dir():
                    shutil.copytree(origin, source)
                else:
                    shutil.copy2(origin, source)
            # A prior interruption may have moved current code aside. Never overwrite
            # either that archive or the original legacy rollback tree.
            target = root / relative
            prior = rejected / relative
            require(not prior.exists() or not (target.exists() or target.is_symlink()),
                    f"fallback target changed after interrupted swap: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            swap_path(source, target, prior)
            state["fallback_swapped"].append(relative)
            write_json(release / "activation.json", state)
        run(["chown", "-hR", "otz:otz", *(str(root / name) for name in RUNTIME[:4])])
        desired = {p.name: p for p in (root / "deploy").glob("otz-*")
                   if p.suffix in {".service", ".timer"}}
        state["fallback_units"] = sorted(
            set(state.get("fallback_units", [])) | set(state["units"]) | set(desired)
        )
        write_json(release / "activation.json", state)
        for name in state["fallback_units"]:
            require(re.fullmatch(r"otz-[a-z0-9-]+\.(service|timer)", name),
                    "invalid fallback unit name")
            target = SYSTEMD_UNITS / name
            if name in desired:
                shutil.copy2(desired[name], target)
            elif target.exists():
                target.unlink()
            state.setdefault("fallback_units_installed", [])
            if name not in state["fallback_units_installed"]:
                state["fallback_units_installed"].append(name)
            write_json(release / "activation.json", state)
        run(["systemctl", "daemon-reload"])
        verify_rollback_compatibility(
            root, release, {"swapped": []}, revisions, manifest=manifest,
        )
        require(current_revisions(root / "data/otz.db") == revisions,
                "schema changed during fallback")
        state["fallback_effective_units"] = effective_units(root / "deploy")
        restart_services(state)
        state["fallback_http_readiness"] = wait_for_http_health(
            "http://127.0.0.1:8001/healthz", "http://127.0.0.1:8001/readyz",
        )
        restart_timers(state)
        state["result"] = "rolled_back_to_compatible_fallback"
        write_json(release / "activation.json", state)
        return state
    except BaseException:
        # No schedules run against a partially installed/unhealthy fallback.
        pause_timers(state)
        if services:
            run(["systemctl", "stop", *services], timeout=60)
        state["result"] = "rollback_fallback_failed"
        write_json(release / "activation.json", state)
        raise


def verify_rollback_compatibility(root, release, state, revisions, *, manifest=None):
    """Additive SQL is insufficient: the prior runtime must understand durable work.

    The declaration and every prior runtime file are bound to its release manifest.
    Missing/unknown contracts fail closed, leaving current code/data untouched.
    This is a compatibility declaration tested in CI, not a proof of model quality.
    """
    require(revisions, "cannot establish rollback schema compatibility")
    if legacy_schema(revisions):
        return  # Before durable validation/identity tables existed.

    def prior(relative):
        parts = PurePosixPath(relative).parts
        swapped = any(relative == item or item == parts[0] for item in state["swapped"])
        return (release / "rollback" if swapped else root) / relative

    if manifest is None:
        marker = prior("release.json")
        require(marker.is_file(), "rollback requires a verified prior release manifest")
        manifest = read_json(marker)
    files = manifest.get("files", {})
    contract_path = "scripts/runtime_contract.json"
    require(contract_path in files, "rollback lacks a schema compatibility contract")
    require({"src/matching/matcher.py", "src/matching/llm_validator.py",
             "src/matching/validation_jobs.py", "src/matching/identity.py",
             "src/telegram/digest_jobs.py"} <= set(files),
            "prior rollback manifest lacks critical runtime files")
    for relative, digest in files.items():
        require(allowed_path(relative), "unsafe prior release manifest path")
        # Runtime only: docs/tests need not exist in the activated application directory.
        if PurePosixPath(relative).parts[0] not in RUNTIME and relative != "data/districts.yaml":
            continue
        candidate = prior(relative)
        require(candidate.is_file() and hash_bytes(candidate.read_bytes()) == digest,
                f"prior rollback payload hash mismatch: {relative}")
    contract = read_json(prior(contract_path))
    require(contract.get("version") == 1, "unsupported rollback compatibility contract")
    require(set(revisions) <= set(contract.get("compatible_revisions", [])),
            "prior runtime is incompatible with current schema")
    require({"fail_closed_validation", "durable_digest", "identity_edges"}
            <= set(contract.get("semantics", [])), "prior runtime lacks durable-work semantics")


def stage_runtime_environment(release, uv_bin):
    """Install and smoke-test one isolated packet environment before stopping writers."""
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(release / "venv")
    # New environment only: failure cannot break the currently running services.
    run(
        [
            str(uv_bin),
            "sync",
            "--project",
            str(release / "code"),
            "--frozen",
            "--no-dev",
            "--no-editable",
            "--extra",
            "clip",
            "--python",
            "/usr/bin/python3",
        ],
        env=env,
    )
    new_python = release / "venv/bin/python"
    run([str(new_python), "-c", "import sqlalchemy,playwright,torch,open_clip,fastapi"])
    # Install under the actual service account/home, not root's cache or an assumed home.
    # Existing service environment overrides remain an operator configuration gate.
    import pwd

    service_home = pwd.getpwnam("otz").pw_dir
    run(["chown", "-hR", "otz:otz", str(release / "venv")])
    browser_env = {**os.environ, "HOME": service_home}
    browser_env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    run(
        [
            "runuser",
            "-u",
            "otz",
            "--",
            str(release / "venv/bin/playwright"),
            "install",
            "chromium",
        ],
        env=browser_env,
    )
    # A downloaded executable is insufficient: prove Linux shared-library/cache access
    # under the same account and HOME before touching the currently running release.
    browser_smoke = (
        "import asyncio\n"
        "from playwright.async_api import async_playwright\n"
        "async def check():\n"
        "    async with async_playwright() as playwright:\n"
        "        browser = await playwright.chromium.launch(headless=True, timeout=30000)\n"
        "        await browser.close()\n"
        "asyncio.run(check())\n"
    )
    run(
        ["runuser", "-u", "otz", "--", str(new_python), "-c", browser_smoke],
        env=browser_env,
        timeout=45,
    )
    return new_python


def staged_heads(new_python, code):
    """Inspect actual Alembic revisions without connecting to a database."""
    head_probe = (
        "import json,sys; from alembic.config import Config; "
        "from alembic.script import ScriptDirectory; cfg=Config(); "
        "cfg.set_main_option('script_location',sys.argv[1]); "
        "print(json.dumps(ScriptDirectory.from_config(cfg).get_heads()))"
    )
    target_heads = json.loads(run(
        [str(new_python), "-c", head_probe, str(code / "alembic")],
        capture=True, timeout=30,
    ))
    require(isinstance(target_heads, list) and target_heads
            and all(isinstance(head, str) for head in target_heads),
            "cannot establish staged migration heads")
    return target_heads


def stage_fallback(packet, release, candidate_revision, target_heads, uv_bin):
    """Prepare a separately tested runtime; no service or live database operations."""
    fallback = release / "fallback"
    with tempfile.TemporaryDirectory(prefix="otz-fallback-") as temporary:
        unpacked = Path(temporary)
        manifest = unpack_packet(packet, unpacked)
        validate_tests(manifest["tests"], manifest["revision"], CHECKS)
        require(manifest["revision"] != candidate_revision, "fallback must be a distinct revision")
        for relative in (*RUNTIME, "data/districts.yaml"):
            require((unpacked / "code" / relative).exists(), f"incomplete fallback: {relative}")
        verify_rollback_compatibility(
            unpacked / "code", unpacked, {"swapped": []}, target_heads, manifest=manifest,
        )
        fallback.mkdir(mode=0o750)
        fallback.chmod(0o750)
        run(["chown", "root:otz", str(fallback)])
        shutil.copytree(unpacked / "code", fallback / "code")
        write_json(fallback / "release.json", manifest)
    python = stage_runtime_environment(fallback, uv_bin)
    heads = staged_heads(python, fallback / "code")
    contract = read_json(fallback / "code/scripts/runtime_contract.json")
    require(sorted(heads) == sorted(target_heads)
            == sorted(contract.get("migration_heads", [])), "fallback migration heads mismatch")
    return {"fallback_revision": manifest["revision"],
            "fallback_manifest_sha256": hash_bytes((fallback / "release.json").read_bytes()),
            "fallback_packet_sha256": hash_bytes(packet.read_bytes())}


def fallback_marker_gap(root, release, state):
    """Prove interrupted marker replacement without accepting an unrelated release."""
    if state.get("result") not in {"rollback_fallback_in_progress", "rollback_fallback_failed"}:
        return False
    if not state.get("fallback_revision") or (root / "release.json").exists():
        return False
    archived = release / "fallback-rejected/release.json"
    if not archived.is_file() or read_json(archived).get("revision") not in {
        state["revision"], state.get("previous_revision"),
    }:
        return False
    fallback = release / "fallback"
    if not (root / ".venv").is_symlink() or (root / ".venv").resolve() != fallback / "venv":
        return False
    marker = fallback / "release.json"
    require(hash_bytes(marker.read_bytes()) == state["fallback_manifest_sha256"],
            "staged fallback identity changed")
    manifest = read_json(marker)
    require(manifest["revision"] == state["fallback_revision"], "fallback revision changed")
    # The marker is swapped last: all runtime hashes must already match the fallback.
    verify_rollback_compatibility(
        root, release, {"swapped": []}, current_revisions(root / "data/otz.db"),
        manifest=manifest,
    )
    return True


def activation_marker_gap(root, release, state):
    """Prove an interruption between the final candidate marker's two renames."""
    if state.get("result") not in {"activating", "activated_manual_verification_pending"}:
        return False
    archived = release / "rollback/release.json"
    if (root / "release.json").exists() or not archived.is_file():
        return False
    if read_json(archived).get("revision") != state.get("previous_revision"):
        return False
    if not (root / ".venv").is_symlink() or (root / ".venv").resolve() != release / "venv":
        return False
    manifest = read_json(release / "release.json")
    require(manifest["revision"] == state["revision"], "candidate revision changed")
    validate_tests(manifest["tests"], state["revision"], CHECKS)
    verify_rollback_compatibility(
        root, release, {"swapped": []}, current_revisions(root / "data/otz.db"),
        manifest=manifest,
    )
    return True


def stage_release_control(root, release, manifest):
    """Keep the verified recovery controller outside runtime swaps (including scripts)."""
    source = release / "code/scripts/release.py"
    digest = manifest["files"].get("scripts/release.py")
    require(source.is_file() and hash_bytes(source.read_bytes()) == digest,
            "release controller missing from verified packet")
    control = release / "control"
    control.mkdir(mode=0o700)
    destination = control / "release.py"
    shutil.copy2(source, destination)
    destination.chmod(0o600)
    return {
        "control_sha256": digest,
        "rollback_command": ["/usr/bin/python3", str(destination), "rollback",
                             "--remote-root", str(root), "--revision", manifest["revision"]],
    }


def activate(args):
    import fcntl

    root = args.remote_root.resolve()
    require(root == Path("/opt/otz"), "canonical units target /opt/otz; different root unsupported")
    require(os.geteuid() == 0, "activation requires root for coherent systemd update")
    require(args.uv_bin.is_absolute() and args.uv_bin.is_file(), "explicit uv binary unavailable")
    backup = read_json(args.backup_receipt)
    code_only = bool(getattr(args, "code_only_data_backup", False))
    if code_only:
        validate_backup(backup, allow_data_only=True)
    else:
        validate_backup(backup)
    with (root / "data/state/release.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError("another release operator holds the lock") from None
        with tempfile.TemporaryDirectory(prefix="otz-packet-") as temporary:
            unpacked = Path(temporary)
            manifest = unpack_packet(args.packet, unpacked)
            validate_tests(manifest["tests"], manifest["revision"], CHECKS)
            if code_only:
                require(getattr(args, "fallback_packet", None) is None,
                        "code-only mode rolls back to the current verified runtime")
                validate_data_only_release(root, manifest, backup)
            require(
                current_revisions(root / "data/otz.db") == sorted(backup["database"]["revisions"]),
                "live schema no longer matches the verified backup",
            )
            release = root / ".releases" / manifest["revision"]
            require(
                not release.exists(), "release already staged; inspect existing activation first"
            )
            release.mkdir(parents=True, mode=0o750)
            release.chmod(0o750)
            release.parent.chmod(0o750)
            run(["chown", "root:otz", str(release.parent), str(release)])
            shutil.copytree(unpacked / "code", release / "code")
            write_json(release / "release.json", manifest)
            write_json(release / "backup-receipt.json", backup)
        new_python = stage_runtime_environment(release, args.uv_bin)
        state = service_state()
        state.update(revision=manifest["revision"], result="activating", swapped=[], units=[])
        state.update(stage_release_control(root, release, manifest))
        state["previous_revision"] = (
            read_json(root / "release.json")["revision"]
            if (root / "release.json").exists()
            else None
        )
        target_contract = release / "code/scripts/runtime_contract.json"
        require("src/matching/validation_jobs.py" not in manifest["files"]
                or target_contract.is_file(), "queue-aware release requires a runtime contract")
        live_revisions = current_revisions(root / "data/otz.db")
        # Derive the migration destination from the checked artifact itself. A stale
        # declaration must not bypass compatibility checks before `upgrade head`.
        target_heads = staged_heads(new_python, release / "code")
        if code_only:
            require(sorted(target_heads) == sorted(live_revisions),
                    "data-only mode forbids database migrations")
            state.update(backup_scope="db_media", code_only=True, protected_recovery_verified=False)
        if target_contract.is_file():
            require(
                sorted(read_json(target_contract).get("migration_heads", []))
                == sorted(target_heads),
                "runtime contract does not match staged migration heads",
            )
        # Reject a legacy runtime against a newer DB, including an attempted activation
        # downgrade. Then prove the fallback before the first stop/swap/migration.
        for revisions in (live_revisions, target_heads):
            verify_rollback_compatibility(
                release / "code", release, {"swapped": []}, revisions, manifest=manifest,
            )
        fallback_packet = getattr(args, "fallback_packet", None)
        if fallback_packet is not None:
            state.update(stage_fallback(
                fallback_packet, release, manifest["revision"], target_heads, args.uv_bin,
            ))
        else:
            verify_rollback_compatibility(root, release, state, target_heads)
        write_json(release / "activation.json", state)
        try:
            stop_units(state)
            # No scheduled job may start during swaps, migration and service restart.
            for relative in (*RUNTIME, "data/districts.yaml"):
                staged = release / "code" / relative
                require(staged.exists(), f"required runtime path missing: {relative}")
                journal_swap(root, release, state, relative, staged)
            link = release / "new-venv-link"
            link.symlink_to(release / "venv", target_is_directory=True)
            journal_swap(root, release, state, ".venv", link)
            # Files are from the verified exact-revision packet; secrets/data remain owned by otz.
            run(
                [
                    "chown",
                    "-hR",
                    "otz:otz",
                    str(release / "venv"),
                    str(root / "src"),
                    str(root / "alembic"),
                    str(root / "scripts"),
                    str(root / "deploy"),
                ]
            )
            if not code_only:
                run(
                    [str(root / ".venv/bin/alembic"), "upgrade", "head"],
                    cwd=root,
                    env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{root}/data/otz.db"},
                )
            require(current_revisions(root / "data/otz.db") == sorted(target_heads),
                    "migrated schema does not match staged migration heads")
            for unit in sorted((root / "deploy").glob("otz-*")):
                if unit.suffix not in {".service", ".timer"}:
                    continue
                target = SYSTEMD_UNITS / unit.name
                prior = release / "rollback-units" / unit.name
                prior.parent.mkdir(exist_ok=True)
                if target.exists():
                    shutil.copy2(target, prior)
                state["units"].append(unit.name)
                write_json(release / "activation.json", state)
                shutil.copy2(unit, target)
            run(["systemctl", "daemon-reload"])
            state["effective_units"] = effective_units(root / "deploy")
            restart_services(state)
            # Local responses only; no notification/canary command is run by deployment.
            state["http_readiness"] = wait_for_http_health(
                "http://127.0.0.1:8001/healthz", "http://127.0.0.1:8001/readyz",
            )
            for relative, expected in manifest["files"].items():
                if relative.split("/")[0] in RUNTIME or relative == "data/districts.yaml":
                    require(
                        hash_bytes((root / relative).read_bytes()) == expected,
                        "activated code hash mismatch",
                    )
            state.update(
                result="activated_manual_verification_pending",
                schema=current_revisions(root / "data/otz.db"),
            )
            write_json(release / "new-release.json", manifest)
            journal_swap(root, release, state, "release.json", release / "new-release.json")
            # Persistent timers may catch up immediately. Resume only after application
            # health, content hashes and the active release marker have all been verified.
            restart_timers(state)
            return state
        except Exception:
            # The rollback gate also checks durable-work semantics. Never downgrade live DB.
            restore_code(root, release)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    packet = commands.add_parser("prepare")
    packet.add_argument("--revision", required=True)
    packet.add_argument("--test-receipt", type=Path, required=True)
    packet.add_argument("--output", type=Path, required=True)
    packet.add_argument("--required-check", choices=CHECKS, action="append")
    activation = commands.add_parser("activate")
    activation.add_argument("--packet", type=Path, required=True)
    activation.add_argument("--fallback-packet", type=Path,
                            help="Separately tested queue-aware fallback for the first migration")
    activation.add_argument("--remote-root", type=Path, required=True)
    activation.add_argument("--backup-receipt", type=Path, required=True)
    activation.add_argument("--code-only-data-backup", action="store_true",
                            help="DB+media proof; no schema/config/units/dependency changes")
    activation.add_argument("--uv-bin", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--remote-root", type=Path, required=True)
    rollback.add_argument("--revision", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args)
        elif args.command == "activate":
            result = activate(args)
        else:
            require(re.fullmatch(r"[0-9a-f]{40}", args.revision), "full revision required")
            root = args.remote_root.resolve()
            require(root == Path("/opt/otz") and os.geteuid() == 0, "unsupported rollback target")
            release = root / ".releases" / args.revision
            saved = read_json(release / "activation.json")
            require(saved["result"] not in {
                "rolled_back_code_only", "rolled_back_to_compatible_fallback",
            }, "release already rolled back")
            import fcntl

            with (root / "data/state/release.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ReleaseError("another release operator holds the lock") from None
                current = (
                    read_json(root / "release.json")["revision"]
                    if (root / "release.json").exists()
                    else None
                )
                if saved.get("control_sha256"):
                    require(hash_bytes((release / "control/release.py").read_bytes())
                            == saved["control_sha256"], "saved recovery controller changed")
                require(
                    current == args.revision
                    or (
                        saved["result"] in {
                            "activating",
                            "rollback_blocked_maintenance", "rollback_blocked_incompatible",
                            "rollback_fallback_in_progress", "rollback_fallback_failed",
                        }
                        and (saved["swapped"] or saved.get("swap_intent"))
                        and "previous_revision" in saved
                        and current in {saved["previous_revision"], saved.get("fallback_revision")}
                    )
                    or (current is None and (
                        fallback_marker_gap(root, release, saved)
                        or activation_marker_gap(root, release, saved)
                    )),
                    "requested rollback is not the active release",
                )
                result = restore_code(root, release)
        print(json.dumps(result, sort_keys=True))
    except (
        ReleaseError,
        OSError,
        ValueError,
        KeyError,
        tarfile.TarError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        sqlite3.Error,
    ) as error:
        print(f"RELEASE FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
