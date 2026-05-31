#!/bin/bash
# deploy/kiosk-start.sh
# Launched by kiosk.service. Auto-detects the Wayland compositor socket
# so the service works regardless of whether it's wayland-0 or wayland-1.

XDG_RUNTIME_DIR=/run/user/1000

# Find whichever wayland socket the compositor created
WAYLAND_SOCK=$(ls "$XDG_RUNTIME_DIR"/wayland-? 2>/dev/null | head -1)

if [ -n "$WAYLAND_SOCK" ]; then
    export XDG_RUNTIME_DIR
    export WAYLAND_DISPLAY=$(basename "$WAYLAND_SOCK")
    OZONE=--ozone-platform=wayland
else
    # Fall back to XWayland / X11
    export DISPLAY=:0
    OZONE=--ozone-platform=x11
fi

CHROMIUM=$(command -v chromium || command -v chromium-browser)

exec "$CHROMIUM" \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-restore-session-state \
    --disable-session-crashed-bubble \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    --check-for-update-interval=31536000 \
    "$OZONE" \
    http://localhost:3000/live
