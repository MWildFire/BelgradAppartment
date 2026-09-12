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

## Daily off-host directory generations (no recurring 70 GB archive)

`scripts/offhost_backup.py` uses the existing SSH destination and a FileVault-protected
Mac directory. It creates complete immutable generations, not incomplete daily archives:
SQLite online snapshot + all state + every file declared in all media roots + a complete
manifest. Media unchanged by SHA-256 is hard-linked from the previous generation or an
extracted seed on the same filesystem. Changed files use system rsync protocol 29 with
`-I` and an exact NUL-separated file list. The changed-file transfer deliberately omits
`--link-dest`: unchanged files are already linked explicitly, and macOS openrsync can
exhaust launchd's file descriptor limit when copying changed files from a link destination.
No `--inplace`, source deletion
or remote prune occurs. Source and receiver re-hash all media; this consumes local I/O
but unchanged image bytes are neither retransferred nor duplicated per day. New or
changed media still requires its actual extra disk space.

The photo index is frozen before enumerating media, so concurrent publication cannot
add index references after the file inventory is fixed. Restore still requires every
indexed original and derivative, every DB photo reference, and every declared checksum.

Preserve the first completed full archive's extraction instead of downloading it again:

```bash
.venv/bin/python scripts/recovery.py verify /private/completed.tar.gz \
  --restore-dir /private/seed-0011 --expected-schema 0011_keep_on_site
```

Use code compatible with the recorded schema for this first smoke. The destination must
not exist. New `_smoke` copies only the database before repathing, leaving the extracted
generation pristine. An older drill's extraction whose DB was repathed remains usable
as a media seed: every new generation always gets a fresh source DB/state and a new full
validation. Seed layout is `manifest.json`, `data/otz.db`, and the manifest's media roots
(e.g. `data/inbound`, `data/listings`). Seed and generation directory must share a filesystem.
The original full archive stays unchanged; seed cleanup remains an explicit operator step.

After the first full transfer has finished and the new helper is installed on the server:

```bash
.venv/bin/python scripts/offhost_backup.py run \
  --ssh-target "$APP_SSH_TARGET" --ssh-key "$APP_SSH_KEY" \
  --private-root /private/offhost-generations --seed-dir /private/seed-0011 \
  --remote-root /opt/otz --remote-python /opt/otz/.venv/bin/python \
  --remote-user otz --remote-published-dir /opt/otz/data/inbound
```

The optional `--remote-user otz` runs remote reads as the service account through
noninteractive sudo, including rsync. The SSH identity must already have that existing
permission; the tool never installs credentials or changes sudo configuration.
A local flock and an SSH-held remote flock prevent overlapping generation jobs; future
full archives from the updated helper use the same source lock. A legacy full stream
already running before this change has no such lock: finish it before enabling the job.
These are backup locks, not scraper locks. Concurrent source changes fail hash validation;
this is a consistent SQLite snapshot plus individually verified media, not a global
filesystem point-in-time snapshot. Do not run concurrent media deletion. No source
maintenance process is stopped automatically.

A successful run stores `generations/<id>` and a separate `receipts/<id>.json`. It retains
only the last two **accepted** generations, pruning an older one only after the new full
restore PASS. Pending/failed generations are never accepted or used as the latest backup;
they remain for inspection and explicit cleanup. A 20 GiB free-space floor is checked
before work (`--min-free-gib` can raise it); an unexpectedly large delta can still fail,
without deleting the last good copy. Default cadence is 24 hours since the last verified
snapshot. Every successful daily generation gets the actual isolated catalog/card/photo
restore drill; there is no weekly-only shortcut to the 36-hour gate.

The receipt declares `backup_format: directory_generation`, `manifest_sha256` and
`generation_manifest_sha256`; it does **not** fabricate an `archive_sha256`. Consumers must
validate the declared format. `offhost_verified` is true only after the destination's full
verification. Without explicit protected inputs, `protected_recovery_verified` remains
false. Enable the fresh protected-generation workflow below to create the combined proof;
an old protected receipt is never inherited or given a new verification timestamp.

Replace `run` with `launch-agent` using the same arguments to print a macOS LaunchAgent
plist. It does not install/load a scheduler. The plist checks every six hours and at login,
while the runner performs work only when the daily copy is due; it writes private logs
under `--private-root/logs` (create that directory before loading the plist, or complete
the first manual run). Root/operator installs the reviewed plist and verifies its first
actual scheduled run. A Mac that is asleep/offline cannot satisfy a cloud-style backup
SLA: missed/offline/old evidence remains a real overdue condition. Existing AC power
awake settings do not guarantee battery, lid-closed or disconnected operation.

Standalone strict verification, including a fresh application/photo restore:

```bash
.venv/bin/python scripts/recovery.py verify-directory /private/offhost-generations/generations/ID
```

`launch-agent` and fake-SSH test results are configuration/local proof only. F07 automatic
acceptance still requires the scheduled run, fresh protected recovery proof, current
health receipt and a read-back of the accepted generation at the real off-host location.

Before application deployment, `--remote-recovery-script /opt/otz/tools/recovery/recovery.py`
lets the runner use separately staged tooling while source schema/models are read from
its explicit `--application-project-root /opt/otz`. Locally provide
`--application-project-root /path/to/compatible-checkout` for the source's actual schema
(e.g. 0011); schema checks and the network-disabled child app smoke use that checkout.
Do not run a 0013 ORM against a 0011 snapshot or silently migrate the recovered data.

A salvaged media tree may declare `format: unverified_media_seed` and
`result: seed_only_unverified`, with locally measured `files` sizes/hashes. It is a cache,
never an accepted backup or recovery receipt. The runner may reuse matching media bytes
from it, always obtaining a new authoritative remote manifest and fresh DB/state, then
checking every final hash and the actual application restore. The strict directory
verifier rejects the unverified seed itself.

## Fresh protected recovery for every directory generation

Stage `protected_recovery.py` beside the separately staged `recovery.py`; neither helper
requires an application deployment. Add these explicit arguments to the same runner or
printed LaunchAgent command, replacing session paths with the actual operator-verified
paths (aliases are safe labels, not session contents):

```bash
--protected-env-file /opt/otz/.env \
--protected-caddy-file /etc/caddy/Caddyfile \
--protected-session userbot=/absolute/actual-userbot.session \
--protected-session bot=/absolute/actual-bot.session \
--protected-remote-user root \
--remote-protected-recovery-script /opt/otz/tools/recovery/protected_recovery.py \
--publish-health-receipt
```

The runner requires all configured inputs and at least one explicit session. Existing
noninteractive sudo authorization must permit reading them; it never changes sudo or
installs credentials. `--protected-remote-user root` applies only to protected reads and
optional publication. The health receipt is owned by the ordinary `--remote-user` service
account (default `otz`), so an elevated secret reader does not make it unreadable by the app.

Before copying any secrets, the Mac verifies FileVault is on and the destination is inside
the current user's home on the same filesystem. External volumes and non-Mac destinations
are rejected by this workflow. Every generation creates fresh configuration copies and
SQLite online session snapshots, including committed WAL changes. Protected bytes go to
`protected-pending/<id>`, then `protected/<id>`; they never enter the data generation,
data archive, repository or health receipt. All protected directories require mode 0700,
all files 0600, and symlinks are rejected. Verification checks the exact component inventory,
every size/hash, valid nonempty configuration, SQLite integrity/FKs and nonempty session
authorization material. The protected manifest records only logical names and hashes.

The proof binds the current generation ID and data manifest SHA, checks creation/verification
dates, and records these actual checks. `protected_proof.live_authentication_checked` is
false: offline integrity and authorization bytes do not prove a live Telegram login.
Missing/unreadable configuration, missing sessions, unsafe storage/permissions or a failed
check prevent acceptance and preserve the previous completed pair. A previously data-only
copy cannot satisfy a newly enabled protected requirement, even before the daily deadline.
At each cadence check the protected bytes are rechecked before deciding a copy is not due.
Only two accepted paired generations remain after a new full verification succeeds;
failed/pending data or protected work is retained for explicit inspection.

`--publish-health-receipt` is optional and off by default. It performs one explicit remote
mutation **after** both local copies and their combined receipt are complete: an atomic
0600 write to `/opt/otz/data/state/recovery_receipt.json`. The source helper requires the
canonical root, safe backup staging paths, exact current data/protected manifest hashes,
fresh matched dates, successful catalog/card/photo evidence, protected verification proof
and the still-current live schema. Only the sanitized receipt is sent on private SSH stdin;
no secret file contents are included. Publication failure leaves the completed local copy
valid but records `health_receipt_published: false`; the next scheduler check retries by
making a fresh generation, so stale app health cannot be silently treated as updated.
An accepted local receipt includes `backup_format: directory_generation`, both data manifest
hash fields, `generation_id`, `protected_recovery_verified: true` and the fresh
`protected_proof`. It contains no fabricated archive hash. Release consumers must validate
the directory format explicitly.

For a one-time continuation of a source snapshot that already completed with exit 0,
the `run` command also accepts `--prepared-snapshot-dir` and
`--prepared-snapshot-manifest-sha256`. Obtain the SHA-256 of the completed manifest and
pass it explicitly; these options must never be used while its writer is still running.
The directory must be a direct `.manual-*` or `.offhost-*` child of the source backup
folder. Update the trusted staged `recovery.py` helper too: the runner calls its read-only
`check-snapshot-metadata` command while holding a new remote lease. It validates the
pinned manifest, age, source root and staged inventory without rehashing live source
media. The receiver checks the transferred manifest against the pinned SHA again, then
still hashes every data file and runs the isolated application restore. Fresh protected
snapshots and health publication use the normal gates. The manually prepared source
directory is preserved on both success and failure for operator review. These options
are rejected for `launch-agent`; scheduled backups must create fresh source snapshots.
