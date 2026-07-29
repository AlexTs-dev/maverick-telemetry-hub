#!/bin/bash
# jetson/deploy/pull-deploy.sh
#
# The Jetson's over-the-air update, the counterpart to the Pi's
# deploy/pull-deploy.sh. Run via pull-deploy.timer every 2 minutes.
#
# Like the Pi, the Jetson only needs OUTBOUND HTTPS to GitHub — no inbound SSH
# and no self-hosted runner. In the truck it may have no internet at all; every
# failure path here is non-fatal so a offline poll simply leaves the running
# vision stack untouched.
#
# TWO INDEPENDENT TRACKS
#   1. CODE   — jetson/ checked out at the latest `deploy-*` release tag. The
#               Jetson runs pure Python, so there is no build artifact to fetch;
#               the tag is the version marker, exactly as on the Pi.
#   2. MODELS — trained *.pt weights from the fixed `models-latest` release.
#               These are gitignored (they're trained, not built — CI cannot
#               regenerate them), so they travel as release assets published by
#               tools/publish-models.sh.
#
# The tracks are deliberately independent: retraining ships new weights without
# a code commit, and a code release doesn't reship 8 MB of identical weights.

set -euo pipefail

PROD="$HOME/maverick-telemetry-hub"
JETSON_DIR="$PROD/jetson"
TAG_FILE="$HOME/.maverick-deployed-tag"
MODELS_STAMP_FILE="$HOME/.maverick-models-stamp"
MODELS_TAG="models-latest"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# shellcheck source=../../deploy/lib/release.sh
. "$PROD/deploy/lib/release.sh"

REPO=$(mav_repo_slug "$PROD")
CHANGED=0

# ---------------------------------------------------------------------------
# Track 1 — code
# ---------------------------------------------------------------------------
RELEASE=$(mav_fetch_release "$REPO" "latest") || {
  echo "[pull-deploy] Failed to fetch latest release for $REPO" >&2
  exit 1
}

LATEST_TAG=$(printf '%s' "$RELEASE" | mav_json_field tag_name)
DEPLOYED_TAG=$(cat "$TAG_FILE" 2>/dev/null || echo "none")

[ -n "$LATEST_TAG" ] || { echo "[pull-deploy] Could not read tag_name from release JSON" >&2; exit 1; }

if [ "$LATEST_TAG" = "$DEPLOYED_TAG" ]; then
  echo "[pull-deploy] Code already at $LATEST_TAG"
else
  echo "[pull-deploy] New release: $LATEST_TAG (was: $DEPLOYED_TAG)"
  cd "$PROD"
  git fetch origin main --tags --force

  # Only jetson/ and the shared deploy lib — the Jetson has no use for the
  # client, server or Pi-side Python, and checking them out would just create
  # dead files to confuse anyone debugging on-device.
  #
  # deploy/lib/ is added conditionally: `git checkout <tag> -- <path>` fails
  # outright ("pathspec did not match") if the path is absent from that tag, so
  # a hardcoded list would break any rollback to a tag predating the shared lib
  # — and would take the whole deploy down with it, not just skip the path.
  CHECKOUT_PATHS=(jetson/)
  if git rev-parse --verify --quiet "$LATEST_TAG:deploy/lib" >/dev/null; then
    CHECKOUT_PATHS+=(deploy/lib/)
  else
    echo "[pull-deploy] Tag $LATEST_TAG predates deploy/lib — keeping the local copy"
  fi
  git checkout "$LATEST_TAG" -- "${CHECKOUT_PATHS[@]}"

  CHANGED=1
fi

# ---------------------------------------------------------------------------
# Track 2 — models
#
# The models release keeps ONE tag forever, so unlike a deploy-<sha> tag the tag
# name says nothing about whether the payload changed. Fingerprint the assets
# (name:size:updated_at) and re-download only when that fingerprint moves —
# otherwise every 2-minute poll would pull ~8 MB.
# ---------------------------------------------------------------------------
sync_models() {
  local release stamp deployed_stamp url name

  release=$(mav_fetch_release "$REPO" "tags/$MODELS_TAG") || {
    echo "[pull-deploy] No '$MODELS_TAG' release yet — skipping models."
    echo "[pull-deploy] Publish with tools/publish-models.sh; vision runs without weights."
    return 0
  }

  stamp=$(mav_assets_stamp "$release")
  [ -n "$stamp" ] || { echo "[pull-deploy] '$MODELS_TAG' has no assets — skipping models."; return 0; }

  deployed_stamp=$(cat "$MODELS_STAMP_FILE" 2>/dev/null || echo "")
  if [ "$stamp" = "$deployed_stamp" ]; then
    echo "[pull-deploy] Models already current"
    return 0
  fi

  echo "[pull-deploy] New model weights detected"
  mkdir -p "$JETSON_DIR/models"

  # Download every asset to the work dir first, then move into place, so an
  # interrupted download can never leave a half-written .pt that YOLO would
  # fail to load on the next start.
  local names
  names=$(printf '%s' "$release" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assets', []):
    print(a['name'])
")

  while IFS= read -r name; do
    [ -n "$name" ] || continue
    case "$name" in
      *.pt|*.onnx) ;;
      *) echo "[pull-deploy] Skipping non-model asset $name"; continue ;;
    esac
    url=$(mav_asset_url "$release" "$name")
    [ -n "$url" ] || continue
    echo "[pull-deploy] Downloading $name"
    mav_download "$url" "$WORK_DIR/$name"
    install -m 644 "$WORK_DIR/$name" "$JETSON_DIR/models/$name"
  done <<< "$names"

  # A .engine is a TensorRT build of a .pt, tied to this exact GPU + TensorRT
  # version. New weights invalidate the old engines, and a stale engine would
  # silently keep serving the PREVIOUS model — so retire them and let
  # models/README.md's export step rebuild.
  local stale
  stale=$(find "$JETSON_DIR/models" -maxdepth 1 -name '*.engine' 2>/dev/null || true)
  if [ -n "$stale" ]; then
    while IFS= read -r e; do
      [ -n "$e" ] || continue
      mv "$e" "$e.stale"
      echo "[pull-deploy] Retired stale engine $(basename "$e") — rebuild per jetson/models/README.md"
    done <<< "$stale"
  fi

  printf '%s' "$stamp" > "$MODELS_STAMP_FILE"
  CHANGED=1
}
sync_models || echo "[pull-deploy] WARN: model sync failed, continuing" >&2

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
if [ "$CHANGED" -eq 0 ]; then
  echo "[pull-deploy] Nothing to do"
  exit 0
fi

# Keep the venv in step with requirements. The venv is built with
# --system-site-packages so JetPack's CUDA-accelerated OpenCV/numpy are used;
# only paho-mqtt is installed into it (see jetson/README.md). Installing
# jetson/requirements.txt here would shadow those with CPU-only wheels.
if [ -x "$JETSON_DIR/venv/bin/pip" ]; then
  "$JETSON_DIR/venv/bin/pip" install --quiet --upgrade paho-mqtt || \
    echo "[pull-deploy] WARN: paho-mqtt refresh failed (offline?), continuing" >&2
fi

# vision_publisher owns the camera and the recorder; clip_server only reads the
# clip directory, so restarting it is cheap and cannot lose footage. Restart it
# first so a browser mid-playback reconnects to the new build before the
# recorder churns.
#
# clip_server may not exist yet on a Jetson provisioned before the dashcam
# landed — bootstrap.sh installs it, but a device that has not re-bootstrapped
# still needs vision_publisher restarted, so this must not be fatal.
sudo systemctl restart clip_server 2>/dev/null || \
  echo "[pull-deploy] NOTE: clip_server not installed — re-run jetson/deploy/bootstrap.sh" >&2
sudo systemctl restart vision_publisher

echo "$LATEST_TAG" > "$TAG_FILE"
echo "[pull-deploy] Deployed $LATEST_TAG successfully"
