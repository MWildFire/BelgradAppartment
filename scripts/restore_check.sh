#!/usr/bin/env bash
# Strict F07 restore drill. All DB/app activity stays in a private temporary copy.
# Existing B2 download support retained; no bucket or resource is ever created.
# Required: project Python environment (set PYTHON_BIN when running off-host).
# RESTORE_CHECK_LOCAL_TARBALL skips B2; otherwise choose latest named backup.
# RESTORE_CHECK_MAX_AGE_HOURS defaults to 36; schema defaults to checkout head.
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PYTHON_BIN=${PYTHON_BIN:-"$SCRIPT_DIR/../.venv/bin/python"}
B2_BUCKET=${B2_BUCKET:-otz-backups}
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/otz-restore-check.XXXXXX")
trap 'rm -rf "$WORKDIR"' EXIT
fail() { printf '[restore-check] FAIL: %s\n' "$*" >&2; exit 1; }
[ -x "$PYTHON_BIN" ] || fail 'project Python unavailable; set PYTHON_BIN'
if [ -n "${RESTORE_CHECK_LOCAL_TARBALL:-}" ]; then
  TARBALL=$RESTORE_CHECK_LOCAL_TARBALL
  [ -s "$TARBALL" ] || fail 'local backup missing/empty'
else
  B2=${B2_BIN:-b2}
  if ! command -v "$B2" >/dev/null 2>&1; then
    B2=${B2_BIN:-"$HOME/.local/bin/b2"}
  fi
  [ -x "$B2" ] || command -v "$B2" >/dev/null 2>&1 || fail 'B2 CLI unavailable'
  LATEST=$("$B2" ls "$B2_BUCKET" | sed -nE 's/.*(otz-[0-9]{8}-[0-9]{6}\.tar\.gz).*/\1/p' | sort | tail -1)
  [ -n "$LATEST" ] || fail 'no named backups found in configured bucket'
  TARBALL="$WORKDIR/$LATEST"
  "$B2" download-file-by-name "$B2_BUCKET" "$LATEST" "$TARBALL" >/dev/null 2>&1 \
    || "$B2" file download "b2://$B2_BUCKET/$LATEST" "$TARBALL" >/dev/null 2>&1 \
    || fail 'backup download failed'
fi
ARGS=(verify "$TARBALL" --max-age-hours "${RESTORE_CHECK_MAX_AGE_HOURS:-36}")
if [ -n "${RESTORE_CHECK_EXPECTED_SCHEMA:-}" ]; then
  ARGS+=(--expected-schema "$RESTORE_CHECK_EXPECTED_SCHEMA")
fi
"$PYTHON_BIN" "$SCRIPT_DIR/recovery.py" "${ARGS[@]}"
