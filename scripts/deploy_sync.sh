#!/usr/bin/env bash
# Sync local code changes to the production VPS.
#
# Pushes everything except:
#   - .venv, .git, caches
#   - data/ (DB, photos, sessions — owned by the VPS, never overwrite)
#   - .env (env file is per-environment; copy manually when it changes)
#
# Usage:
#   ./scripts/deploy_sync.sh                 # rsync to VPS
#   ./scripts/deploy_sync.sh --restart       # rsync + restart otz-monitor

set -euo pipefail

VPS=${VPS:-root@90.156.254.159}
REMOTE=${REMOTE:-/opt/otz}
RESTART=false

for arg in "$@"; do
  case "$arg" in
    --restart) RESTART=true ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

echo "→ rsync to $VPS:$REMOTE"
# Note: macOS ships openrsync (2.6-compatible) which lacks --chown. We don't preserve
# owner/group (no -o -g) and fix ownership in a follow-up ssh chown below. Falls back to
# `data/` being owned by root, then we re-own to otz so the service can write.
# IMPORTANT: '.venv' has NO trailing slash so it also excludes a .venv *symlink* (a worktree
# uses one). With the trailing-slash form, rsync tried to replace the server's .venv dir with
# the symlink and --delete-after wiped the venv. Also exclude server-only state (logs, .claude,
# .DS_Store) so --delete-after never removes it.
rsync -rlptzv --delete-after \
  --exclude='.venv' --exclude='.git/' --exclude='__pycache__/' \
  --exclude='*.pyc' --exclude='.pytest_cache/' --exclude='.mypy_cache/' --exclude='.ruff_cache/' \
  --exclude='.cache/' --exclude='.config/' --exclude='.local/' \
  --include='data/' --include='data/districts.yaml' --exclude='data/*' \
  --exclude='.env' --exclude='.env.bak-*' --exclude='*.session' --exclude='*.session-journal' \
  --exclude='logs/' --exclude='.claude/' --exclude='.DS_Store' \
  --exclude='tests/fixtures/halooglasi/' --exclude='tests/e2e/site/_artifacts/' \
  ./ "$VPS:$REMOTE/" | tail -10
# `data/` may contain a stale/dangling Telethon `*.session-journal` symlink.  GNU chown follows
# symlinks by default and then exits non-zero, even though the code sync succeeded.  Change the
# symlink's ownership itself instead, so an obsolete journal cannot block a deploy.
ssh "$VPS" "chown -hR otz:otz $REMOTE/data $REMOTE/src $REMOTE/scripts $REMOTE/tests"

if $RESTART; then
  echo ""
  echo "→ restart otz-monitor"
  ssh "$VPS" 'systemctl restart otz-monitor && sleep 2 && systemctl is-active otz-monitor'
fi

echo ""
echo "✓ done"
echo ""
echo "Run something in the background on the VPS:"
echo "  ssh $VPS 'otz-bg <subcommand>'"
