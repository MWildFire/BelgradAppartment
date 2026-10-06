#!/usr/bin/env python3
"""Explicit DB+media snapshots and restore checks; no protected backup job or credentials.

The source command only builds a private, immutable copy. Transport is a separate command
with explicit source/destination, a shared source lease, and its own generation namespace.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import data_only_preflight as preflight
from scripts import offhost_backup as transport
from scripts import recovery

FORMAT = "renfinder-db-media-v1"


def failure_details(error):
    """Fixed failure codes and reviewed source locations, never exception text/values."""
    recovery_codes = {
        "backup output directory must have mode 0700": "nonprivate_output_directory",
        "database integrity_check failed": "database_integrity_failed",
        "database foreign_key_check failed": "database_foreign_key_failed",
        "unexpected schema revision": "unexpected_schema",
        "required service tables are empty": "empty_service_table",
        "credential/session-like file in data inputs; use protected recovery storage":
            "sensitive_media_entry",
        "symlinks are not allowed in recovery inputs": "symlink_media_entry",
    }
    if isinstance(error, preflight.PreflightError):
        code = str(error)
    elif isinstance(error, recovery.RecoveryError):
        code = recovery_codes.get(str(error), "recovery_gate_failed")
    else:
        code = {PermissionError: "permission_denied", FileNotFoundError: "missing_file",
                ValueError: "invalid_value", TypeError: "invalid_type"}.get(
                    type(error), "data_only_backup_failed")
    result = {"result": "blocked", "reason": code}
    frame = error.__traceback__
    while frame is not None:
        filename = Path(frame.tb_frame.f_code.co_filename).name
        if filename in {"data_only_backup.py", "data_only_preflight.py", "recovery.py"}:
            result["source_module"] = filename
            result["source_line"] = frame.tb_lineno
        frame = frame.tb_next
    return result


def require(condition, code):
    preflight.require(condition, code)


def progress(phase, completed, total):
    print(json.dumps({"phase": phase, "completed": completed, "total": total}), flush=True)


def allowed_member(name):
    path = Path(name)
    return (name == "data/otz.db" or name == "data/photo_manifest.json" or (
        len(path.parts) >= 3 and path.parts[:2] in {("data", "inbound"), ("data", "listings")}
        and preflight.allowed_image(path)
    )) and path.as_posix() == name


def check_manifest(manifest):
    require(manifest.get("format") == FORMAT and manifest.get("scope") == "db_media",
            "unsupported_snapshot_scope")
    require(manifest.get("protected_recovery_verified") is False, "unexpected_protected_scope")
    require(manifest.get("credential_inspection", {}).get("result") == "heuristic_scan_passed",
            "credential_scan_unconfirmed")
    require(set(manifest.get("components", {})) == {"inbound", "listings", "published"},
            "invalid_components")
    for role, component in manifest["components"].items():
        expected = "data/inbound" if role == "published" else "data/" + role
        require(component.get("archive") == expected, "invalid_components")
    files = manifest.get("files")
    require(isinstance(files, dict) and "data/otz.db" in files, "invalid_file_inventory")
    require(all(allowed_member(name) for name in files), "forbidden_snapshot_member")
    for metadata in files.values():
        require(type(metadata.get("size")) is int and metadata["size"] >= 0
                and re.fullmatch(r"[a-f0-9]{64}", metadata.get("sha256", "")),
                "invalid_file_metadata")
    created = datetime.fromisoformat(manifest["created_at"])
    require(created.tzinfo is not None
            and 0 <= (datetime.now(UTC) - created).total_seconds() <= 36 * 3600,
            "stale_snapshot")


def create_snapshot(args):
    output = args.output_dir.absolute()
    require(output.resolve() == output and not output.exists(), "invalid_output_path")
    recovery.private_dir(output.parent)
    with tempfile.TemporaryDirectory(prefix=".db-media-build-", dir=output.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        options = copy.copy(args)
        options.output_dir = stage
        preflight.prepare(options)
        initial = json.loads((stage / "manifest.json").read_text())
        required_bytes = sum(row["size"] for row in initial["media"].values())
        require(shutil.disk_usage(stage).free >= required_bytes + args.min_free_gib * 1024**3,
                "insufficient_snapshot_space")
        files = {"data/otz.db": initial["database_file"]}
        for index, (name, expected) in enumerate(sorted(initial["media"].items()), 1):
            require(allowed_member(name), "forbidden_snapshot_member")
            source = args.project_root.absolute() / name
            require(source.resolve() == source and source.is_file(), "media_path_changed")
            with source.open("rb") as stream:
                raw = stream.read(preflight.MAX_IMAGE_BYTES + 1)
            require(len(raw) == expected["size"]
                    and hashlib.sha256(raw).hexdigest() == expected["sha256"], "media_changed")
            # Only bytes already inspected/decoded are copied into private snapshot storage.
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                target.chmod(0o600)
                stream.write(raw)
            files[name] = {key: expected[key] for key in ("size", "sha256")}
            if (getattr(args, "progress", None)
                    and (index % 1000 == 0 or index == len(initial["media"]))):
                args.progress("copy_media", index, len(initial["media"]))
        photos = {}
        for name, metadata in initial["media"].items():
            if (not name.startswith("data/inbound/")
                    or name.endswith((".card.webp", ".detail.webp"))):
                continue
            relative = name.removeprefix("data/inbound/")
            entry = {"w": metadata["width"], "h": metadata["height"]}
            for kind in ("card", "detail"):
                if name + f".{kind}.webp" in files:
                    entry[kind] = relative + f".{kind}.webp"
            photos[relative] = entry
        index_path = stage / "data/photo_manifest.json"
        index_path.write_text(json.dumps(photos, sort_keys=True))
        index_path.chmod(0o600)
        files["data/photo_manifest.json"] = {
            "size": index_path.stat().st_size, "sha256": recovery.digest(index_path),
        }
        root = args.project_root.absolute()
        release = root / "release.json"
        source_revision = json.loads(release.read_text())["revision"] if release.is_file() else None
        manifest = {
            "format": FORMAT, "scope": "db_media", "created_at": initial["created_at"],
            "source_project_root": str(root), "source_revision": source_revision,
            "database": initial["database"],
            "credential_inspection": initial["credential_inspection"],
            "components": {role: {"source": str(root / ("data/inbound" if role == "published"
                                                         else "data/" + role)),
                                  "archive": ("data/inbound" if role == "published"
                                              else "data/" + role)}
                           for role in ("inbound", "listings", "published")},
            "files": files, "protected_recovery_verified": False,
            "protected_recovery": {"config_and_sessions": "excluded_by_scope"},
            "source_snapshot_immutable": True, "personal_data_present": True,
        }
        manifest["photo_references"] = recovery.check_media_references(
            stage / "data/otz.db", manifest, files,
        )
        check_manifest(manifest)
        transport.atomic_json(stage / "manifest.json", manifest)
        stage.rename(output)
    return {"result": "data_only_source_snapshot_created", "files": len(files),
            "manifest_sha256": recovery.digest(output / "manifest.json")}


def verify_snapshot(folder, *, application=False, report_progress=None):
    require(folder.is_dir() and not folder.is_symlink(), "invalid_snapshot_directory")
    manifest = json.loads((folder / "manifest.json").read_text())
    check_manifest(manifest)
    actual = {path.relative_to(folder).as_posix() for path in recovery.files_below(folder)}
    require(actual == set(manifest["files"]) | {"manifest.json"}, "snapshot_inventory_differs")
    decoded = 0
    for index, (name, expected) in enumerate(manifest["files"].items(), 1):
        path = folder / name
        require(path.resolve() == folder.resolve() / name, "symlink_snapshot_member")
        require(path.stat().st_size == expected["size"]
                and recovery.digest(path) == expected["sha256"], "snapshot_checksum_differs")
        if name not in {"data/otz.db", "data/photo_manifest.json"}:
            preflight.inspect_image(path)
            decoded += 1
        if report_progress and (index % 1000 == 0 or index == len(manifest["files"])):
            report_progress("verify_media", index, len(manifest["files"]))
    require(recovery.database_manifest(folder / "data/otz.db") == manifest["database"],
            "snapshot_database_differs")
    preflight.inspect_database(folder / "data/otz.db")
    require(recovery.check_media_references(folder / "data/otz.db", manifest, manifest["files"])
            == manifest["photo_references"], "snapshot_references_differ")
    # The generated index admits only dimensions and existing local media variants.
    for name, item in json.loads((folder / "data/photo_manifest.json").read_text()).items():
        require("data/inbound/" + name in manifest["files"]
                and set(item) <= {"w", "h", "card", "detail"}, "invalid_photo_index")
        for key in ("w", "h"):
            require(type(item.get(key)) is int and item[key] > 0, "invalid_photo_index")
        for key in ("card", "detail"):
            if key in item:
                require("data/inbound/" + item[key] in manifest["files"], "invalid_photo_index")
    result = (recovery.verify_application(folder, manifest) if application else
              {"result": "data_only_snapshot_verified"})
    return result | {
        "backup_scope": "db_media", "source_revision": manifest["source_revision"],
        "credential_inspection": manifest["credential_inspection"],
        "media_decode_verified": True, "media_files_verified": decoded,
        "source_snapshot_immutable": True, "protected_recovery_verified": False,
        "health_receipt_published": False, "personal_data_present": True,
    }


def receive(args):
    """One explicit transfer into its own namespace; never invoke the protected job."""
    root = args.private_root.absolute()
    require(root.resolve() == root and root.name == "data-only", "separate_namespace_required")
    require(not any((root / name).exists() for name in ("protected", "protected-pending")),
            "protected_namespace_forbidden")
    require(args.remote_root == Path("/opt/otz"), "unsupported_source_root")
    require(re.fullmatch(r"/var/tmp/otz-data-only/[0-9]{8}-[0-9]{6}-[a-f0-9]{8}",
                         str(args.source_snapshot)), "invalid_source_snapshot_path")
    require(re.fullmatch(r"/tmp/otz-dbmedia-tools-[a-f0-9]{8}/scripts/data_only_backup.py",
                         str(args.remote_helper)), "invalid_remote_helper_path")
    recovery.private_dir(root)
    for name in ("pending", "generations", "receipts"):
        recovery.private_dir(root / name)
    scope = root / "scope.json"
    if scope.exists():
        require(json.loads(scope.read_text()) == {"scope": "db_media", "format": FORMAT},
                "namespace_scope_mismatch")
    else:
        require(not any((root / "generations").iterdir()), "unbound_generation_namespace")
        transport.atomic_json(scope, {"scope": "db_media", "format": FORMAT})
    generation = args.source_snapshot.name
    pending = root / "pending" / generation
    destination = root / "generations" / generation
    require(not pending.exists() and not destination.exists(), "generation_exists")
    pending.mkdir(mode=0o700)
    ssh_transport = shlex.join(transport.ssh(args)[:-1])
    rsync_prefix = [
        "/usr/bin/rsync", "-rt", "--rsync-path",
        shlex.join([*transport.prefix(args), "/usr/bin/rsync"]), "-e", ssh_transport,
    ]
    with transport.remote_lease(args):
        # Frozen source snapshot is completed (including credential gate) before egress.
        transport.remote(args, [args.remote_python, str(args.remote_helper), "snapshot",
                               "--project-root", str(args.remote_root), "--output-dir",
                               str(args.source_snapshot), "--application-project-root",
                               str(args.remote_root), "--min-free-gib", str(args.min_free_gib),
                               "--status-file", str(args.source_snapshot) + ".status.json"])
        transport.run([*rsync_prefix, f"{args.ssh_target}:{args.source_snapshot}/manifest.json",
                       str(pending / "manifest.json")])
        manifest = json.loads((pending / "manifest.json").read_text())
        check_manifest(manifest)
        require(manifest["source_project_root"] == str(args.remote_root), "wrong_snapshot_source")
        require(manifest.get("source_snapshot_immutable") is True, "source_copy_not_frozen")
        needed = sum(entry["size"] for entry in manifest["files"].values())
        require(shutil.disk_usage(root).free >= needed + args.min_free_gib * 1024**3,
                "insufficient_receiver_space")
        seed = args.seed_dir.resolve() if args.seed_dir else None
        seed_files = json.loads((seed / "manifest.json").read_text())["files"] if seed else {}
        changed = []
        linked = 0
        for name, metadata in manifest["files"].items():
            target = pending / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            original = seed / name if seed else None
            # Reuse only declared media, never the old database, state, indexes or sessions.
            if (original is not None and name.startswith(("data/inbound/", "data/listings/"))
                    and seed_files.get(name) == metadata and original.is_file()
                    and original.resolve() == original
                    and original.stat().st_dev == root.stat().st_dev):
                os.link(original, target)
                linked += 1
            else:
                changed.append(name)
        listing = root / "pending" / (generation + ".files")
        listing.write_bytes(b"\0".join(name.encode() for name in changed) + b"\0")
        listing.chmod(0o600)
        try:
            transport.run([*rsync_prefix, "-I", "-0", "--files-from", str(listing),
                           f"{args.ssh_target}:{args.source_snapshot}/", str(pending) + "/"])
        finally:
            listing.unlink(missing_ok=True)
        transport.assert_lease()
        result = verify_snapshot(pending, application=True, report_progress=progress)
        transport.assert_lease()
        result.update(
            backup_format="data_only_directory_generation", generation_id=generation,
            manifest_sha256=result["generation_manifest_sha256"], offhost_verified=True,
            media_reused_from_verified_seed=linked, files_transferred=len(changed),
        )
        pending.rename(destination)
        transport.atomic_json(root / "receipts" / (generation + ".json"), result)
        # No retention deletion and no production health/protected receipt publication.
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--project-root", type=Path, required=True)
    snapshot.add_argument("--output-dir", type=Path, required=True)
    snapshot.add_argument("--min-free-gib", type=float, default=20)
    verify = commands.add_parser("verify")
    verify.add_argument("directory", type=Path)
    verify.add_argument("--receipt", type=Path, required=True)
    receive_command = commands.add_parser("receive")
    for option in ("private-root", "source-snapshot", "remote-root", "remote-helper", "ssh-key"):
        receive_command.add_argument("--" + option, type=Path, required=True)
    for option in ("ssh-target", "remote-python"):
        receive_command.add_argument("--" + option, required=True)
    receive_command.add_argument("--remote-user", default="otz", choices=["otz"])
    receive_command.add_argument("--seed-dir", type=Path)
    receive_command.add_argument("--min-free-gib", type=float, default=20)
    for command in (snapshot, verify, receive_command):
        command.add_argument("--application-project-root", type=Path)
        command.add_argument("--status-file", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.application_project_root:
        recovery.ROOT = args.application_project_root.resolve()
    if args.status_file:
        def status(phase, completed, total):
            transport.atomic_json(args.status_file, {
                "at_utc": datetime.now(UTC).isoformat(), "phase": phase,
                "completed": completed, "total": total,
            })
            progress(phase, completed, total)
    else:
        status = progress
    try:
        if args.status_file:
            recovery.private_dir(args.status_file.parent)
        if args.command == "snapshot":
            args.progress = status
            status("snapshot_database_and_inspection", 0, 1)
            result = create_snapshot(args)
        elif args.command == "verify":
            result = verify_snapshot(args.directory, application=True, report_progress=status)
            transport.atomic_json(args.receipt, result)
        else:
            result = receive(args)
    except Exception as error:
        result = failure_details(error)
        if args.status_file:
            transport.atomic_json(args.status_file, result)
        print(json.dumps(result), flush=True)
        return 1
    if args.status_file:
        transport.atomic_json(args.status_file, result)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
