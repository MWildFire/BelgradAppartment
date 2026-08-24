#!/usr/bin/env bash
# One-shot installer for /opt/otz on a fresh Ubuntu 24.04 VPS.
# Idempotent — safe to re-run after partial failure.
#
# Run AS ROOT once (creates the otz user + system deps), then it drops privileges
# to the otz user for venv/playwright/Python work.
#
# Usage:
#   curl -fsSL https://example.com/vps_install.sh | bash
# or after rsync:
#   sudo bash /opt/otz/scripts/vps_install.sh

set -euo pipefail

OTZ_HOME=/opt/otz
OTZ_USER=otz
LOG=/var/log/otz_install.log

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*" | tee -a "$LOG"; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" | tee -a "$LOG" >&2; exit 1; }

if [ "$EUID" -ne 0 ]; then
  fail "must run as root (sudo bash $0)"
fi

mkdir -p /var/log
touch "$LOG"

# ─────────────────────────── 1. System packages ───────────────────────────
log "apt update + base packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-dev \
  build-essential libssl-dev libffi-dev \
  git curl rsync ca-certificates sqlite3 \
  ufw fail2ban unattended-upgrades \
  xvfb \
  chromium-browser libnss3 libatk1.0-0 libatk-bridge2.0-0 libxss1 \
  libasound2t64 libcups2t64 libxcomposite1 libxdamage1 libxrandr2 \
  libgbm1 libgtk-3-0t64 libpango-1.0-0 fonts-liberation \
  expect openssh-server

# ─────────────────────────── 2. UFW + fail2ban + auto-updates ──────────────
log "firewall + ssh hardening…"
ufw allow OpenSSH >/dev/null
yes | ufw enable >/dev/null 2>&1 || true
systemctl enable --now fail2ban >/dev/null 2>&1 || true
dpkg-reconfigure -p low unattended-upgrades || true

# ─────────────────────────── 3. otz user + dirs ────────────────────────────
if ! id -u "$OTZ_USER" >/dev/null 2>&1; then
  log "creating user $OTZ_USER…"
  adduser --disabled-password --gecos "" "$OTZ_USER"
fi
mkdir -p "$OTZ_HOME"/{data/{inbound,listings,state,playwright,backups},logs}
chown -R "$OTZ_USER:$OTZ_USER" "$OTZ_HOME"
mkdir -p /var/log/otz
chown -R "$OTZ_USER:$OTZ_USER" /var/log/otz

# ─────────────────────────── 4. Python venv + deps ─────────────────────────
if [ ! -d "$OTZ_HOME/.venv" ]; then
  log "creating venv with system python3…"
  sudo -u "$OTZ_USER" python3 -m venv "$OTZ_HOME/.venv"
fi

log "installing python deps (will take ~10 min on first run)…"
sudo -u "$OTZ_USER" "$OTZ_HOME/.venv/bin/pip" install --upgrade pip -q

if [ -f "$OTZ_HOME/pyproject.toml" ]; then
  # torch + torchvision MUST both come from the CPU wheel index — open_clip pulls torchvision
  # via pip-dependency-resolver, and a CUDA-wheel torchvision + cpu-wheel torch combo throws
  # "operator torchvision::nms does not exist" at runtime. Install both up-front.
  sudo -u "$OTZ_USER" "$OTZ_HOME/.venv/bin/pip" install -q \
    --index-url https://download.pytorch.org/whl/cpu 'torch>=2.3' 'torchvision'
  sudo -u "$OTZ_USER" "$OTZ_HOME/.venv/bin/pip" install -q -e "$OTZ_HOME"
  sudo -u "$OTZ_USER" "$OTZ_HOME/.venv/bin/pip" install -q \
    'open-clip-torch>=2.24' \
    'playwright>=1.45' 'playwright-stealth>=2.0' \
    'anthropic>=0.40'
  # Sanity — fail loud here, not at first scrape
  sudo -u "$OTZ_USER" "$OTZ_HOME/.venv/bin/python" -c \
    "import torch, torchvision, open_clip; assert '+cpu' in torch.__version__ and '+cpu' in torchvision.__version__, 'CPU wheels not picked up'" \
    || fail "torch/torchvision ABI mismatch — open_clip will crash at runtime"
else
  log "WARN: no pyproject.toml in $OTZ_HOME — skipping pip install; rsync code first"
fi

if [ -d "$OTZ_HOME/.venv" ] && ! sudo -u "$OTZ_USER" \
    "$OTZ_HOME/.venv/bin/python" -c "import playwright" 2>/dev/null; then
  log "playwright not installed; deps probably skipped — fix above"
elif [ -d "$OTZ_HOME/.venv" ]; then
  log "installing playwright chromium (full, ~200MB)…"
  sudo -u "$OTZ_USER" "$OTZ_HOME/.venv/bin/playwright" install chromium --no-shell >/dev/null
fi

# ─────────────────────────── 5. DB migrations ──────────────────────────────
if [ -f "$OTZ_HOME/alembic.ini" ] && [ -f "$OTZ_HOME/.env" ]; then
  log "running alembic migrations…"
  sudo -u "$OTZ_USER" bash -c "cd $OTZ_HOME && .venv/bin/alembic upgrade head"
fi

# ─────────────────────────── 6. systemd units ──────────────────────────────
log "installing systemd units…"

cat >/etc/systemd/system/otz-monitor.service <<UNIT
[Unit]
Description=otz-rent-finder — live Telegram monitor
After=network-online.target
Wants=network-online.target
# OnFailure fires on EVERY transition to failed (including between Restart cycles) — NOT only
# when restarts are exhausted. StartLimit*/Restart bound HOW MANY times we fail (and thus how
# many alerts), not whether OnFailure is single-shot. 5 crashes / 10 min then StartLimit trips:
StartLimitIntervalSec=600
StartLimitBurst=5
OnFailure=otz-notify-failure@%n.service

[Service]
Type=simple
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
Environment="PATH=$OTZ_HOME/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
ExecStart=/usr/bin/xvfb-run -a $OTZ_HOME/.venv/bin/python -m src.cli monitor
Restart=on-failure
RestartSec=30
# Exit 3 = Telethon session revoked/unauthorized: re-auth is the only fix, so do NOT crash-loop.
# Go straight to failed ONCE (one OnFailure → one alert). Transient errors (exit 1) still restart.
RestartPreventExitStatus=3
StandardOutput=append:/var/log/otz/monitor.log
StandardError=append:/var/log/otz/monitor.log

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/otz-nightly-refresh.service <<UNIT
[Unit]
Description=otz-rent-finder — nightly incremental refresh
OnFailure=otz-notify-failure@%n.service

[Service]
Type=oneshot
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
ExecStart=/usr/bin/xvfb-run -a $OTZ_HOME/.venv/bin/python -m src.cli nightly-refresh --incremental --max-pages=5
ExecStartPost=-$OTZ_HOME/.venv/bin/python -m src.cli ship-log /var/log/otz/nightly-refresh.log --label nightly-refresh
StandardOutput=append:/var/log/otz/nightly-refresh.log
StandardError=append:/var/log/otz/nightly-refresh.log
TimeoutStartSec=8h
UNIT

cat >/etc/systemd/system/otz-nightly-refresh.timer <<UNIT
[Unit]
Description=otz nightly incremental refresh (03:00 UTC = 05:00 Belgrade)

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
UNIT

cat >/etc/systemd/system/otz-weekly-full-refresh.service <<UNIT
[Unit]
Description=otz-rent-finder — weekly full refresh
OnFailure=otz-notify-failure@%n.service

[Service]
Type=oneshot
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
# Browser fallback is required by Nekretnine's DataDome-protected pages. Keep this idempotent
# guard here as well as in scrape-smoke so a cleaned Playwright cache cannot break weekly.
ExecStartPre=$OTZ_HOME/.venv/bin/playwright install chromium
ExecStart=/usr/bin/xvfb-run -a $OTZ_HOME/.venv/bin/python -m src.cli nightly-refresh --max-pages=500
ExecStartPost=-$OTZ_HOME/.venv/bin/python -m src.cli ship-log /var/log/otz/nightly-refresh.log --label weekly-full-refresh
StandardOutput=append:/var/log/otz/nightly-refresh.log
StandardError=append:/var/log/otz/nightly-refresh.log
TimeoutStartSec=8h
UNIT

cat >/etc/systemd/system/otz-weekly-full-refresh.timer <<UNIT
[Unit]
Description=otz weekly full refresh (Sunday 04:00 UTC)

[Timer]
OnCalendar=Sun *-*-* 04:00:00
Persistent=true
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
UNIT

cat >/etc/systemd/system/otz-daily-summary.service <<UNIT
[Unit]
Description=otz-rent-finder — daily activity summary to bot recipients
OnFailure=otz-notify-failure@%n.service

[Service]
Type=oneshot
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
ExecStart=$OTZ_HOME/.venv/bin/python -m src.cli daily-summary
ExecStartPost=-$OTZ_HOME/.venv/bin/python -m src.cli ship-log /var/log/otz/daily-summary.log --label daily-summary --lines 30
ExecStartPost=-$OTZ_HOME/.venv/bin/python -m src.cli ship-log /var/log/otz/monitor.log --label monitor --lines 200
StandardOutput=append:/var/log/otz/daily-summary.log
StandardError=append:/var/log/otz/daily-summary.log
TimeoutStartSec=5min
UNIT

cat >/etc/systemd/system/otz-daily-summary.timer <<UNIT
[Unit]
Description=otz daily summary (09:00 Belgrade)

[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT

cat >/etc/systemd/system/otz-scrape-smoke.service <<UNIT
[Unit]
Description=otz-rent-finder — live smoke probe (markup-change protection, A2)
# `scrape-smoke` delivers one detailed, platform-specific Telegram alert itself.  Do not attach
# the generic systemd notifier here: it produced a duplicate second alert for every same failure.

[Service]
Type=oneshot
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
# `playwright install` is idempotent.  It makes a fresh VPS or a cleaned cache
# recover its browser runtime before a browser-fallback source is declared down.
ExecStartPre=$OTZ_HOME/.venv/bin/playwright install chromium
# xvfb-run because halooglasi's probe drives Playwright (same wrapper as monitor/nightly).
ExecStart=/usr/bin/xvfb-run -a $OTZ_HOME/.venv/bin/python -m src.cli scrape-smoke
ExecStartPost=-$OTZ_HOME/.venv/bin/python -m src.cli ship-log /var/log/otz/scrape-smoke.log --label scrape-smoke --lines 40
StandardOutput=append:/var/log/otz/scrape-smoke.log
StandardError=append:/var/log/otz/scrape-smoke.log
TimeoutStartSec=30min
UNIT

cat >/etc/systemd/system/otz-scrape-smoke.timer <<UNIT
[Unit]
Description=otz scrape smoke probe (every 6h, offset from the 03:00 nightly)

[Timer]
OnCalendar=*-*-* 00/6:30:00
Persistent=true
RandomizedDelaySec=5min

[Install]
WantedBy=timers.target
UNIT

# ─── C2: saved-search subscription LINKING daemon (OPTIONAL, OFF by default) ───
# A NEW long-running aiogram poller that handles /start sub_<token> to link a Telegram chat to a
# saved-search subscription. It is OPTIONAL: enabling it is a deploy choice (it needs TG_BOT_TOKEN
# + SITE_TELEGRAM_BOT_USERNAME set, and SUBSCRIPTION_BOT_ENABLED=1 in .env — without that the
# command no-ops). The unit is written but NOT enabled here; enable it manually only when running
# the Telegram subscription channel. e-mail subscriptions need NONE of this.
cat >/etc/systemd/system/otz-subscription-bot.service <<UNIT
[Unit]
Description=otz-rent-finder — Telegram saved-search subscription linking daemon (C2, optional)
After=network-online.target
Wants=network-online.target
OnFailure=otz-notify-failure@%n.service

[Service]
Type=simple
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
ExecStart=$OTZ_HOME/.venv/bin/python -m src.cli subscription-bot
Restart=on-failure
RestartSec=30
StartLimitBurst=5
StartLimitIntervalSec=600
StandardOutput=append:/var/log/otz/subscription-bot.log
StandardError=append:/var/log/otz/subscription-bot.log

[Install]
WantedBy=multi-user.target
UNIT
# NOTE: intentionally NOT enabled. To turn on the Telegram subscription channel:
#   1) set SUBSCRIPTION_BOT_ENABLED=1, TG_BOT_TOKEN=…, SITE_TELEGRAM_BOT_USERNAME=<botname> in .env
#   2) systemctl enable --now otz-subscription-bot

# Triggered via OnFailure=otz-notify-failure@%n.service on the units above.
# %i = name of the failed unit. Smoke: systemctl start otz-notify-failure@smoke.service
cat >/etc/systemd/system/otz-notify-failure@.service <<UNIT
[Unit]
Description=otz-rent-finder — Telegram alert that unit %i failed
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
ExecStart=$OTZ_HOME/.venv/bin/otz notify-failure --service %i
StandardOutput=append:/var/log/otz/notify-failure.log
StandardError=append:/var/log/otz/notify-failure.log
TimeoutStartSec=2min
UNIT

systemctl daemon-reload
log "systemd units installed. Enable + start with:"
log "  systemctl enable --now otz-monitor"
log "  systemctl enable --now otz-nightly-refresh.timer"
log "  systemctl enable --now otz-weekly-full-refresh.timer"
log "  systemctl enable --now otz-daily-summary.timer"
log "  systemctl enable --now otz-scrape-smoke.timer"

# ─────────────────────────── 7. Final check ────────────────────────────────
log "──────────────────────────────────────────"
log "Install done. Status check:"
log ""
log "  $OTZ_HOME/.venv/bin/python -m src.cli stats"
log "  systemctl status otz-monitor"
log "  journalctl -u otz-monitor -f"
log ""
log "Don't forget:"
log "  1. Put .env at $OTZ_HOME/.env (mode 600)"
log "  2. Put Telethon session at $OTZ_HOME/data/otz_monitor.session (mode 600)"
log "  3. systemctl enable --now otz-monitor otz-nightly-refresh.timer otz-weekly-full-refresh.timer otz-daily-summary.timer otz-scrape-smoke.timer"
log "──────────────────────────────────────────"
