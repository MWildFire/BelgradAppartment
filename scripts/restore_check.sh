#!/usr/bin/env bash
# Backup restore drill + integrity check for the otz B2 backups.
#
# A backup that has never been restore-tested is not a backup. This script
# downloads the LATEST B2 backup, restores it into a throwaway temp dir, runs
# SQLite `PRAGMA integrity_check`, and runs a few smoke count queries. It is
# READ-ONLY with respect to production: it NEVER touches the live
# /opt/otz/data/otz.db — everything happens in a `mktemp -d` workdir that is
# wiped on exit. Safe to run anytime.
#
# Intended cadence: run manually ~monthly, or wire as an occasional nightly
# systemd oneshot that alerts (OnFailure) on a non-zero exit.
#
# Usage:
#   ./scripts/restore_check.sh
#   B2_BIN=~/.local/bin/b2 B2_BUCKET=otz-backups ./scripts/restore_check.sh
#
# Env overrides:
#   B2_BIN     path to the b2 CLI       (default: `b2` on PATH, else ~/.local/bin/b2)
#   B2_BUCKET  bucket to read backups   (default: otz-backups)
#   RESTORE_CHECK_LOCAL_TARBALL
#              if set to a path of an otz-*.tar.gz, SKIP B2 entirely and use that
#              local tarball. Handy for dry-running off-prod without B2 creds.
#
# Exit codes:
#   0  PASS — integrity_check == ok (a 0-listings WARNING does not fail the run)
#   non-zero  FAIL — no backup found, download/extract failure, or integrity != ok

set -euo pipefail

B2_BUCKET=${B2_BUCKET:-otz-backups}

log()  { printf '\033[1;32m[restore-check]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# ─────────────────────────── workdir + cleanup ─────────────────────────────
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/otz-restore-check.XXXXXX")
trap 'rm -rf "$WORKDIR"' EXIT

# ─────────────────────────── deps ──────────────────────────────────────────
command -v sqlite3 >/dev/null 2>&1 || fail "sqlite3 not found on PATH"
command -v tar     >/dev/null 2>&1 || fail "tar not found on PATH"

# ─────────────────────────── resolve b2 binary ────────────────────────────
resolve_b2() {
  if [ -n "${B2_BIN:-}" ]; then
    command -v "$B2_BIN" >/dev/null 2>&1 && { echo "$B2_BIN"; return; }
    [ -x "$B2_BIN" ] && { echo "$B2_BIN"; return; }
    fail "B2_BIN='$B2_BIN' is not executable / not on PATH"
  fi
  if command -v b2 >/dev/null 2>&1; then echo "b2"; return; fi
  if [ -x "$HOME/.local/bin/b2" ]; then echo "$HOME/.local/bin/b2"; return; fi
  fail "b2 CLI not found (set B2_BIN, install b2, or use RESTORE_CHECK_LOCAL_TARBALL)"
}

# ─────────────────────────── fetch the tarball ────────────────────────────
TARBALL=""
if [ -n "${RESTORE_CHECK_LOCAL_TARBALL:-}" ]; then
  log "using local tarball (B2 skipped): $RESTORE_CHECK_LOCAL_TARBALL"
  [ -f "$RESTORE_CHECK_LOCAL_TARBALL" ] \
    || fail "RESTORE_CHECK_LOCAL_TARBALL='$RESTORE_CHECK_LOCAL_TARBALL' is not a file"
  TARBALL="$WORKDIR/$(basename "$RESTORE_CHECK_LOCAL_TARBALL")"
  cp "$RESTORE_CHECK_LOCAL_TARBALL" "$TARBALL"
else
  B2=$(resolve_b2)
  log "b2 binary: $B2 ; bucket: $B2_BUCKET"

  # List the bucket and pick the latest otz-*.tar.gz. Backup names are
  # otz-<YYYYMMDD-HHMMSS>.tar.gz, so plain lexical sort == chronological.
  # `b2 ls <bucket>` prints one file name per line (newer CLIs also accept
  # `b2 ls b2://<bucket>`); we grep for our pattern and take the last sorted.
  LATEST=$("$B2" ls "$B2_BUCKET" 2>/dev/null \
    | grep -oE 'otz-[0-9]{8}-[0-9]{6}\.tar\.gz' \
    | sort \
    | tail -1 || true)

  [ -n "$LATEST" ] || fail "no otz-*.tar.gz found in bucket '$B2_BUCKET' (empty bucket or b2 not authorized?)"
  log "latest backup: $LATEST"

  TARBALL="$WORKDIR/$LATEST"
  log "downloading…"
  # download-file-by-name <bucket> <fileName> <localPath> works across b2 v3/v4.
  "$B2" download-file-by-name "$B2_BUCKET" "$LATEST" "$TARBALL" >/dev/null 2>&1 \
    || "$B2" file download "b2://$B2_BUCKET/$LATEST" "$TARBALL" >/dev/null 2>&1 \
    || fail "failed to download '$LATEST' from '$B2_BUCKET'"
  [ -s "$TARBALL" ] || fail "downloaded tarball is empty: $TARBALL"
fi

# ─────────────────────────── extract + locate db ──────────────────────────
EXTRACT="$WORKDIR/extract"
mkdir -p "$EXTRACT"
log "extracting…"
tar -xzf "$TARBALL" -C "$EXTRACT" || fail "tar extraction failed for $TARBALL"

DB=$(find "$EXTRACT" -maxdepth 2 -type f -name 'otz-*.db' | sort | tail -1 || true)
[ -n "$DB" ] || fail "no otz-*.db found inside the tarball"
[ -s "$DB" ] || fail "restored db is empty: $DB"
log "restored db: $(basename "$DB") ($(wc -c <"$DB" | tr -d ' ') bytes)"

# ─────────────────────────── integrity check ──────────────────────────────
log "running PRAGMA integrity_check…"
INTEGRITY=$(sqlite3 "$DB" 'PRAGMA integrity_check;' 2>&1) \
  || fail "sqlite3 could not open the restored db: $INTEGRITY"
if [ "$INTEGRITY" != "ok" ]; then
  printf '%s\n' "$INTEGRITY" >&2
  fail "integrity_check != ok (see above)"
fi
log "integrity_check: ok"

# ─────────────────────────── smoke count queries ──────────────────────────
# Guard each query: only count tables that actually exist (a backup may predate
# a table). table_exists / safe_count never abort the run on a missing table.
table_exists() {
  local t="$1" n
  n=$(sqlite3 "$DB" \
    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='$t';" 2>/dev/null || echo 0)
  [ "$n" = "1" ]
}
safe_count() {
  # safe_count <table> [where-clause] -> prints an integer, or "-" if absent.
  local t="$1" where="${2:-}"
  if table_exists "$t"; then
    sqlite3 "$DB" "SELECT count(*) FROM $t${where:+ WHERE $where};" 2>/dev/null || echo "-"
  else
    echo "-"
  fi
}

LISTINGS=$(safe_count listings)
LISTINGS_ACTIVE=$(safe_count listings "is_active=1")
MATCHES=$(safe_count matches)
LEADS=$(safe_count leads)

log "smoke counts:"
log "  listings           = $LISTINGS"
log "  listings(is_active)= $LISTINGS_ACTIVE"
log "  matches            = $MATCHES"
log "  leads              = $LEADS"

# `listings` is a core table — its absence means a bad/wrong backup, hard-fail.
table_exists listings || fail "core table 'listings' missing from restored db"

# 0 listings is suspicious but not corruption: WARN loudly, let integrity drive exit.
if [ "$LISTINGS" = "0" ]; then
  warn "restored db has 0 listings — backup may be stale/empty. Investigate."
fi

# ─────────────────────────── pass summary ─────────────────────────────────
log "──────────────────────────────────────────"
log "PASS — backup restored & integrity_check == ok"
log "  source: ${RESTORE_CHECK_LOCAL_TARBALL:-b2://$B2_BUCKET/${LATEST:-?}}"
log "  listings=$LISTINGS active=$LISTINGS_ACTIVE matches=$MATCHES leads=$LEADS"
log "──────────────────────────────────────────"
