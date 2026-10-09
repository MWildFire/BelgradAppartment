#!/usr/bin/env python3
"""Upload an already verified packet to an explicit SSH host; optionally activate it."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from release import CHECKS, ReleaseError, require, unpack_packet, validate_backup, validate_tests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="explicit user@host; no legacy default")
    parser.add_argument("--remote-root", required=True, choices=["/opt/otz"])
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--fallback-packet", type=Path,
                        help="separately tested compatible runtime for first schema transition")
    parser.add_argument("--backup-receipt", type=Path, required=True)
    parser.add_argument("--uv-bin", required=True, help="absolute remote pinned uv binary")
    parser.add_argument("--ssh-key", type=Path)
    parser.add_argument(
        "--activate", action="store_true", help="apply staged release through gates"
    )
    parser.add_argument("--sudo", action="store_true", help="use sudo for remote activation")
    parser.add_argument("--code-only-data-backup", action="store_true",
                        help="Explicit same-schema/config/dependency release with DB+media proof")
    parser.add_argument("--acknowledge-sanitized-url-loss", action="store_true",
                        help="With code-only mode: reviewed v1 loss of 43 URLs in backup")
    args = parser.parse_args()
    require(re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+", args.target), "invalid SSH target")
    require(Path(args.uv_bin).is_absolute(), "absolute uv binary path required")
    validate_backup(json.loads(args.backup_receipt.read_text()),
                    allow_data_only=args.code_only_data_backup,
                    allow_sanitized=args.acknowledge_sanitized_url_loss)
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
    if args.ssh_key:
        options.extend(["-i", str(args.ssh_key)])
    with tempfile.TemporaryDirectory(prefix="otz-release-check-") as temp:
        local = Path(temp)
        manifest = unpack_packet(args.packet, local)
        validate_tests(manifest["tests"], manifest["revision"], CHECKS)
        fallback = None
        if args.fallback_packet:
            fallback = unpack_packet(args.fallback_packet, local / "fallback-check")
            validate_tests(fallback["tests"], fallback["revision"], CHECKS)
            require(fallback["revision"] != manifest["revision"],
                    "fallback must be a distinct revision")
        staging = subprocess.run(
            ["ssh", *options, args.target, "umask 077 && mktemp -d /tmp/otz-release.XXXXXXXX"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        require(
            re.fullmatch(r"/tmp/otz-release\.[A-Za-z0-9]+", staging), "invalid remote staging path"
        )
        uploads = [
            (args.packet, "packet.tar.gz"),
            (args.backup_receipt, "backup.json"),
            (local / "code/scripts/release.py", "release.py"),
        ]
        if args.fallback_packet:
            uploads.append((args.fallback_packet, "fallback.tar.gz"))
        for source, target in uploads:
            subprocess.run(
                ["scp", *options, str(source), f"{args.target}:{staging}/{target}"], check=True
            )
        command = (["sudo", "--"] if args.sudo else []) + [
            "/usr/bin/python3",
            staging + "/release.py",
            "activate",
            "--packet",
            staging + "/packet.tar.gz",
            "--backup-receipt",
            staging + "/backup.json",
            "--remote-root",
            args.remote_root,
            "--uv-bin",
            args.uv_bin,
        ]
        if args.fallback_packet:
            command.extend(["--fallback-packet", staging + "/fallback.tar.gz"])
        if args.code_only_data_backup:
            command.append("--code-only-data-backup")
        if args.acknowledge_sanitized_url_loss:
            command.append("--acknowledge-sanitized-url-loss")
        if args.activate:
            subprocess.run(["ssh", *options, args.target, shlex.join(command)], check=True)
        print(
            json.dumps(
                {
                    "revision": manifest["revision"],
                    "fallback_revision": fallback["revision"] if fallback else None,
                    "target": args.target,
                    "staging": staging,
                    "result": "activation_finished" if args.activate else "staged_not_activated",
                    "activation_command": shlex.join(command),
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"DEPLOY FAILED: {error}") from None
