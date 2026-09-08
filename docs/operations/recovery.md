# Verified recovery (F07, 8 September 2026)

`scripts/recovery.py` creates a consistent online SQLite snapshot, reads the complete
specified state/source/published media trees, and records migration revision, schema
hash, key table counts, per-file SHA-256/size, UTC creation time and DB photo-reference
count. The original DB is opened read-only; committed WAL content is included through
SQLite's backup API. Media files must stay unchanged while each is archived; a changing
file fails the backup so the operator can retry. Do not run concurrent media deletion
while creating a recovery copy.

The tool does not import production settings for backup/validation. Its inputs are
explicit paths. `.env`, Telegram session files, browser cookies/profile state must be
stored separately in existing protected/encrypted recovery storage. A data-restore
PASS does **not** claim that protected credentials, off-host storage or automatic backup
cadence have been verified. Those remain explicit fields in its result and operator gates.

## Local private archive

Run with the application's existing Python environment and an output directory owned
by the operator with mode 0700. The helper writes mode 0600 archives and rejects shared
output directories. All required media directories must exist and be nonempty; an empty
state directory is permitted and recorded. Do not put the output under an input tree.

```bash
/opt/otz/.venv/bin/python /opt/otz/scripts/recovery.py backup \
  --db /opt/otz/data/otz.db \
  --state-dir /opt/otz/data/state \
  --inbound-dir /opt/otz/data/inbound \
  --listings-dir /opt/otz/data/listings \
  --published-dir /opt/otz/data/inbound \
  --project-root /opt/otz \
  --output-dir /opt/otz/data/backups/verified
```

Use the live `SITE_PHOTOS_ROOT` for `--published-dir`. Identical inbound/published roots
are archived once. `photo_manifest.json` beside that root is also included, when present;
every referenced derivative must be present. All `inbound_posts.photo_paths` must resolve
to included files. No photographs are silently dropped to fit available disk space.

## Stream to an existing off-host destination

A full archive can exceed free space on the production disk. `--stream` holds only a
small SQLite snapshot in a private temporary directory and writes compressed tar bytes
to stdout; status is JSON on stderr. Do not send stdout to a terminal. The receiver must
use a private directory, `umask 077`, a `.partial` name, and check SSH's exit status.
Only a completed archive that passes a destination restore drill can become an accepted
backup. A disconnected stream remains partial and cannot pass inventory/hash checks.

Example (replace the approved SSH target/options and paths with the live environment):

```bash
umask 077
ssh "$APP_SSH_TARGET" \
  '/opt/otz/.venv/bin/python /opt/otz/scripts/recovery.py backup --stream --db /opt/otz/data/otz.db --state-dir /opt/otz/data/state --inbound-dir /opt/otz/data/inbound --listings-dir /opt/otz/data/listings --published-dir /opt/otz/data/inbound --project-root /opt/otz' \
  > "$PRIVATE_BACKUP_DIR/otz-current.tar.gz.partial"
"$APP_PYTHON" scripts/recovery.py verify "$PRIVATE_BACKUP_DIR/otz-current.tar.gz.partial"
```

The example intentionally has no automatic promotion/upload/delete operation. The
release operator records the actual path, SHA-256, completion time and restore result,
then atomically renames the checked archive. Keep `.partial` files out of scheduling's
latest-backup selection. This script creates no paid resource and does not prune backups.
A mounted/off-host destination should have private ACLs and appropriate encryption at
rest. Production local storage alone does not meet the F07 off-host criterion.

## Strict restore drill

```bash
PYTHON_BIN=/opt/otz/.venv/bin/python \
RESTORE_CHECK_LOCAL_TARBALL=/private/otz-20260908-120000.tar.gz \
  scripts/restore_check.sh
```

Without `RESTORE_CHECK_LOCAL_TARBALL`, the wrapper retains the existing B2 latest-download
path, using `B2_BUCKET` and optionally `B2_BIN`. The wrapper never provisions a bucket.

The default maximum age is 36 hours; override explicitly with
`RESTORE_CHECK_MAX_AGE_HOURS` / `--max-age-hours` only for a deliberate historical
restore, never to hide an overdue automatic backup. Migration defaults to the checkout's
Alembic head. For an intentional pre-migration recovery drill, pass the exact recorded
revision using `RESTORE_CHECK_EXPECTED_SCHEMA` / `--expected-schema` and run application
code compatible with that revision. This does not migrate the recovered DB silently.

Verification rejects missing/extra/duplicate archive entries, links/traversal, stale or
future timestamps, missing tables, empty core service tables, wrong migration/schema,
FK errors, content corruption, missing source photos and missing published derivatives.
The manifest has hashes for corruption detection, not a cryptographic signature; trust
and permissions of the chosen storage remain an operator responsibility.

After validation a child process adapts photo paths **inside a throwaway DB only** and
starts the real FastAPI application's lifespan. It opens the actual catalog and a
visible apartment, serves that apartment's photo from the restored media root and
verifies image decoding. Environment variables are cleared, `.env` loading is disabled,
analytics writes disabled, and socket connections blocked. There is no Telegram/email
client or listener opened, and no process points at production data. The copy is deleted
on completion, leaving the input archive unchanged. Report:
`data_restore_verified` + catalog/apartment/photo HTTP 200 + `photo_decoded: true`.

## Release and automation evidence still required

Record the real backup/restore outputs, protected config/session recovery location and
verification, off-host transfer integrity, retention policy, and the scheduler's first
actual automatic run. Do not call F07 live-verified from a unit test, file listing, old
archive or `PRAGMA integrity_check` alone. Restore new leads/messages by forward recovery;
do not automatically replace the live database with an older backup during app rollback.
