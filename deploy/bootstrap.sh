#!/bin/bash
# deploy/bootstrap.sh
#
# Takes a freshly imaged Raspberry Pi 5 to a fully running Maverick Telemetry
# Hub, then hands ongoing updates to the pull-deploy timer.
#
# ONE-LINER ON A BARE PI:
#   sudo apt install -y git \
#     && git clone https://github.com/AlexTs-dev/maverick-telemetry-hub.git ~/maverick-telemetry-hub \
#     && ~/maverick-telemetry-hub/deploy/bootstrap.sh
#
# DESIGN: this script installs *prerequisites and units only*. It never fetches
# build artifacts itself — it runs deploy/pull-deploy.sh for that, so there is
# exactly one code path that installs a release and no chance of bootstrap and
# OTA drifting apart.
#
# Idempotent: safe to re-run at any time.

set -euo pipefail

PROD="$HOME/maverick-telemetry-hub"
SVC_USER="$(id -un)"
DB_PATH="$HOME/maverick_telemetry.db"
SYSTEMCTL="$(command -v systemctl)"

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as the service user (e.g. pi), not root — sudo is used where needed."
[ -d "$PROD" ]       || die "Repo not found at $PROD. Clone it first (see the header)."

log "User=$SVC_USER  Repo=$PROD  DB=$DB_PATH"

# ---------------------------------------------------------------------------
# 1. System packages
#
# nodejs/npm come from Debian rather than NodeSource: the express_bridge unit
# hardcodes /usr/bin/node, and Debian 13 ships Node 20, which is what CI builds
# the client against. build-essential + python3-dev are needed because
# better-sqlite3 falls back to compiling from source when no arm64 prebuild
# matches the local Node ABI.
# ---------------------------------------------------------------------------
log "Installing system packages (apt)..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  git curl \
  mosquitto mosquitto-clients \
  nodejs npm \
  python3 python3-venv python3-dev \
  sqlite3 build-essential \
  chrony

# Tauri RUNTIME libraries for the kiosk binary.
#
# CI installs the -dev variants to *build* the binary; the Pi only ever runs a
# prebuilt one, and a prebuilt Tauri app is still dynamically linked against
# WebKitGTK. Without these the kiosk crash-loops with
#   libwebkit2gtk-4.1.so.0: cannot open shared object file
# which is invisible until something actually tries to open the window.
log "Installing Tauri runtime libraries..."
sudo apt-get install -y -qq \
  libwebkit2gtk-4.1-0 \
  libjavascriptcoregtk-4.1-0 \
  librsvg2-2 \
  libayatana-appindicator3-1

log "node $(node --version)  npm $(npm --version)  python $(python3 --version)"

# ---------------------------------------------------------------------------
# 2. Mosquitto — the broker every process communicates through
# ---------------------------------------------------------------------------
log "Configuring mosquitto..."
sudo install -m 644 "$PROD/deploy/mosquitto-maverick.conf" \
                    /etc/mosquitto/conf.d/maverick.conf
sudo "$SYSTEMCTL" enable --now mosquitto
sudo "$SYSTEMCTL" restart mosquitto

# ---------------------------------------------------------------------------
# 2b. Time service
#
# The Pi is the ONLY clock source in the truck (no internet), and the Jetson
# gates frame publishing on having a sane clock — the Orin has no RTC and boots
# in 1970. Frame/reading correlation is by timestamp, so this is load-bearing
# for vision, not a nicety. See jetson/deploy/chrony-jetson.conf for the client.
# ---------------------------------------------------------------------------
log "Configuring chrony to serve time to the Jetson..."
sudo install -m 644 "$PROD/deploy/chrony-pi.conf" /etc/chrony/conf.d/maverick.conf
sudo "$SYSTEMCTL" enable --now chrony
sudo "$SYSTEMCTL" restart chrony

# ---------------------------------------------------------------------------
# 3. Serial access for the OBDLink EX
#
# obd_poller runs as this user, so it needs dialout for /dev/ttyUSB0. Group
# changes only apply to NEW logins — the poller picks it up because systemd
# starts it fresh, but an existing shell won't see it until re-login.
# ---------------------------------------------------------------------------
if id -nG "$SVC_USER" | tr ' ' '\n' | grep -qx dialout; then
  log "User already in dialout"
else
  log "Adding $SVC_USER to dialout"
  sudo usermod -a -G dialout "$SVC_USER"
fi

# ---------------------------------------------------------------------------
# 4. Python environment
#
# obd + paho-mqtt are the only runtime deps of the three edge processes.
# ---------------------------------------------------------------------------
if [ ! -x "$PROD/venv/bin/python" ]; then
  log "Creating Python venv..."
  python3 -m venv "$PROD/venv"
fi
log "Installing Python deps..."
"$PROD/venv/bin/pip" install --quiet --upgrade pip
"$PROD/venv/bin/pip" install --quiet --upgrade obd paho-mqtt

# ---------------------------------------------------------------------------
# 5. Server environment file
#
# Only the DTC interpreter needs the Anthropic key; the bridge runs fine
# without it (diagnosis requests fail gracefully), so an absent key must never
# block a deploy. Never overwrite an existing .env — it holds a real secret.
# ---------------------------------------------------------------------------
if [ ! -f "$PROD/server/.env" ]; then
  log "Creating server/.env (add ANTHROPIC_API_KEY later to enable DTC diagnosis)"
  cat > "$PROD/server/.env" <<'EOF'
# Maverick Telemetry Hub — Express bridge environment
# The bridge runs fine without a key; only DTC interpretation needs it.
# ANTHROPIC_API_KEY=your_key_here
PORT=3000
EOF
  chmod 600 "$PROD/server/.env"
else
  log "server/.env already present — leaving untouched"
fi

# ---------------------------------------------------------------------------
# 6. Passwordless restart for unattended deploys
#
# pull-deploy.sh (and the dashboard's POST /api/version/update) restart these
# units with no TTY to type a password into. Scoped to exactly these commands.
# ---------------------------------------------------------------------------
log "Installing sudoers rule..."
sudo tee /etc/sudoers.d/maverick >/dev/null <<EOF
# Maverick Telemetry Hub — unattended service restarts for pull-deploy.
# Installed by deploy/bootstrap.sh. Scoped to these exact commands.
$SVC_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart express_bridge, $SYSTEMCTL restart kiosk, $SYSTEMCTL restart express_bridge kiosk
EOF
sudo chmod 440 /etc/sudoers.d/maverick
# A malformed sudoers file can lock the user out of sudo entirely, so validate
# and roll back rather than leaving a broken rule in place.
sudo visudo -c -f /etc/sudoers.d/maverick >/dev/null || {
  sudo rm -f /etc/sudoers.d/maverick
  die "sudoers validation failed — removed the bad file"
}

# ---------------------------------------------------------------------------
# 7. systemd units
# ---------------------------------------------------------------------------
log "Installing systemd units..."
sudo install -m 644 "$PROD"/deploy/*.service /etc/systemd/system/
sudo install -m 644 "$PROD"/deploy/pull-deploy.timer /etc/systemd/system/
sudo "$SYSTEMCTL" daemon-reload

# ---------------------------------------------------------------------------
# 8. Database
#
# migrate.py is idempotent and applies only pending versions. MAVERICK_DB_PATH
# must match the value in the *.service units, or the services would read a
# different database than the one being migrated.
# ---------------------------------------------------------------------------
log "Migrating database at $DB_PATH..."
cd "$PROD"
MAVERICK_DB_PATH="$DB_PATH" "$PROD/venv/bin/python" db/migrate.py

# ---------------------------------------------------------------------------
# 9. First deploy — fetch the current release via the normal OTA path
# ---------------------------------------------------------------------------
log "Running pull-deploy for the first release..."
rm -f "$HOME/.maverick-deployed-tag"   # force a fetch even if a stamp lingers
"$PROD/deploy/pull-deploy.sh" || die "pull-deploy failed — see the output above"

# ---------------------------------------------------------------------------
# 10. Enable services
#
# Boot order is enforced by the units' After=/Requires=:
#   mosquitto → db_writer → trip_manager → obd_poller
#                        → express_bridge → kiosk
# ---------------------------------------------------------------------------
log "Enabling services..."
sudo "$SYSTEMCTL" enable --now db_writer trip_manager obd_poller crash_detector express_bridge

# The kiosk needs a Wayland session on the in-cab display. Enable it always so
# it comes up on the next graphical boot, but only start it now if a session is
# actually present — otherwise a headless/SSH-only provisioning run would fail
# on something that is working as designed.
sudo "$SYSTEMCTL" enable kiosk
if ls /run/user/"$(id -u)"/wayland-? >/dev/null 2>&1; then
  sudo "$SYSTEMCTL" restart kiosk
  log "Kiosk started against the live Wayland session"
else
  log "No Wayland session right now — kiosk enabled, starts on next graphical boot"
fi

log "Enabling OTA timer..."
sudo "$SYSTEMCTL" enable --now pull-deploy.timer

# ---------------------------------------------------------------------------
# 11. Report
# ---------------------------------------------------------------------------
echo
log "Done. Status:"
"$SYSTEMCTL" --no-pager --plain status \
  mosquitto db_writer trip_manager obd_poller express_bridge 2>/dev/null \
  | grep -E 'Loaded:|Active:|^●|^\S+\.service' || true
echo
log "Health:   curl -s http://localhost:3000/api/health"
log "Version:  curl -s http://localhost:3000/api/version"
log "Dashboard: http://$(hostname -I | awk '{print $1}'):3000"
