#!/usr/bin/env bash
# Exact-revision packet deployment. No host defaults, live-tree rsync or data overwrites.
# Stage is the default; --activate is an explicit operator action after receipt checks.
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "${PYTHON_BIN:-"$SCRIPT_DIR/../.venv/bin/python"}" "$SCRIPT_DIR/deploy_packet.py" "$@"
