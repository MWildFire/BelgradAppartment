#!/usr/bin/env bash
# Pretty progress watcher for running scrape jobs.
#
# Usage:
#   ./scripts/watch_scrape.sh                  # one snapshot of all running scrapes
#   ./scripts/watch_scrape.sh nekretnine       # only the nekretnine job
#   watch -n 30 ./scripts/watch_scrape.sh      # auto-refresh every 30s

cd "$(dirname "$0")/.." || exit 1

WHICH=${1:-all}

show_log() {
  local name=$1 logfile=$2
  echo ""
  echo "────────────────────────────────────────────────────────────"
  echo "  $name  ($logfile)"
  echo "────────────────────────────────────────────────────────────"
  if [ ! -f "$logfile" ]; then
    echo "  (log file does not exist)"
    return
  fi
  local started size
  started=$(stat -f '%Sm' -t '%H:%M:%S' "$logfile" 2>/dev/null || stat -c '%y' "$logfile" 2>/dev/null | cut -d. -f1)
  size=$(stat -f '%z' "$logfile" 2>/dev/null || stat -c '%s' "$logfile" 2>/dev/null)
  echo "  log started: $started   size: ${size} bytes"
  echo ""
  echo "  --- phase markers (list_recent / Scrape summary) ---"
  grep -E 'list_recent →|Scrape summary|previewed=' "$logfile" 2>/dev/null | tail -5 | sed 's/^/  /'
  echo ""
  echo "  --- last 4 log lines ---"
  tail -4 "$logfile" 2>/dev/null | sed 's/^/  /'
}

echo "════════════════════════════════════════════════════════════"
echo "  SCRAPE STATUS @ $(date '+%H:%M:%S')"
echo "════════════════════════════════════════════════════════════"

echo ""
echo "  --- running processes ---"
if pgrep -f "src.cli scrape" >/dev/null; then
  pgrep -lf "src.cli scrape" | sed 's/^/  ✓ /'
else
  echo "  ✗ no scrape jobs running"
fi

echo ""
echo "  --- DB counters ---"
.venv/bin/python -m src.cli stats 2>&1 | grep -E '(NEKRETNINE|CETIRI_ZIDA|HALOOGLASI)' | sed 's/^/  /'

if [ "$WHICH" = "all" ] || [ "$WHICH" = "full" ]; then
  show_log "FULL scrape (all platforms)" /tmp/scrape_full.log
fi
if [ "$WHICH" = "all" ] || [ "$WHICH" = "nekretnine" ]; then
  show_log "NEKRETNINE scrape (with /stranica/N/ pagination fix)" /tmp/scrape_nekretnine.log
fi
if [ "$WHICH" = "all" ] || [ "$WHICH" = "halooglasi" ]; then
  show_log "HALOOGLASI scrape (Playwright)" /tmp/scrape_halooglasi.log
fi

echo ""
echo "════════════════════════════════════════════════════════════"
