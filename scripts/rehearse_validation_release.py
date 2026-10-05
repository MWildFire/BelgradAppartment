"""Synthetic-only migration, SQLite backup/restore and code-rollback rehearsal.

Accepts a NEW output directory, never an existing DB, endpoint or credentials.
All service operations are intercepted. The receipt cannot authorize production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

from alembic.config import Config
from PIL import Image

from alembic import command

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import release  # noqa: E402

TABLES = (
    "match_deliveries",
    "inbound_posts",
    "validation_jobs",
    "digest_work_items",
    "listing_identity_edges",
    "identity_events",
    "apartment_identities",
    "source_channels",
)


def config(database: Path) -> Config:
    # No fileConfig: this utility must not disable loggers in an embedding/test process.
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")
    return cfg


def backup(source: Path, destination: Path) -> None:
    # Online SQLite backup includes committed WAL frames, unlike copying just otz.db.
    with sqlite3.connect(source) as reader, sqlite3.connect(destination) as writer:
        reader.backup(writer)


def snapshot(database: Path) -> dict:
    with sqlite3.connect(database) as con:
        assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        return {
            table: con.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall() for table in TABLES
        }


def seed(database: Path) -> None:
    with sqlite3.connect(database) as con:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("""INSERT INTO source_channels(id,tg_id,is_active,added_at,
            last_processed_message_id) VALUES(1,-12345,1,'2026-10-05',40)""")
        for n in (1, 2):
            con.execute(
                """INSERT INTO inbound_posts(id,source_channel_id,tg_message_id,
                raw_text,photo_paths,photo_phashes,received_at,status)
                VALUES(?,1,?,'synthetic 600€ 45m²','[]','[]','2026-10-05',?)""",
                (n, n, "MATCHED" if n == 1 else "NEW"),
            )
            con.execute(
                """INSERT INTO listings(id,platform,external_id,url,photo_urls,
                photo_phashes,scraped_at,last_seen_at,is_active,is_primary,absence_streak)
                VALUES(?,'NEKRETNINE',?,'https://example.test','[]','[]',
                '2026-10-05','2026-10-05',1,1,0)""",
                (n, f"synthetic-{n}"),
            )
        con.execute("""INSERT INTO matches(id,inbound_post_id,listing_id,confidence,
            matched_photo_count,method,created_at,status)
            VALUES(1,1,1,.99,2,'phash','2026-10-05','PUBLISHED')""")
        for n, state in ((1, "sent"), (2, "unknown")):
            con.execute(
                """INSERT INTO match_deliveries(id,match_id,recipient_id,status,
                message_ids,attempts,created_at,updated_at)
                VALUES(?,1,?,?,'[17]',1,'2026-10-05','2026-10-05')""",
                (n, 100 + n, state),
            )


def rehearse(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    app = output / "app"
    data = app / "data"
    data.mkdir(parents=True)
    database = data / "otz.db"
    photo = data / "synthetic.jpg"
    Image.new("RGB", (64, 48), "beige").save(photo)
    photo_hash = hashlib.sha256(photo.read_bytes()).hexdigest()
    cfg = config(database)
    command.upgrade(cfg, "0014_group_availability_index")
    seed(database)
    # Keep a WAL connection open during backup: proves the snapshot includes its commit.
    with sqlite3.connect(database) as wal:
        assert wal.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        wal.execute("UPDATE source_channels SET last_processed_message_id=41")
        wal.commit()
        backup(database, output / "before-migration.db")
    with sqlite3.connect(output / "before-migration.db") as con:
        assert (
            con.execute("SELECT last_processed_message_id FROM source_channels").fetchone()[0] == 41
        )
        old_deliveries = con.execute("SELECT * FROM match_deliveries ORDER BY id").fetchall()
    command.upgrade(cfg, "head")
    with sqlite3.connect(database) as con:
        con.execute("PRAGMA foreign_keys=ON")
        assert (
            con.execute("SELECT * FROM match_deliveries ORDER BY id").fetchall() == old_deliveries
        )
        con.execute("""INSERT INTO validation_jobs(inbound_id,state,reason_code,attempts,
            outcomes,updated_at) VALUES(2,'pending','provider_unavailable',2,'[]','2026-10-05')""")
        con.execute("""INSERT INTO digest_work_items(id,source_id,message_id,share_id,payload,
            post_date,state,attempts,reason_code,lease_token,lease_until)
            VALUES(1,1,41,'synthetic-share','{}','2026-10-05','processing',1,'',
                   'synthetic-lease','2026-10-06')""")
        con.execute("""INSERT INTO apartment_identities(id,anchor_listing_id,created_at)
            VALUES(1,1,'2026-10-05')""")
        con.execute("""INSERT INTO listing_identity_edges
            (left_id,right_id,state,evidence,created_at)
            VALUES(1,2,'confirmed','{"synthetic":true}','2026-10-05')""")
        con.execute("""INSERT INTO identity_events(id,kind,payload,created_at)
            VALUES(1,'confirm','{"synthetic":true}','2026-10-05')""")
    expected = snapshot(database)
    backup(database, output / "after-migration.db")
    restored = output / "restored"
    restored.mkdir()
    backup(output / "after-migration.db", restored / "otz.db")
    shutil.copy2(photo, restored / photo.name)
    assert snapshot(restored / "otz.db") == expected
    assert hashlib.sha256((restored / photo.name).read_bytes()).hexdigest() == photo_hash
    with Image.open(restored / photo.name) as image:
        image.load()
        assert image.size == (64, 48)
    try:
        command.downgrade(cfg, "0014_group_availability_index")
    except RuntimeError as error:
        assert "preserve queued work" in str(error)
    else:
        raise AssertionError("Destructive migration downgrade unexpectedly allowed")
    assert snapshot(database) == expected

    # A real code swap and restore on isolated paths, current queue-aware code in both
    # synthetic builds. No claim is made that the old production runtime is compatible.
    shutil.copytree(ROOT / "src", app / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (app / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/runtime_contract.json", app / "scripts/runtime_contract.json")
    manifest = {
        "revision": "synthetic-current-worktree",
        "files": {
            str(path.relative_to(app)): release.hash_bytes(path.read_bytes())
            for parent in (app / "src", app / "scripts")
            for path in parent.rglob("*")
            if path.is_file()
        },
    }
    release.write_json(app / "release.json", manifest)
    staged = output / "release"
    shutil.copytree(app / "src", staged / "code/src")
    (staged / "code/src/new-release-marker.txt").write_text("synthetic newer build")
    release.swap_path(staged / "code/src", app / "src", staged / "rollback/src")
    state = {"swapped": ["src"], "units": [], "services": [], "timers": []}
    release.write_json(staged / "activation.json", state)
    original_contract = (app / "scripts/runtime_contract.json").read_bytes()
    (app / "scripts/runtime_contract.json").unlink()
    with (
        patch.object(release, "run", return_value=""),
        patch.object(release, "active", return_value=False),
        patch.object(release, "drain_maintenance", return_value=None),
    ):
        try:
            release.restore_code(app, staged)
        except release.ReleaseError:
            pass
        else:
            raise AssertionError("Incompatible rollback was not blocked")
        assert (app / "src/new-release-marker.txt").exists()
        assert snapshot(database) == expected
        (app / "scripts/runtime_contract.json").write_bytes(original_contract)
        result = release.restore_code(app, staged)
    assert result["result"] == "rolled_back_code_only"
    assert not (app / "src/new-release-marker.txt").exists()
    assert snapshot(database) == expected
    assert hashlib.sha256(photo.read_bytes()).hexdigest() == photo_hash
    receipt = {
        "result": "synthetic_rehearsal_passed",
        "production_authorization": False,
        "actual_external_calls": 0,
        "actual_system_commands": 0,
        "database_revisions": release.current_revisions(database),
        "preserved_rows": {table: len(rows) for table, rows in expected.items()},
        "delivery_states": ["sent", "unknown"],
        "photo_sha256": photo_hash,
        "checks": [
            "committed_wal_backup",
            "migration_0014_to_head",
            "sqlite_integrity_fk",
            "restore_logical_equality",
            "restored_photo_decode",
            "downgrade_denied",
            "incompatible_rollback_denied",
            "compatible_code_rollback_keeps_new_work",
        ],
        "limitations": [
            "synthetic only",
            "not off-host",
            "no production configuration/session",
            "no HTTP restore",
            "both compatible builds use current worktree code",
        ],
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rehearse(args.output.resolve()), indent=2))
