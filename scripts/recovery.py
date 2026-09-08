#!/usr/bin/env python3
"""Consistent, private data backups and strict isolated restore drills (F07).

Never loads application settings during backup/verification. Explicit filesystem inputs
are intentional: production credentials and Telegram sessions must not enter this archive.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
FORMAT = 1
KEY_TABLES = (
    "alembic_version", "source_channels", "inbound_posts", "listings", "matches",
    "published_posts", "site_listings", "leads", "subscriptions", "subscription_matches",
    "site_events", "site_stats_daily", "daily_metrics",
)
NONEMPTY_TABLES = ("listings", "inbound_posts", "matches", "site_listings")


class RecoveryError(Exception):
    """A recovery gate failed; the backup is not accepted."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise RecoveryError(message)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def ro_db(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def schema_heads() -> list[str]:
    # ScriptDirectory reads migration declarations, not env.py or the production .env.
    from alembic.script import ScriptDirectory
    return sorted(ScriptDirectory(str(ROOT / "alembic")).get_heads())


def database_manifest(path: Path, expected: list[str] | None = None) -> dict:
    with ro_db(path) as connection:
        require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
                "database integrity_check failed")
        require(not connection.execute("PRAGMA foreign_key_check").fetchall(),
                "database foreign_key_check failed")
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        require(set(KEY_TABLES) <= tables,
                f"missing key tables: {sorted(set(KEY_TABLES) - tables)}")
        counts = {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                  for name in KEY_TABLES}
        require(all(counts[name] > 0 for name in NONEMPTY_TABLES),
                "required service tables are empty")
        revisions = sorted(row[0] for row in connection.execute(
            "SELECT version_num FROM alembic_version"))
        require(revisions == sorted(expected or schema_heads()), "unexpected schema revision")
        schema = list(connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type,name"))
        return {"revisions": revisions, "table_counts": counts,
                "schema_sha256": hashlib.sha256(json.dumps(schema).encode()).hexdigest()}


def safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    require(bool(value) and not path.is_absolute() and ".." not in path.parts
            and path.as_posix() == value, "unsafe archive member path")
    return path


def private_dir(path: Path) -> None:
    if path.exists():
        require(path.is_dir() and not path.is_symlink(), "output must be a real directory")
        require(stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
                "backup output directory must have mode 0700")
    else:
        path.mkdir(parents=True, mode=0o700)


def sensitive(path: Path) -> bool:
    return any(part == ".env" or part.startswith(".env.") or ".session" in part
               or part.lower() in {"cookies.json", "storage_state.json"}
               for part in path.parts)


def files_below(root: Path) -> list[Path]:
    require(root.is_dir() and not root.is_symlink(), "required data directory is missing/symlink")
    paths = []
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "symlinks are not allowed in recovery inputs")
        require(not sensitive(path.relative_to(root)),
                "credential/session-like file in data inputs; use protected recovery storage")
        if path.is_file():
            paths.append(path)
        else:
            require(path.is_dir(), "special files are not allowed in recovery inputs")
    return paths


def resolve_media(stored: str, manifest: dict) -> str:
    path = Path(stored)
    if not path.is_absolute():
        path = Path(manifest["source_project_root"]) / path
    require(".." not in path.parts and "://" not in stored, "invalid stored source photo path")
    for name in ("inbound", "published", "listings"):
        component = manifest["components"][name]
        try:
            relative = path.relative_to(component["source"])
        except ValueError:
            continue
        return f"{component['archive']}/{relative.as_posix()}"
    raise RecoveryError("a DB photo reference is outside declared media roots")


def check_media_references(db: Path, manifest: dict, entries: dict) -> int:
    count = 0
    with ro_db(db) as connection:
        for (raw,) in connection.execute("SELECT photo_paths FROM inbound_posts"):
            photos = json.loads(raw or "[]")
            require(isinstance(photos, list), "invalid inbound photo_paths")
            for stored in photos:
                require(isinstance(stored, str) and stored, "invalid inbound photo reference")
                require(resolve_media(stored, manifest) in entries,
                        "missing media referenced by the database")
                count += 1
    require(count > 0, "no database photo references in service backup")
    return count


class HashingReader:
    """Hash exactly the bytes tar consumes, including during a streamed backup."""

    def __init__(self, handle):
        self.handle = handle
        self.hasher = hashlib.sha256()

    def read(self, size=-1):
        data = self.handle.read(size)
        self.hasher.update(data)
        return data


def create_backup(args: argparse.Namespace) -> dict:
    output = args.output_dir.resolve() if args.output_dir else None
    if output:
        private_dir(output)
    require(not args.stream or not sys.stdout.isatty(), "binary backup stream needs redirection")
    source_db = args.db.resolve()
    require(source_db.is_file(), "source database does not exist")
    roots = {"state": args.state_dir.resolve(), "inbound": args.inbound_dir.resolve(),
             "listings": args.listings_dir.resolve(), "published": args.published_dir.resolve()}
    components, unique_roots = {}, {}
    for name, path in roots.items():
        require(not output or not output.is_relative_to(path),
                "backup output cannot be inside an input tree")
        archive = unique_roots.setdefault(path, f"data/{name}")
        components[name] = {"source": str(path), "archive": archive}
    now = datetime.now(UTC)
    name = f"otz-{now:%Y%m%d-%H%M%S}.tar.gz"
    target = output / name if output else None
    require(not target or not target.exists(), "backup filename already exists; try next second")
    with tempfile.TemporaryDirectory(prefix=".recovery-", dir=output) as temp:
        staging = Path(temp)
        db = staging / "otz.db"
        # SQLite backup includes committed WAL content without stopping the writer.
        with ro_db(source_db) as source, sqlite3.connect(db) as destination:
            source.backup(destination, pages=1024, sleep=0.1)
            destination.execute("PRAGMA journal_mode=DELETE")
        os.chmod(db, 0o600)
        database = database_manifest(db, args.expected_schema)
        manifest = {"format": FORMAT, "created_at": now.isoformat(),
                    "source_project_root": str(args.project_root.resolve()),
                    "database": database, "components": components, "files": {},
                    "protected_recovery": {
                        "config_and_sessions": "external_protected_copy_required",
                        "offhost_copy": "operator_verification_required"}}
        inputs = {"data/otz.db": db}
        for path, archive in unique_roots.items():
            for item in files_below(path):
                inputs[f"{archive}/{item.relative_to(path).as_posix()}"] = item
        # Derivative manifest lives beside the public media root, not inside it.
        photo_manifest = roots["published"].parent / "photo_manifest.json"
        if photo_manifest.exists():
            require(not photo_manifest.is_symlink(), "photo manifest must not be a symlink")
            inputs["data/photo_manifest.json"] = photo_manifest
        manifest["photo_references"] = check_media_references(db, manifest, inputs)
        for role in ("inbound", "listings", "published"):
            archive = components[role]["archive"]
            require(any(name.startswith(archive + "/") for name in inputs),
                    f"required {role} media tree is empty")
        partial = staging / name
        tar_options = ({"fileobj": sys.stdout.buffer, "mode": "w|gz"} if args.stream
                       else {"name": partial, "mode": "w:gz"})
        with tarfile.open(**tar_options) as tar:
            for relative, path in sorted(inputs.items()):
                before = path.stat()
                info = tar.gettarinfo(str(path), arcname=relative)
                info.mode, info.uid, info.gid, info.uname, info.gname = 0o600, 0, 0, "", ""
                with path.open("rb") as handle:
                    reader = HashingReader(handle)
                    tar.addfile(info, reader)
                after = path.stat()
                require((before.st_size, before.st_mtime_ns, before.st_ino) ==
                        (after.st_size, after.st_mtime_ns, after.st_ino),
                        "input changed while being archived; retry backup")
                manifest["files"][relative] = {
                    "size": after.st_size, "sha256": reader.hasher.hexdigest()}
            payload = json.dumps(manifest, indent=2, sort_keys=True).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode = len(payload), 0o600
            tar.addfile(info, io.BytesIO(payload))
        if not args.stream:
            os.chmod(partial, 0o600)
            # Verify bytes actually archived, rather than trusting a source-file checksum.
            with tempfile.TemporaryDirectory(prefix="verify-", dir=staging) as extracted:
                extract_verify(partial, Path(extracted), args.max_age_hours, args.expected_schema)
            os.replace(partial, target)
    return {"result": "stream_created_verify_destination" if args.stream else "backup_created",
            "archive": "stdout" if args.stream else str(target),
            "sha256": None if args.stream else digest(target),
            "created_at": manifest["created_at"], "database": database,
            "files": len(manifest["files"]), "photo_references": manifest["photo_references"],
            "protected_recovery": manifest["protected_recovery"]}


def extract_verify(archive: Path, destination: Path, max_age_hours: float,
                   expected_schema: list[str] | None = None) -> dict:
    require(max_age_hours > 0, "max age must be positive")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        names = [member.name for member in members]
        require(len(names) == len(set(names)), "duplicate archive entries")
        require("manifest.json" in names, "backup manifest is missing (legacy archive rejected)")
        for member in members:
            safe_relative(member.name)
            require(member.isfile(), "archive contains a link/directory/special entry")
        manifest_member = tar.getmember("manifest.json")
        require(manifest_member.size < 100 * 1024 * 1024, "oversized backup manifest")
        manifest = json.load(tar.extractfile(manifest_member))
        require(manifest["format"] == FORMAT, "unsupported backup format")
        created = datetime.fromisoformat(manifest["created_at"])
        require(created.tzinfo is not None, "backup timestamp has no timezone")
        age = (datetime.now(UTC) - created).total_seconds() / 3600
        require(-0.1 <= age <= max_age_hours, "backup is stale or dated in the future")
        require(set(manifest["files"]) == set(names) - {"manifest.json"},
                "archive inventory does not match manifest")
        require(set(manifest["components"]) == {"state", "inbound", "listings", "published"},
                "backup component declaration is incomplete")
        for component in manifest["components"].values():
            safe_relative(component["archive"])
        # No tar extraction helpers: only regular, validated paths created privately.
        for member in members:
            target = destination / member.name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("xb") as handle:
                os.chmod(target, 0o600)
                shutil.copyfileobj(tar.extractfile(member), handle)
            if member.name != "manifest.json":
                info = manifest["files"][member.name]
                require(target.stat().st_size == info["size"] and digest(target) == info["sha256"],
                        "restored file checksum/size differs from manifest")
    db = destination / "data/otz.db"
    require(db.is_file(), "backup database is missing")
    actual = database_manifest(db, expected_schema)
    require(actual == manifest["database"], "database schema/counts differ from manifest")
    for name, component in manifest["components"].items():
        root = destination / component["archive"]
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if name != "state":
            require(any(root.rglob("*")), f"required {name} media missing")
    references = check_media_references(db, manifest, manifest["files"])
    require(references == manifest["photo_references"], "photo reference count differs")
    photo_manifest = destination / "data/photo_manifest.json"
    if photo_manifest.exists():
        published = manifest["components"]["published"]["archive"]
        for original, variants in json.loads(photo_manifest.read_text()).items():
            safe_relative(original)
            require(f"{published}/{original}" in manifest["files"],
                    "photo manifest original is missing")
            for kind in ("card", "detail"):
                if variants.get(kind):
                    relative = variants[kind]
                    safe_relative(relative)
                    require(f"{published}/{relative}" in manifest["files"],
                            "photo manifest derivative is missing")
    return manifest


def application_smoke(restored: Path) -> dict:
    """Executed only in a dedicated process. Imports cannot read production credentials."""
    os.environ.clear()
    import socket

    def deny_network(*_args, **_kwargs):
        raise RecoveryError("network access is forbidden during restore drill")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    sys.path.insert(0, str(ROOT))
    import src.config as config

    manifest = json.loads((restored / "manifest.json").read_text())
    db = restored / "data/otz.db"
    # Adapt absolute source media paths in the THROWAWAY database only.
    with sqlite3.connect(db) as connection:
        posts = connection.execute("SELECT id,photo_paths FROM inbound_posts").fetchall()
        for post_id, raw in posts:
            paths = [str(restored / resolve_media(p, manifest)) for p in json.loads(raw or "[]")]
            connection.execute("UPDATE inbound_posts SET photo_paths=? WHERE id=?",
                               (json.dumps(paths), post_id))
    settings = config.Settings(
        _env_file=None, database_url=f"sqlite+aiosqlite:///{db}", data_dir=restored / "data",
        inbound_photos_dir=restored / manifest["components"]["inbound"]["archive"],
        listings_photos_dir=restored / manifest["components"]["listings"]["archive"],
        site_photos_root=restored / manifest["components"]["published"]["archive"],
        site_analytics_enabled=False, site_analytics_salt="", tg_bot_token="",
        tg_bot_recipients="", site_subscription_telegram_bot_token="",
    )
    config.get_settings = lambda: settings
    import asyncio

    import httpx
    from fastapi.staticfiles import StaticFiles
    from PIL import Image

    from src.site.app import app
    from src.site.db import dispose_engines

    app.mount("/photos", StaticFiles(directory=str(settings.site_photos_root)),
              name="restore-photos")

    async def check():
        try:
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://restore.invalid",
            ) as client:
                for route in ("/healthz", "/catalog"):
                    response = await client.get(route)
                    require(response.status_code == 200, f"restored {route} failed")
                candidates = re.findall(r'href=["\'](/apt/\d+)["\']', response.text)
                require(candidates, "restored catalog has no apartment cards")
                card = await client.get(candidates[0])
                require(card.status_code == 200, "restored apartment page failed")
                images = re.findall(r'(?:src|href)=["\'](/photos/[^"\']+)["\']', card.text)
                require(images, "restored apartment has no local photos")
                photo = await client.get(images[0])
                require(photo.status_code == 200 and photo.content, "restored photo cannot open")
                with Image.open(io.BytesIO(photo.content)) as decoded:
                    decoded.verify()
                return {"catalog": 200, "apartment": 200, "photo": 200,
                        "photo_decoded": True, "network": "disabled"}
        finally:
            await dispose_engines()

    return asyncio.run(check())


def verify_backup(args: argparse.Namespace) -> dict:
    with tempfile.TemporaryDirectory(prefix="otz-restore-") as temp:
        restored = Path(temp)
        manifest = extract_verify(args.archive.resolve(), restored, args.max_age_hours,
                                  args.expected_schema)
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                  "_smoke", str(restored)], capture_output=True, text=True,
                                 timeout=120, check=False)
        require(process.returncode == 0, "isolated application/photo smoke failed; "
                + process.stderr[-1500:])
        smoke = json.loads(process.stdout)
    return {"result": "data_restore_verified", "created_at": manifest["created_at"],
            "database": manifest["database"], "files": len(manifest["files"]),
            "photo_references": manifest["photo_references"], "application": smoke,
            "protected_recovery": manifest["protected_recovery"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    for option in ("db", "state-dir", "inbound-dir", "listings-dir", "published-dir",
                   "project-root"):
        backup.add_argument(f"--{option}", type=Path, required=True)
    output = backup.add_mutually_exclusive_group(required=True)
    output.add_argument("--output-dir", type=Path)
    output.add_argument("--stream", action="store_true",
                        help="write private tar stream to stdout; verify at destination")
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    for command in (backup, verify):
        command.add_argument("--max-age-hours", type=float, default=36)
        command.add_argument("--expected-schema", action="append",
                             help="exact approved migration revision (default: checkout head)")
    smoke = commands.add_parser("_smoke", help=argparse.SUPPRESS)
    smoke.add_argument("restored", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            result = create_backup(args)
        elif args.command == "verify":
            result = verify_backup(args)
        else:
            result = application_smoke(args.restored)
        stream = sys.stderr if getattr(args, "stream", False) else sys.stdout
        print(json.dumps(result, sort_keys=True), file=stream)
    except (RecoveryError, OSError, ValueError, KeyError, sqlite3.Error,
            tarfile.TarError, subprocess.TimeoutExpired) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
