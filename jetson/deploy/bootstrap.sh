#!/bin/bash
# jetson/deploy/bootstrap.sh
#
# Takes a fresh Jetson Orin Nano to a running vision companion, then hands
# ongoing updates to the pull-deploy timer — the Jetson half of the Pi's
# deploy/bootstrap.sh.
#
# ONE-LINER ON A BARE JETSON:
#   sudo apt install -y git \
#     && git clone https://github.com/AlexTs-dev/maverick-telemetry-hub.git ~/maverick-telemetry-hub \
#     && ~/maverick-telemetry-hub/jetson/deploy/bootstrap.sh
#
# If the Jetson reaches the Pi over WiFi rather than the direct ethernet cable,
# point it at the Pi's LAN address:
#   MAVERICK_PI_HOST=10.0.0.18 ~/maverick-telemetry-hub/jetson/deploy/bootstrap.sh
#
# Idempotent: safe to re-run at any time.

set -euo pipefail

PROD="$HOME/maverick-telemetry-hub"
JETSON_DIR="$PROD/jetson"
SVC_USER="$(id -un)"
SYSTEMCTL="$(command -v systemctl)"

# The Pi's address, used for BOTH time sync and MQTT. Defaults to the direct
# ethernet link documented in jetson/README.md.
PI_HOST="${MAVERICK_PI_HOST:-192.168.100.1}"

log() { echo "[bootstrap] $*"; }
die() { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as the service user (e.g. jetson), not root — sudo is used where needed."
[ -d "$JETSON_DIR" ] || die "Repo not found at $PROD. Clone it first (see the header)."

log "User=$SVC_USER  Repo=$PROD  Pi=$PI_HOST"

# ---------------------------------------------------------------------------
# 1. System packages
#
# Deliberately minimal. JetPack already provides CUDA-accelerated OpenCV and
# numpy system-wide; installing them again from pip would shadow the
# accelerated builds with CPU-only wheels.
# ---------------------------------------------------------------------------
log "Installing system packages (apt)..."
sudo apt-get update -qq
sudo apt-get install -y -qq git curl python3 python3-venv chrony

# OpenCV. vision_publisher hard-requires cv2, and a minimal JetPack image does
# not always have it, so install rather than warn.
#
# Prefer NVIDIA's nvidia-opencv (the CUDA-accelerated JetPack build) over
# Ubuntu's python3-opencv, which is CPU-only — decoding camera frames on the CPU
# is exactly what the Orin is here to avoid. Never `pip install opencv-python`:
# that CPU-only wheel would shadow the accelerated build inside the venv.
# Prefer NVIDIA's JetPack OpenCV (libopencv / libopencv-python) over Ubuntu's
# python3-opencv. NVIDIA's build carries the Jetson GStreamer integration the
# CSI camera pipeline needs (nvarguscamerasrc / NVMM); Ubuntu's generic build
# does not.
#
# Attempt the install and fall back only on genuine failure. An `apt-cache
# policy` pre-check is unreliable: the NVIDIA repo lists are fetched over the
# network, and a transient failure during `apt-get update` makes the package
# look permanently unavailable.
if sudo apt-get install -y -qq nvidia-opencv libopencv-python 2>/dev/null; then
  # Ubuntu's python3-opencv installs cv2 into /usr/lib/python3/dist-packages and
  # SHADOWS NVIDIA's bindings, so `import cv2` silently keeps returning the
  # generic build even after NVIDIA's is installed. Remove it — but only when
  # nothing else depends on it, and only once NVIDIA's is confirmed working.
  if dpkg -s python3-opencv >/dev/null 2>&1; then
    if [ -z "$(apt-cache rdepends --installed python3-opencv 2>/dev/null | sed -n '2,$p' | tr -d ' \n')" ]; then
      log "Removing Ubuntu python3-opencv (it shadows NVIDIA's cv2 bindings)"
      sudo apt-get remove -y -qq python3-opencv
    else
      log "WARN: python3-opencv has dependents; leaving it (cv2 may be the generic build)"
    fi
  fi
  log "Installed NVIDIA JetPack OpenCV"
elif python3 -c "import cv2" 2>/dev/null; then
  log "NVIDIA OpenCV unavailable; an OpenCV is already present - keeping it"
else
  log "NVIDIA OpenCV unavailable - falling back to Ubuntu's python3-opencv"
  sudo apt-get install -y -qq python3-opencv
fi

# GStreamer + PyGObject — the dashcam recording path.
#
# recorder.py drives a tee'd pipeline directly through gi rather than through
# cv2.VideoCapture, because only the native API exposes splitmuxsink's
# format-location-full (exact per-segment filenames) and split-now (forcing a
# cut at a trip boundary or a crash). cv2's GStreamer backend gives neither.
#
# Element homes, for when one of these turns out to be missing:
#   splitmuxsink, mp4mux  -> plugins-good      h264parse -> plugins-bad
#   videoconvert, appsink -> plugins-base      nvv4l2h264enc -> JetPack (nvidia-l4t-gstreamer)
#
# NON-FATAL by design. If any of this is unavailable the recorder refuses to
# start and camera.py falls back to cv2.VideoCapture — inference keeps running.
# A dashcam that cannot record is a degraded system; a Jetson that no longer
# detects speed limit signs is a broken one.
log "Installing GStreamer + PyGObject (dashcam recording)..."
sudo apt-get install -y -qq \
  python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  v4l-utils \
  || log "WARN: GStreamer/PyGObject install failed — recording will stay disabled"

# ---------------------------------------------------------------------------
# 2. Time sync from the Pi
#
# Load-bearing: the Orin has no battery-backed RTC and boots in 1970, and
# vision_publisher refuses to publish frames until the clock looks sane.
# Templated from PI_HOST so the direct-link and WiFi topologies both work.
# ---------------------------------------------------------------------------
log "Configuring chrony to sync from $PI_HOST..."
sudo tee /etc/chrony/conf.d/maverick.conf >/dev/null <<EOF
# Managed by jetson/deploy/bootstrap.sh — see jetson/deploy/chrony-jetson.conf.
# The truck has no internet; the Pi is the ONLY time source. Frame/reading
# alignment depends on these clocks agreeing.
#
# makestep 1.0 -1: step the clock at ANY offset on EVERY update, not just the
# first three — the Orin boots in 1970 and must catch up hard.
server $PI_HOST iburst minpoll 4 maxpoll 6
makestep 1.0 -1
EOF
sudo "$SYSTEMCTL" restart chrony || log "WARN: chrony restart failed — check 'systemctl status chrony'"

# ---------------------------------------------------------------------------
# 3. Python environment
#
# --system-site-packages is REQUIRED: it is what lets the venv see JetPack's
# CUDA OpenCV. paho-mqtt is the only thing installed into the venv itself.
# Do not install jetson/requirements.txt here — that file is for dev machines.
# ---------------------------------------------------------------------------
if [ ! -x "$JETSON_DIR/venv/bin/python" ]; then
  log "Creating Python venv (--system-site-packages)..."
  python3 -m venv --system-site-packages "$JETSON_DIR/venv"
fi
log "Installing paho-mqtt..."
"$JETSON_DIR/venv/bin/pip" install --quiet --upgrade pip
"$JETSON_DIR/venv/bin/pip" install --quiet --upgrade paho-mqtt

# Confirm the venv can actually see OpenCV. This is the check that catches a
# venv accidentally created without --system-site-packages, which is the usual
# reason vision_publisher dies on import.
#
# Note on CUDA: stock JetPack OpenCV is NOT built with the cv2.cuda module, so
# getCudaEnabledDeviceCount() returning 0 is normal and not a fault. It does not
# affect this system — vision_publisher uses cv2 only for capture and JPEG
# encode (hardware paths come from GStreamer/NVMM) and never calls cv2.cuda.*.
# Only a hand-built OpenCV with -DWITH_CUDA=ON reports otherwise.
if "$JETSON_DIR/venv/bin/python" -c "import cv2" 2>/dev/null; then
  CV_INFO=$("$JETSON_DIR/venv/bin/python" -c \
    "import cv2; print(cv2.__version__, cv2.__file__)" 2>/dev/null || echo "unknown")
  log "OpenCV visible to the venv: $CV_INFO"
else
  log "WARN: cv2 not importable from the venv — vision_publisher will not start."
  log "      Recreate the venv with --system-site-packages (see jetson/README.md)."
fi

# ---------------------------------------------------------------------------
# 4. Dashcam storage
#
# Point MAVERICK_DASHCAM_ROOT at the roomiest filesystem — an NVMe in the M.2
# slot if there is one. At 1080p30 / 8 Mbps footage lands at ~3.6 GB/hour, so
# 30 days of everyday driving is well over 100 GB and the byte budget, not the
# age limit, is what will actually bind on a small disk.
#
# Owned by the service user: vision_publisher (via recorder.py) is the only
# writer, and clip_server.py gets it ReadOnlyPaths= in its unit.
# ---------------------------------------------------------------------------
DASHCAM_ROOT="${MAVERICK_DASHCAM_ROOT:-/var/lib/maverick-dashcam}"
log "Creating dashcam clip root at $DASHCAM_ROOT..."
sudo install -d -o "$SVC_USER" -g "$SVC_USER" -m 755 "$DASHCAM_ROOT" "$DASHCAM_ROOT/clips"

DASHCAM_AVAIL_KB="$(df -Pk "$DASHCAM_ROOT" | awk 'NR==2 {print $4}')"
log "Dashcam root has $(( DASHCAM_AVAIL_KB / 1024 / 1024 )) GiB available"

# ---------------------------------------------------------------------------
# 5. Per-device environment
#
# Not in git, so device-specific settings survive every pull-deploy. The
# vision_publisher unit reads this after its baked-in Environment= lines.
#
# VISION_RECORD_ENABLED lives here rather than in the unit on purpose: the
# shipped default is off, so landing the recording code on a device never
# changes its behaviour until someone deliberately turns it on here.
# ---------------------------------------------------------------------------
log "Writing ~/.maverick-env (MQTT_HOST=$PI_HOST)..."
cat > "$HOME/.maverick-env" <<EOF
# Maverick Vision — per-device overrides. Not in git; survives pull-deploy.
MQTT_HOST=$PI_HOST

# Dashcam. Set VISION_RECORD_ENABLED=1 to start recording on this device.
VISION_RECORD_ENABLED=${MAVERICK_RECORD_ENABLED:-0}
MAVERICK_DASHCAM_ROOT=$DASHCAM_ROOT
EOF

# ---------------------------------------------------------------------------
# 6. Passwordless restart for unattended deploys
# ---------------------------------------------------------------------------
log "Installing sudoers rule..."
sudo tee /etc/sudoers.d/maverick >/dev/null <<EOF
# Maverick Vision — unattended service restart for pull-deploy.
# Installed by jetson/deploy/bootstrap.sh. Scoped to these exact commands.
$SVC_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart vision_publisher
$SVC_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart clip_server
EOF
sudo chmod 440 /etc/sudoers.d/maverick
# A malformed sudoers file can lock the user out of sudo entirely.
sudo visudo -c -f /etc/sudoers.d/maverick >/dev/null || {
  sudo rm -f /etc/sudoers.d/maverick
  die "sudoers validation failed — removed the bad file"
}

# ---------------------------------------------------------------------------
# 7. systemd units
# ---------------------------------------------------------------------------
log "Installing systemd units..."
sudo install -m 644 "$JETSON_DIR"/deploy/vision_publisher.service /etc/systemd/system/
sudo install -m 644 "$JETSON_DIR"/deploy/clip_server.service      /etc/systemd/system/
sudo install -m 644 "$JETSON_DIR"/deploy/pull-deploy.service      /etc/systemd/system/
sudo install -m 644 "$JETSON_DIR"/deploy/pull-deploy.timer        /etc/systemd/system/

# The clip server's ReadOnlyPaths= has to name the real clip root, which is a
# per-device path — patch it in rather than baking a guess into the unit.
sudo mkdir -p /etc/systemd/system/clip_server.service.d
sudo tee /etc/systemd/system/clip_server.service.d/paths.conf >/dev/null <<EOF
# Managed by jetson/deploy/bootstrap.sh — per-device clip root.
[Service]
Environment=MAVERICK_DASHCAM_ROOT=$DASHCAM_ROOT
ReadOnlyPaths=$DASHCAM_ROOT
EOF

sudo "$SYSTEMCTL" daemon-reload

# ---------------------------------------------------------------------------
# 8. First deploy — code at the latest release tag, plus model weights
#
# Runs the normal OTA path so bootstrap and OTA can never drift apart. Model
# weights are optional: vision_publisher logs one warning and runs the scene
# track only when none are present.
# ---------------------------------------------------------------------------
log "Running pull-deploy for the first release..."
rm -f "$HOME/.maverick-deployed-tag"
"$JETSON_DIR/deploy/pull-deploy.sh" || die "pull-deploy failed — see the output above"

# ---------------------------------------------------------------------------
# 9. Enable services
# ---------------------------------------------------------------------------
log "Enabling services..."
sudo "$SYSTEMCTL" enable --now vision_publisher
sudo "$SYSTEMCTL" enable --now clip_server
sudo "$SYSTEMCTL" enable --now pull-deploy.timer

# ---------------------------------------------------------------------------
# 10. Report
# ---------------------------------------------------------------------------
echo
log "Done. Status:"
for unit in vision_publisher clip_server; do
  "$SYSTEMCTL" --no-pager --plain status "$unit" 2>/dev/null \
    | grep -E 'Loaded:|Active:' || true
done
echo
log "Clock:   chronyc tracking            (Reference ID should be $PI_HOST)"
log "Logs:    journalctl -u vision_publisher -f"
log "OTA:     journalctl -u pull-deploy -f"
log "Models:  $JETSON_DIR/models  (publish with tools/publish-models.sh)"
log "Footage: $DASHCAM_ROOT/clips  (served read-only on :8088)"
echo
if grep -q '^VISION_RECORD_ENABLED=0' "$HOME/.maverick-env"; then
  log "Recording is OFF on this device. To enable it:"
  log "  sed -i 's/^VISION_RECORD_ENABLED=0/VISION_RECORD_ENABLED=1/' ~/.maverick-env"
  log "  sudo systemctl restart vision_publisher"
fi
