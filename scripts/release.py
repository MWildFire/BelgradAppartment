#!/usr/bin/env python3
"""Exact-revision, receipt-gated staged releases. Never restores/downgrades live data."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
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
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ("src", "alembic", "scripts", "deploy", "pyproject.toml", "uv.lock", "alembic.ini")
SERVICES = ("otz-monitor", "otz-site", "otz-admin", "otz-subscription-bot")
JOBS = ("otz-nightly-refresh", "otz-weekly-full-refresh", "otz-scrape-smoke", "otz-daily-summary")
TIMERS = tuple(f"{name}.timer" for name in JOBS)
CHECKS = ("unit", "changed_lint", "shell", "browser")
MAINTENANCE_PATTERN = (
    r"(src\.cli |/otz |^otz )(nightly-refresh|weekly-refresh|scrape-smoke|scrape|"
    r"refresh-broken|rolling-refresh|verify-links|daily-summary)( |$)"
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


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    Path(path).chmod(0o600)


def validate_tests(receipt, revision, checks):
    require(receipt.get("revision") == revision, "tests do not cover the exact release revision")
    require(receipt.get("result") == "passed", "test receipt is not passed")
    require(
        all(receipt.get("checks", {}).get(check) == "passed" for check in checks),
        "required checks missing or not passed",
    )


def validate_backup(receipt, now=None):
    require(receipt.get("result") == "data_restore_verified", "backup restore is not verified")
    created = datetime.fromisoformat(receipt["created_at"])
    require(created.tzinfo is not None, "backup timestamp missing timezone")
    age = ((now or datetime.now(UTC)) - created).total_seconds() / 3600
    require(-0.1 <= age <= 36, "release requires a backup no older than 36 hours")
    require(
        re.fullmatch(r"[0-9a-f]{64}", receipt.get("archive_sha256", "")),
        "verified archive SHA-256 missing",
    )
    require(receipt.get("offhost_verified") is True, "off-host backup is not verified")
    require(
        receipt.get("protected_recovery_verified") is True,
        "protected configuration/session recovery is not verified",
    )
    application = receipt.get("application", {})
    require(
        all(application.get(name) == 200 for name in ("catalog", "apartment", "photo"))
        and application.get("photo_decoded") is True,
        "application/photo restore gate missing",
    )
    require(receipt.get("database", {}).get("revisions"), "backup schema revision missing")


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
        for member in members:
            content = tar.extractfile(member).read()
            if member.name != "release.json":
                require(
                    hash_bytes(content) == manifest["files"][member.name.removeprefix("code/")],
                    "packet hash mismatch",
                )
            target = destination / member.name
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
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
    rejected = release / "rejected"
    rejected.mkdir(exist_ok=True)
    for relative in reversed(state["swapped"]):
        target = root / relative
        if target.exists() or target.is_symlink():
            destination = rejected / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, destination)
        prior = release / "rollback" / relative
        if prior.exists() or prior.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prior, target)
    for name in state["units"]:
        prior = release / "rollback-units" / name
        target = Path("/etc/systemd/system") / name
        if prior.exists():
            shutil.copy2(prior, target)
        elif target.exists():
            target.unlink()
    run(["systemctl", "daemon-reload"])
    state["result"] = "rolled_back_code_only"
    write_json(release / "activation.json", state)
    restart_units(state)
    return state


def activate(args):
    import fcntl

    root = args.remote_root.resolve()
    require(root == Path("/opt/otz"), "canonical units target /opt/otz; different root unsupported")
    require(os.geteuid() == 0, "activation requires root for coherent systemd update")
    require(args.uv_bin.is_absolute() and args.uv_bin.is_file(), "explicit uv binary unavailable")
    backup = read_json(args.backup_receipt)
    validate_backup(backup)
    with (root / "data/state/release.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReleaseError("another release operator holds the lock") from None
        with tempfile.TemporaryDirectory(prefix="otz-packet-") as temporary:
            unpacked = Path(temporary)
            manifest = unpack_packet(args.packet, unpacked)
            require(
                current_revisions(root / "data/otz.db") == sorted(backup["database"]["revisions"]),
                "live schema no longer matches the verified backup",
            )
            release = root / ".releases" / manifest["revision"]
            require(
                not release.exists(), "release already staged; inspect existing activation first"
            )
            release.mkdir(parents=True, mode=0o750)
            release.parent.chmod(0o750)
            run(["chown", "root:otz", str(release.parent), str(release)])
            shutil.copytree(unpacked / "code", release / "code")
            write_json(release / "release.json", manifest)
            write_json(release / "backup-receipt.json", backup)
        env = os.environ.copy()
        env["UV_PROJECT_ENVIRONMENT"] = str(release / "venv")
        # New environment only: failure cannot break the currently running services.
        run(
            [
                str(args.uv_bin),
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
        state = service_state()
        state.update(revision=manifest["revision"], result="activating", swapped=[], units=[])
        state["previous_revision"] = (
            read_json(root / "release.json")["revision"]
            if (root / "release.json").exists()
            else None
        )
        write_json(release / "activation.json", state)
        try:
            stop_units(state)
            # No scheduled job may start during swaps, migration and service restart.
            for relative in (*RUNTIME, "data/districts.yaml"):
                staged = release / "code" / relative
                require(staged.exists(), f"required runtime path missing: {relative}")
                swap_path(staged, root / relative, release / "rollback" / relative)
                state["swapped"].append(relative)
                write_json(release / "activation.json", state)
            link = release / "new-venv-link"
            link.symlink_to(release / "venv", target_is_directory=True)
            swap_path(link, root / ".venv", release / "rollback/.venv")
            state["swapped"].append(".venv")
            write_json(release / "activation.json", state)
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
            run(
                [str(root / ".venv/bin/alembic"), "upgrade", "head"],
                cwd=root,
                env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{root}/data/otz.db"},
            )
            current_revisions(root / "data/otz.db")
            for unit in sorted((root / "deploy").glob("otz-*")):
                if unit.suffix not in {".service", ".timer"}:
                    continue
                target = Path("/etc/systemd/system") / unit.name
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
            run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--output",
                    "/dev/null",
                    "--retry",
                    "5",
                    "--retry-connrefused",
                    "--retry-delay",
                    "1",
                    "http://127.0.0.1:8001/healthz",
                ]
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
            swap_path(
                release / "new-release.json",
                root / "release.json",
                release / "rollback/release.json",
            )
            state["swapped"].append("release.json")
            write_json(release / "activation.json", state)
            # Persistent timers may catch up immediately. Resume only after application
            # health, content hashes and the active release marker have all been verified.
            restart_timers(state)
            return state
        except Exception:
            # App rollback is safe with additive schema. Never downgrade/restore the live DB.
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
    activation.add_argument("--remote-root", type=Path, required=True)
    activation.add_argument("--backup-receipt", type=Path, required=True)
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
            require(saved["result"] != "rolled_back_code_only", "release already rolled back")
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
                require(
                    current == args.revision
                    or (
                        saved["result"] == "rollback_blocked_maintenance"
                        and saved["swapped"]
                        and "previous_revision" in saved
                        and current == saved["previous_revision"]
                    ),
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
