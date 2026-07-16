"""
jetson/vision_publisher.py
Maverick Telemetry Hub — Jetson Orin Nano companion

Publishes scene-change events to the Pi hub's MQTT broker over the direct
ethernet link, plus a status heartbeat. Camera capture is driven by
classifier.step() — this process never captures on its own initiative. The
temporal-gating pipeline in classifier.py is real shipped code; only the
model call is stubbed, and the bare stub confirms nothing, so /frame and
/scene stay silent until real inference (or synthetic-label mode) lands.

Publishes (all QoS 1, no retain):
- maverick/vision/status — {status, detail, ts} on connect, on change, and
  every 5s as a heartbeat. An MQTT Last Will on the same topic lets the
  broker announce "disconnected" if the cable is yanked or power is cut.
- maverick/vision/frame  — {ts, frame_id, source, width_px, height_px,
  jpeg_b64, scene_label, confidence} — ONE message per confirmed change on
  any classifier track (source: "event"); ts/frame_id are stamped at
  capture time. Labels with the "speed_limit_" prefix come from the
  speed-limit track; anything else is a scene label.
- maverick/vision/scene  — {ts, frame_id, scene_label, confidence} — the
  lightweight twin of each confirmed /frame, forwarded live to the
  dashboard by the bridge, not persisted.

Publishes ALWAYS — no trip gating. trip_open events carry no trip id and are
not retained, so a Jetson booting mid-trip could never learn a trip is
active. db_writer on the Pi attaches frames to the active trip and drops
frames that arrive outside one, exactly as it does for OBD readings.

Managed by systemd on the Jetson — see jetson/deploy/vision_publisher.service
Dev machine (no camera, local broker):
    MQTT_HOST=localhost VISION_SOURCE=test python vision_publisher.py
"""

import base64
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import cv2
import numpy as np
import paho.mqtt.client as mqtt
import classifier

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST       = os.environ.get("MQTT_HOST", "192.168.100.1")  # the Pi, over direct ethernet
MQTT_PORT       = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_BASE = "maverick/vision"

HEARTBEAT_INTERVAL_S = 5.0   # the Pi bridge treats 15s of silence as disconnected
TICK_INTERVAL_S      = 0.05  # main-loop tick; the classifier samples at most once
                             # per tick, so the sampling ceiling is 20 fps

# cv2.VideoCapture buffers ~4 frames. A read after a long gap (sparse sampling)
# would return stale pixels stamped with a fresh ts — silently breaking OBD
# alignment — so reads after a gap drain the buffer first.
CAMERA_DRAIN_AFTER_S = 0.5
CAMERA_DRAIN_GRABS   = 4

STEP_ERR_LOG_INTERVAL_S = 10.0  # rate limit for classifier.step failure logs

# auto — try the default camera, fall back to a test pattern until it works
# test — always publish a generated test pattern (dev machines, no camera)
# anything else — passed to cv2.VideoCapture verbatim (e.g. a GStreamer
#                 pipeline string for the CSI camera via nvarguscamerasrc)
VISION_SOURCE = os.environ.get("VISION_SOURCE", "auto")

FRAME_WIDTH  = 1280
FRAME_HEIGHT = 720
JPEG_QUALITY = 80  # ~100-150 KB per 720p frame

# Camera reopen backoff — mirrors obd_poller's serial backoff
INITIAL_BACKOFF = 2
MAX_BACKOFF     = 60

# Refuse to publish frames until the system clock is plausible. The Orin has
# no battery-backed RTC and boots in 1970 until chrony steps the clock from
# the Pi — epoch-1970 timestamps would poison alignment with OBD readings.
CLOCK_SANE_YEAR = 2026

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("vision_publisher")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clock_is_sane() -> bool:
    return datetime.now(timezone.utc).year >= CLOCK_SANE_YEAR

# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

def test_pattern_frame(n: int) -> np.ndarray:
    """Generated frame for camera-less dev machines: moving gradient + stamp."""
    frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    gradient = np.linspace(0, 255, FRAME_WIDTH, dtype=np.uint8)
    frame[:, :, 0] = gradient                               # static blue ramp
    frame[:, :, 1] = (gradient.astype(int) + n * 8) % 256   # slides each frame
    cv2.putText(frame, f"TEST PATTERN frame={n}", (40, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    cv2.putText(frame, utc_now_iso(), (40, 140),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return frame


def open_camera():
    """cv2.VideoCapture per VISION_SOURCE, or None (caller uses test pattern)."""
    if VISION_SOURCE == "test":
        return None
    source = 0 if VISION_SOURCE == "auto" else VISION_SOURCE
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    # Best-effort stale-frame defense; honored on most V4L2 backends, ignored
    # elsewhere. GStreamer sources need drop=true max-buffers=1 on the appsink
    # instead — see jetson/README.md.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def capture_frame(cap, frame_count: int, drain: bool):
    """Returns (frame, cap). Falls back to a test pattern if the camera fails."""
    if cap is not None:
        if drain:
            # grab() dequeues without decoding — discarding buffered stale
            # frames is nearly free, and the read() below returns fresh pixels.
            for _ in range(CAMERA_DRAIN_GRABS):
                cap.grab()
        ok, frame = cap.read()
        if ok:
            return frame, cap
        log.warning("Camera read failed — releasing, falling back to test pattern")
        cap.release()
        cap = None
    return test_pattern_frame(frame_count), cap

# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def publish_status(client: mqtt.Client, status: str, detail: str = "") -> None:
    payload = {"status": status, "detail": detail, "ts": utc_now_iso()}
    client.publish(f"{MQTT_TOPIC_BASE}/status", json.dumps(payload), qos=1)


def publish_frame(client: mqtt.Client, result: dict) -> bool:
    frame = result["frame"]
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        log.warning("JPEG encode failed — dropping frame")
        return False

    payload = {
        "ts":          result["ts"],
        "frame_id":    result["frame_id"],
        "source":      result["source"],
        "width_px":    frame.shape[1],
        "height_px":   frame.shape[0],
        "jpeg_b64":    base64.b64encode(buf).decode("ascii"),
        "scene_label": result["scene_label"],
        "confidence":  result["confidence"],
    }
    client.publish(f"{MQTT_TOPIC_BASE}/frame", json.dumps(payload), qos=1)
    return True


def publish_scene(client: mqtt.Client, result: dict) -> None:
    payload = {
        "ts":          result["ts"],
        "frame_id":    result["frame_id"],
        "scene_label": result["scene_label"],
        "confidence":  result["confidence"],
    }
    client.publish(f"{MQTT_TOPIC_BASE}/scene", json.dumps(payload), qos=1)

# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

def build_mqtt_client() -> mqtt.Client:
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="vision_publisher")
    except AttributeError:
        client = mqtt.Client(client_id="vision_publisher")  # paho-mqtt < 2.0

    # Last Will: broker-side offline detection. Unlike the Pi's localhost-only
    # processes, this client sits across a physical cable that can be yanked —
    # the broker publishes this on our behalf if we vanish without a clean
    # disconnect. ts is null because the LWT payload is frozen at connect time.
    client.will_set(
        f"{MQTT_TOPIC_BASE}/status",
        json.dumps({"status": "disconnected", "detail": "lwt: connection lost", "ts": None}),
        qos=1,
        retain=False,
    )

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected")
            if clock_is_sane():
                publish_status(client, "connected", f"source={VISION_SOURCE}")
            else:
                publish_status(client, "connecting", "waiting for clock sync")
        else:
            log.error(f"MQTT connection failed — rc={rc}")

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning(f"MQTT unexpected disconnect — rc={rc}")

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    return client

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    client = build_mqtt_client()

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.critical(f"Cannot connect to MQTT broker at {MQTT_HOST}:{MQTT_PORT}: {e}")
        sys.exit(1)

    client.loop_start()

    cap = open_camera()
    if cap is None and VISION_SOURCE != "test":
        log.warning("Camera unavailable — publishing test pattern until it returns")

    frame_count       = 0   # total captures — also animates the test pattern
    events_published  = 0
    camera_backoff    = INITIAL_BACKOFF
    next_camera_retry = 0.0
    last_heartbeat    = -HEARTBEAT_INTERVAL_S  # publish immediately
    last_capture_t    = 0.0
    last_step_err_t   = -STEP_ERR_LOG_INTERVAL_S
    clock_was_sane    = False

    def capture() -> dict:
        # Injected into classifier.step() — the only path that touches the
        # camera. ts/frame_id are minted HERE, at capture time: with slow
        # inference, capture and publish drift apart, and capture time is
        # what aligns with OBD readings.
        nonlocal cap, frame_count, last_capture_t
        drain = cap is not None and (time.monotonic() - last_capture_t) > CAMERA_DRAIN_AFTER_S
        frame, cap = capture_frame(cap, frame_count, drain)
        last_capture_t = time.monotonic()
        frame_count += 1
        return {"frame": frame, "ts": utc_now_iso(), "frame_id": uuid.uuid4().hex}

    try:
        while True:
            now = time.monotonic()

            # Heartbeat — freshness signal for the Pi bridge's staleness check
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                if clock_is_sane():
                    mode = "test" if cap is None else "camera"
                    cs = classifier.get_status()
                    tracks = " ".join(f"{n}={t['state']}/{t['label']}"
                                      for n, t in cs["tracks"].items())
                    publish_status(client, "connected",
                                   f"source={mode} {tracks} "
                                   f"captures={frame_count} events={events_published}")
                else:
                    publish_status(client, "connecting", "waiting for clock sync")
                last_heartbeat = now

            # Clock gate — status only, no frames, until the clock is plausible
            if not clock_is_sane():
                time.sleep(0.5)
                continue
            if not clock_was_sane:
                clock_was_sane = True
                log.info("System clock is sane — frame publishing enabled")

            # Camera recovery with backoff (only when a real camera is wanted)
            if cap is None and VISION_SOURCE != "test" and now >= next_camera_retry:
                cap = open_camera()
                if cap is not None:
                    log.info("Camera reopened")
                    camera_backoff = INITIAL_BACKOFF
                else:
                    next_camera_retry = now + camera_backoff
                    camera_backoff = min(camera_backoff * 2, MAX_BACKOFF)

            # Tick the classifier — it decides whether to capture/sample now.
            # Outer guard: nothing coming out of step() may kill the process.
            try:
                results = classifier.step(capture)
            except Exception as e:
                if time.monotonic() - last_step_err_t >= STEP_ERR_LOG_INTERVAL_S:
                    log.warning(f"classifier.step failed — continuing: {e}")
                    last_step_err_t = time.monotonic()
                results = []
            for result in results:
                if publish_frame(client, result):
                    events_published += 1
                # Scene publishes even if the JPEG encode failed — the
                # confirmation happened; persistence and live-UI are
                # independent consumers.
                publish_scene(client, result)

            time.sleep(TICK_INTERVAL_S)
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        publish_status(client, "disconnected", "clean shutdown")
        if cap is not None:
            cap.release()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    log.info("vision_publisher starting")
    run()
