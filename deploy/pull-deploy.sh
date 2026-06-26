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

# Derive owner/repo from the git remote so this script needs no hardcoded values
REPO=$(git -C "$PROD" remote get-url origin \
  | sed -E 's|.*github\.com[:/]||' \
  | sed 's|\.git$||')

API="https://api.github.com/repos/$REPO/releases/latest"
AUTH_HEADER=""
if [ -n "${GITHUB_TOKEN:-}" ]; then
  AUTH_HEADER="Authorization: Bearer $GITHUB_TOKEN"
fi

# ---------------------------------------------------------------------------
# Check for a new release
# ---------------------------------------------------------------------------
RELEASE=$(curl -sf ${AUTH_HEADER:+-H "$AUTH_HEADER"} "$API") || {
  echo "[pull-deploy] Failed to fetch release info from $API" >&2
  exit 1
}

LATEST_TAG=$(echo "$RELEASE" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
DEPLOYED_TAG=$(cat "$TAG_FILE" 2>/dev/null || echo "none")

if [ "$LATEST_TAG" = "$DEPLOYED_TAG" ]; then
  echo "[pull-deploy] Already at $LATEST_TAG — nothing to do"
  exit 0
fi

echo "[pull-deploy] New release: $LATEST_TAG (was: $DEPLOYED_TAG)"

# ---------------------------------------------------------------------------
# Download release assets
# ---------------------------------------------------------------------------
get_asset_url() {
  local name="$1"
  echo "$RELEASE" | python3 -c "
import sys, json
for a in json.load(sys.stdin)['assets']:
    if a['name'] == '$name':
        print(a['browser_download_url'])
        break
"
}

BINARY_URL=$(get_asset_url "maverick-telemetry")
DIST_URL=$(get_asset_url "client-dist.tar.gz")

[ -z "$BINARY_URL" ] && { echo "[pull-deploy] Missing asset: maverick-telemetry" >&2; exit 1; }
[ -z "$DIST_URL"   ] && { echo "[pull-deploy] Missing asset: client-dist.tar.gz" >&2; exit 1; }

curl -fL ${AUTH_HEADER:+-H "$AUTH_HEADER"} -o "$WORK_DIR/maverick-telemetry" "$BINARY_URL"
curl -fL ${AUTH_HEADER:+-H "$AUTH_HEADER"} -o "$WORK_DIR/client-dist.tar.gz"  "$DIST_URL"

# ---------------------------------------------------------------------------
# Apply — update Python/server files from git, then overlay build artifacts
# ---------------------------------------------------------------------------
cd "$PROD"

git fetch origin main
git checkout origin/main -- \
  obd_poller.py trip_manager.py db_writer.py db/ server/ deploy/

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

cd server && npm install --omit=dev && cd ..

sudo systemctl restart express_bridge kiosk

echo "$LATEST_TAG" > "$TAG_FILE"
echo "[pull-deploy] Deployed $LATEST_TAG successfully"
