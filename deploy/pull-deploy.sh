#!/bin/bash
# deploy/pull-deploy.sh
#
# Polls the GitHub releases API for a new release. If one is found,
# downloads the Tauri binary and React dist, applies them, and restarts
# services. Run via pull-deploy.timer every 2 minutes.
#
# The Pi only needs outbound HTTPS access to GitHub — no inbound SSH,
# no self-hosted runner.

set -euo pipefail

PROD="$HOME/maverick-telemetry-hub"
TAG_FILE="$HOME/.maverick-deployed-tag"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# shellcheck source=lib/release.sh
. "$PROD/deploy/lib/release.sh"

REPO=$(mav_repo_slug "$PROD")

# ---------------------------------------------------------------------------
# Check for a new release
# ---------------------------------------------------------------------------
RELEASE=$(mav_fetch_release "$REPO" "latest") || {
  echo "[pull-deploy] Failed to fetch latest release for $REPO" >&2
  exit 1
}

LATEST_TAG=$(printf '%s' "$RELEASE" | mav_json_field tag_name)
DEPLOYED_TAG=$(cat "$TAG_FILE" 2>/dev/null || echo "none")

[ -n "$LATEST_TAG" ] || { echo "[pull-deploy] Could not read tag_name from release JSON" >&2; exit 1; }

if [ "$LATEST_TAG" = "$DEPLOYED_TAG" ]; then
  echo "[pull-deploy] Already at $LATEST_TAG — nothing to do"
  exit 0
fi

echo "[pull-deploy] New release: $LATEST_TAG (was: $DEPLOYED_TAG)"

# ---------------------------------------------------------------------------
# Download release assets
# ---------------------------------------------------------------------------
BINARY_URL=$(mav_asset_url "$RELEASE" "maverick-telemetry")
DIST_URL=$(mav_asset_url "$RELEASE" "client-dist.tar.gz")

[ -z "$BINARY_URL" ] && { echo "[pull-deploy] Missing asset: maverick-telemetry" >&2; exit 1; }
[ -z "$DIST_URL"   ] && { echo "[pull-deploy] Missing asset: client-dist.tar.gz" >&2; exit 1; }

mav_download "$BINARY_URL" "$WORK_DIR/maverick-telemetry"
mav_download "$DIST_URL"   "$WORK_DIR/client-dist.tar.gz"

# ---------------------------------------------------------------------------
# Apply — update Python/server files from git, then overlay build artifacts
#
# Check out the RELEASE TAG, not origin/main: a commit landing between the
# release build and this poll would otherwise pair newer Python/server source
# with the older binary we just downloaded. The tag pins both to one commit.
# ---------------------------------------------------------------------------
cd "$PROD"

git fetch origin main --tags --force
# NOTE: the root-level Python files are an explicit allow-list, not a glob — a
# new *.py at the repo root is NOT deployed unless it is named here.
git checkout "$LATEST_TAG" -- \
  obd_poller.py trip_manager.py db_writer.py crash_detector.py db/ server/ deploy/

# Tauri binary
mkdir -p client/src-tauri/target/release
install -m 755 "$WORK_DIR/maverick-telemetry" \
               client/src-tauri/target/release/maverick-telemetry

# React build
mkdir -p client/dist
tar -xzf "$WORK_DIR/client-dist.tar.gz" -C client/dist --overwrite

# ---------------------------------------------------------------------------
# Kiosk display profile — install the version-controlled kanshi config so the
# DSI panel's rotation/mode is managed through git and survives a reimage.
# kanshi reads ~/.config/kanshi/config (labwc-pi only creates an empty one) and
# applies it on every boot. kanshi 1.5 has no reload IPC, so we also apply it
# live with wlr-randr when a Wayland session is up. Every step here is
# non-fatal: a headless or sessionless deploy must never fail the deploy.
# ---------------------------------------------------------------------------
install_display_config() {
  local src="$PROD/deploy/kanshi.config"
  local cfg_dir="$HOME/.config/kanshi"
  [ -f "$src" ] || return 0
  mkdir -p "$cfg_dir"
  if cmp -s "$src" "$cfg_dir/config" && cmp -s "$src" "$cfg_dir/config.init"; then
    return 0                                   # both already current — nothing to do
  fi
  if ! install -m 644 "$src" "$cfg_dir/config"; then
    echo "[pull-deploy] WARN: could not install kanshi display config" >&2
    return 0
  fi
  cp "$src" "$cfg_dir/config.init" || true     # re-assert the GUI baseline snapshot too
  echo "[pull-deploy] Installed managed kanshi display config"

  # Best-effort live apply; kanshi reapplies from the config on next boot.
  local rt="/run/user/$(id -u)"
  local sock
  sock="$(ls "$rt"/wayland-? 2>/dev/null | head -1 || true)"
  [ -n "$sock" ] || return 0
  command -v wlr-randr >/dev/null 2>&1 || return 0
  local out tf
  out="$(grep -oE 'output [A-Za-z0-9-]+' "$src" | awk '{print $2}' | head -1 || true)"
  tf="$(grep -oE 'transform [a-z0-9-]+' "$src" | awk '{print $2}' | head -1 || true)"
  [ -n "$out" ] && [ -n "$tf" ] || return 0
  # `timeout` bounds the call: a wedged/stale-socket compositor would otherwise
  # block wlr-randr forever, hanging this oneshot and (via the timer's
  # OnUnitActiveSec) freezing all future deploys. `env` is needed because
  # `timeout` doesn't parse leading VAR=val assignments; `|| true` absorbs both
  # wlr-randr errors and timeout's exit 124.
  timeout 5 env XDG_RUNTIME_DIR="$rt" WAYLAND_DISPLAY="$(basename "$sock")" \
    wlr-randr --output "$out" --transform "$tf" >/dev/null 2>&1 || true
}
install_display_config || true

# ---------------------------------------------------------------------------
# Post-deploy
# ---------------------------------------------------------------------------
source venv/bin/activate
# Migrate the LIVE database. MAVERICK_DB_PATH must match the value in the
# *.service files — otherwise migrate.py falls back to its repo-relative
# default and the running services never see the schema changes.
MAVERICK_DB_PATH=/home/pi/maverick_telemetry.db python db/migrate.py

# Retry npm install: better-sqlite3 downloads a prebuilt arm64 binary via
# prebuild-install, and on a slow link that download times out — after which it
# falls back to compiling with node-gyp, which is broken on Debian 13
# ("ModuleNotFoundError: No module named 'gyp'"). A transient timeout would
# otherwise fail the whole deploy, and flaky connectivity is the normal case
# for a vehicle that only sees WiFi in the driveway.
cd server
npm_ok=0
for attempt in 1 2 3; do
  if npm install --omit=dev; then npm_ok=1; break; fi
  echo "[pull-deploy] npm install failed (attempt $attempt/3) — retrying in $((attempt * 10))s" >&2
  sleep $((attempt * 10))
done
cd ..
[ "$npm_ok" -eq 1 ] || { echo "[pull-deploy] npm install failed after 3 attempts" >&2; exit 1; }

sudo systemctl restart express_bridge kiosk

echo "$LATEST_TAG" > "$TAG_FILE"
echo "[pull-deploy] Deployed $LATEST_TAG successfully"
