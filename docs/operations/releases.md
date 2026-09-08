# Staged releases and rollback (F15/F23, 8 September 2026)

The previous `deploy_sync.sh` silently targeted an obsolete VPS and copied an arbitrary
working tree into running processes. It now accepts an exact committed packet and an
explicit SSH destination. Staging is the default; `--activate` performs the separate
operator action. No code path restores or downgrades the live database.

## Required receipts

The test receipt must describe checks actually completed against the full 40-character
release revision. Unit, changed-code lint, shell syntax and browser checks are required
by default. For a reviewed W1-only package the operator can explicitly choose applicable
checks using repeated `--required-check`; W2 changes require the browser check.

```json
{
  "revision": "FULL_COMMIT_SHA",
  "result": "passed",
  "checks": {
    "unit": "passed",
    "changed_lint": "passed",
    "shell": "passed",
    "browser": "passed"
  }
}
```

Keep actual test logs/evidence with this receipt. Do not manufacture a PASS from a prior
revision or skipped checks. The project's CI and `scripts/check_changed_lint.py` own the
check execution; packaging validates their receipt and never reruns an uncontrolled
production test suite.

The backup receipt is the JSON emitted by `scripts/recovery.py verify`, augmented by the
operator with `archive_sha256`, `offhost_verified: true` and
`protected_recovery_verified: true` after checking the off-host copy and protected
configuration/session recovery. Both those flags require real storage/restore evidence.
The restored DB revision must match production immediately before activation. Maximum
backup age is 36 hours. Catalog/apartment/photo HTTP 200 and decoded photo are mandatory.
A local file or a SQLite quick check alone cannot satisfy this gate.

## Prepare and stage

Commit the final reviewed implementation and lockfile, then build the exact Git archive:

```bash
.venv/bin/python scripts/release.py prepare \
  --revision "$RELEASE_REVISION" \
  --test-receipt "$PRIVATE_RELEASE_DIR/tests.json" \
  --output "$PRIVATE_RELEASE_DIR/release.tar.gz"

scripts/deploy_sync.sh \
  --target "$APP_SSH_TARGET" --ssh-key "$APP_SSH_KEY" \
  --remote-root /opt/otz --uv-bin /opt/otz/tools/uv-venv/bin/uv \
  --packet "$PRIVATE_RELEASE_DIR/release.tar.gz" \
  --backup-receipt "$PRIVATE_RELEASE_DIR/backup.json" --sudo
```

No host default exists. Packets contain tracked files only, with a hash/mode inventory;
uncommitted files, local `.env`, sessions and live data are excluded. A tracked private
data file is a hard failure. The stage step validates receipts and content, then uploads
to a private remote temporary directory. It prints the concrete activation command.
Existing packets/staging directories are never silently overwritten by preparation.

Add `--activate` to the same wrapper invocation to proceed through activation, or run the
printed command on the explicit server. Use the existing pinned uv binary supplied by
the operator; the script does not install/upgrade uv. The tracked production lock must
preserve validated dependencies, including CPU PyTorch/open-clip needed by Render Finder.

## Activation behavior

Activation currently supports `/opt/otz` and its canonical systemd topology only. It:

1. Acquires one operator lock and verifies a fresh complete backup receipt, the exact
   tested packet and live DB/FK/schema consistency.
2. Creates a separate versioned environment with `uv sync --frozen --no-dev --extra clip
   --no-editable`. This preserves the existing environment for rollback and installs the
   `otz` entrypoint. Required packages and the matching Playwright Chromium are checked
   before any running service is stopped.
3. Refuses active/starting scheduled or manual maintenance jobs. It pauses the previously
   active timers and checks again, then stops the previously active monitor/site/admin/
   subscription-bot services together. Unrelated units are not changed.
4. Retains prior code/environment in `/opt/otz/.releases/<revision>/rollback`, replaces
   runtime paths, applies additive Alembic migrations to the explicitly selected live DB,
   copies canonical service/timer files and performs `daemon-reload`.
5. Checks effective unit command/type/user/working directory/timeout and timer calendar/
   persistence/randomization; an overriding drop-in cannot silently change those fields.
   It restarts the previously active units, checks local health and exact runtime hashes,
   and saves `/opt/otz/release.json` plus an activation receipt.

Tracked timers preserve the observed schedule: nightly 03:00 UTC (+0–15 minutes), weekly
Sunday 04:00 UTC (+0–15 minutes), parser smoke every six hours at minute 30 (+0–5 minutes),
all persistent. Existing services' `ship-log` and failure notification paths remain.
**Do not use these legacy scheduled units as a no-notify canary.** Use the separately
implemented CLI evaluation/no-notify interface and prove no external sends there.

An activation error attempts to restore the prior application files/environment/units,
then restart their prior state. It never copies an older DB over new leads/messages,
never performs a destructive downgrade and never deletes media. Operator recovery is
still required if systemd/filesystem failure prevents automatic code rollback; inspect
`activation.json` before retrying. There is no automatic retention/pruning of releases.

## Explicit rollback and remaining live checks

```bash
sudo /usr/bin/python3 /opt/otz/scripts/release.py rollback \
  --remote-root /opt/otz --revision "$ACTIVE_RELEASE_REVISION"
```

Only the current recorded release can be rolled back. Maintenance must be idle and the
same operator lock is required. Old code must be compatible with the additive schema;
verify that compatibility with the release's tests before activation.

`activated_manual_verification_pending` intentionally stops short of the W6 live gate.
The operator must still check public catalog/card/map/media, admin authentication, real
Render Finder output/recipients, effective timers' next run, a bounded safe canary, a
full source pass and a real automatic follow-up run. Record those actual outcomes in the
implementation status; a process restart or packet upload is not completion.
