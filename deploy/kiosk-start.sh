#!/bin/bash
# deploy/kiosk-start.sh
# Launched by kiosk.service. Auto-detects the Wayland compositor socket
# so the service works regardless of whether it's wayland-0 or wayland-1,
# then launches the Tauri dashboard binary.

XDG_RUNTIME_DIR=/run/user/1000

WAYLAND_SOCK=$(ls "$XDG_RUNTIME_DIR"/wayland-? 2>/dev/null | head -1)

if [ -n "$WAYLAND_SOCK" ]; then
    export XDG_RUNTIME_DIR
    export WAYLAND_DISPLAY=$(basename "$WAYLAND_SOCK")
else
    export DISPLAY=:0
fi

exec /home/pi/maverick-telemetry-hub/client/src-tauri/target/release/maverick-telemetry
