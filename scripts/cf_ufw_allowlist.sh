#!/usr/bin/env bash
# cf_ufw_allowlist.sh — Stage S Wave 3 (S3-2): lock origin 80/443 to Cloudflare edge IPs only.
#
# After the site is behind Cloudflare (proxied), the origin (90.156.254.159) should accept HTTP/HTTPS
# ONLY from CF's published ranges. A direct hit on the origin IP then fails → origin is hidden.
#
# SAFETY (this runs on the LIVE VPS):
#   • Touches ONLY ports 80 and 443. Never SSH (22), never Outline (50029/28395), never anything else.
#   • DRY-RUN by default — prints the exact `ufw` commands and changes NOTHING. Pass --apply to execute.
#   • Refuses to --apply unless an SSH (22) allow rule already exists (anti-lockout guard).
#   • Fails safe: if the CF range fetch is empty/unreachable, aborts WITHOUT changing the firewall
#     (so a network blip can't leave 80/443 open-to-none or half-configured).
#   • Idempotent: `ufw` skips adding an identical existing rule, so re-running is safe.
#
# Usage:
#   scripts/cf_ufw_allowlist.sh            # dry-run: show what would change
#   sudo scripts/cf_ufw_allowlist.sh --apply
#
# Rollback (to temporarily debug the origin directly):
#   sudo ufw allow 80,443/tcp ; sudo ufw delete deny 80,443/tcp   # re-open, then re-run this script later
set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

CF_V4_URL="https://www.cloudflare.com/ips-v4"
CF_V6_URL="https://www.cloudflare.com/ips-v6"

echo "== Cloudflare origin firewall allowlist (ports 80,443) =="
echo "Fetching CF ranges…"
v4="$(curl -fsS --max-time 15 "$CF_V4_URL" || true)"
v6="$(curl -fsS --max-time 15 "$CF_V6_URL" || true)"

# Fail-safe: both empty → abort, change nothing.
if [[ -z "${v4//[[:space:]]/}" && -z "${v6//[[:space:]]/}" ]]; then
  echo "ERROR: could not fetch Cloudflare ranges (both empty). Aborting — firewall untouched." >&2
  exit 1
fi

mapfile -t ranges < <(printf '%s\n%s\n' "$v4" "$v6" | grep -E '[0-9a-fA-F:.]+/[0-9]+' || true)
if [[ "${#ranges[@]}" -eq 0 ]]; then
  echo "ERROR: fetched data contained no CIDR ranges. Aborting — firewall untouched." >&2
  exit 1
fi
echo "Got ${#ranges[@]} CF CIDR ranges."

# Build the command list. Allows (per-range) FIRST, then a single deny for 80,443 from anyone else.
# ufw is first-match; the more-specific `from <range>` allows win over the catch-all deny.
cmds=()
for r in "${ranges[@]}"; do
  cmds+=("ufw allow proto tcp from $r to any port 80,443 comment cf-edge")
done
cmds+=("ufw deny 80,443/tcp comment cf-lock")

if [[ "$APPLY" -eq 0 ]]; then
  echo
  echo "DRY-RUN — would run (re-run with --apply, as root):"
  printf '  %s\n' "${cmds[@]}"
  echo
  echo "(no changes made)"
  exit 0
fi

# --apply path: must be root + must already allow SSH (anti-lockout).
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "ERROR: --apply must run as root (sudo)." >&2
  exit 1
fi
if ! ufw status | grep -qE '(^|[[:space:]])22(/tcp)?[[:space:]].*ALLOW'; then
  echo "ERROR: no SSH (22) ALLOW rule found in ufw — refusing to apply (anti-lockout)." >&2
  echo "Add it first:  ufw allow 22/tcp" >&2
  exit 1
fi

echo "Applying…"
for c in "${cmds[@]}"; do
  echo "+ $c"
  eval "$c"
done
echo
echo "Done. Verify:  ufw status numbered"
echo "From OUTSIDE CF, a direct hit on the origin :443 must now FAIL (origin hidden)."
