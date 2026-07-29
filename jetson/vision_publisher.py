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

Also drives the dashcam (see recorder.py) when VISION_RECORD_ENABLED=1, and
publishes its metadata — never its bytes:
- maverick/dashcam/clip   — one message per closed segment {clip_id, started_at,
  ended_at, duration_s, size_bytes, width_px, height_px, fps, path}. The Pi
  matches it to a trip by timestamp overlap and streams the file back from
  clip_server.py on demand; footage itself never crosses MQTT.
- maverick/dashcam/status — storage/recording heartbeat alongside the vision one.
- maverick/dashcam/pruned — clip_ids the retention pruner deleted, so db_writer
  can drop the matching rows.

Subscribes:
- maverick/dashcam/command      — delete / protect / unprotect from the dashboard.
- maverick/telemetry/crash_event — on a potential crash, cut the segment
  immediately and self-protect a window around it, so incident footage survives
  even if db_writer is down.

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
from datetime import datetime, timedelta, timezone

import cv2
import paho.mqtt.client as mqtt
import camera
import classifier
import recorder
from clipstore import is_valid_clip_id, parse_iso

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST       = os.environ.get("MQTT_HOST", "192.168.100.1")  # the Pi, over direct ethernet
MQTT_PORT       = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC_BASE = "maverick/vision"

MQTT_DASHCAM_BASE   = "maverick/dashcam"
MQTT_TELEMETRY_BASE = "maverick/telemetry"

HEARTBEAT_INTERVAL_S = 5.0   # the Pi bridge treats 15s of silence as disconnected
TICK_INTERVAL_S      = 0.05  # main-loop tick; the classifier samples at most once
                             # per tick, so the sampling ceiling is 20 fps

STEP_ERR_LOG_INTERVAL_S = 10.0  # rate limit for classifier.step failure logs

JPEG_QUALITY = 80  # ~100-150 KB per 720p frame

# Camera capture, source selection and the test pattern live in camera.py — it
# arbitrates between the recorder's appsink and cv2.VideoCapture so only one
# owner ever touches the device.
VISION_SOURCE = camera.VISION_SOURCE

# How much footage either side of a crash the Jetson protects on its own, before
# the Pi's authoritative trip-scoped window arrives. Deliberately generous: an
# over-retained clip costs disk, an under-retained one is gone forever.
CRASH_SELF_PROTECT_S = 120

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
# Dashcam publishing
#
# Metadata only. Footage is served over HTTP by clip_server.py and proxied by
# the Pi — putting video bytes on MQTT would flood the bridge's ring buffer and
# every connected WebSocket client, which is exactly why the bridge refuses to
# subscribe to maverick/vision/frame.
# ---------------------------------------------------------------------------

def publish_clip(client: mqtt.Client, clip) -> None:
    payload = {**clip.as_payload(), "ts": utc_now_iso()}
    client.publish(f"{MQTT_DASHCAM_BASE}/clip", json.dumps(payload), qos=1)
    log.info(f"Clip closed: {clip.clip_id} ({clip.duration_s:.1f}s, "
             f"{clip.size_bytes / 2**20:.1f} MiB)")


def publish_pruned(client: mqtt.Client, clip_ids: list, reason: str) -> None:
    payload = {"clip_ids": clip_ids, "reason": reason, "ts": utc_now_iso()}
    client.publish(f"{MQTT_DASHCAM_BASE}/pruned", json.dumps(payload), qos=1)


def publish_dashcam_status(client: mqtt.Client, rec, detail: str = "") -> None:
    payload = {**rec.stats(), "detail": detail, "ts": utc_now_iso()}
    payload["status"] = "recording" if payload.get("recording") else "idle"
    client.publish(f"{MQTT_DASHCAM_BASE}/status", json.dumps(payload), qos=1)


def publish_command_result(client: mqtt.Client, request_id, action: str,
                           clip_ids: list, ok: bool, error: str = None) -> None:
    payload = {
        "request_id": request_id, "action": action, "clip_ids": clip_ids,
        "ok": ok, "error": error, "ts": utc_now_iso(),
    }
    client.publish(f"{MQTT_DASHCAM_BASE}/command_result", json.dumps(payload), qos=1)

# ---------------------------------------------------------------------------
# Dashcam command handling
# ---------------------------------------------------------------------------

def handle_dashcam_command(client: mqtt.Client, rec, payload: dict) -> None:
    """delete / protect / unprotect, issued by the Pi's bridge or db_writer.

    Everything here arrives from an anonymous broker, so clip ids are validated
    against the strict clip_id grammar before they are allowed anywhere near a
    filesystem path — the same discipline db_writer applies to vision frames.
    """
    action = str(payload.get("action", "")).lower()
    request_id = payload.get("request_id")

    if action == "delete":
        requested = payload.get("clip_ids") or []
        valid = [c for c in requested if isinstance(c, str) and is_valid_clip_id(c)]
        if len(valid) != len(requested):
            log.warning(f"Ignoring {len(requested) - len(valid)} malformed clip id(s)")
        deleted = rec.delete_clips(valid)
        publish_command_result(client, request_id, action, deleted, True)

    elif action in ("protect", "unprotect"):
        window_id = payload.get("window_id")
        if not isinstance(window_id, str) or not window_id.strip():
            publish_command_result(client, request_id, action, [], False, "missing window_id")
            return
        if action == "unprotect":
            rec.unprotect(window_id)
        else:
            window = payload.get("window") or {}
            frm = window.get("from")
            if not frm:
                publish_command_result(client, request_id, action, [], False, "missing window.from")
                return
            rec.protect(window_id, frm, window.get("to"))
        publish_command_result(client, request_id, action, [], True)

    else:
        log.warning(f"Unknown dashcam command: {action!r}")
        publish_command_result(client, request_id, action, [], False, "unknown action")


def handle_crash_event(rec, payload: dict) -> None:
    """Cut the segment at the incident and self-protect a window around it.

    Belt and braces: db_writer will send an authoritative trip-scoped protect,
    but that depends on the Pi being alive and the trip resolving. This costs
    two local operations and means the footage survives even if neither happens.
    """
    if str(payload.get("severity")) != "potential_crash":
        return
    when = parse_iso(payload.get("ts")) or datetime.now(timezone.utc)
    rec.split_now()
    margin = timedelta(seconds=CRASH_SELF_PROTECT_S)
    window_id = f"crash-{when.strftime('%Y%m%dT%H%M%SZ')}"
    rec.protect(window_id, (when - margin).isoformat(), (when + margin).isoformat())
    log.warning(f"Potential crash at {when.isoformat()} — segment split and "
                f"±{CRASH_SELF_PROTECT_S}s of footage protected")

# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

def build_mqtt_client() -> mqtt.Client:
    # clean_session=False so the broker QUEUES dashcam commands issued while the
    # Jetson is offline. A delete or protect requested from the dashboard during
    # a cable drop must still land — otherwise the Pi's rows and the Jetson's
    # files silently diverge. Requires a stable client_id, which we already have.
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                             client_id="vision_publisher", clean_session=False)
    except AttributeError:
        client = mqtt.Client(client_id="vision_publisher", clean_session=False)  # paho < 2.0

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
            # Resubscribed on every reconnect: with clean_session=False the
            # broker keeps the subscription, but re-issuing is harmless and
            # covers a broker that lost its persistence store.
            client.subscribe(f"{MQTT_DASHCAM_BASE}/command", qos=1)
            client.subscribe(f"{MQTT_TELEMETRY_BASE}/crash_event", qos=1)
            if clock_is_sane():
                publish_status(client, "connected", f"source={VISION_SOURCE}")
            else:
                publish_status(client, "connecting", "waiting for clock sync")
        else:
            log.error(f"MQTT connection failed — rc={rc}")

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning(f"MQTT unexpected disconnect — rc={rc}")

    def on_message(client, userdata, msg):
        # userdata is the Recorder. Nothing arriving from the broker may kill
        # this process — it also carries the inference pipeline.
        rec = userdata
        if rec is None:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not an object")
        except (ValueError, UnicodeDecodeError) as e:
            log.warning(f"Bad payload on {msg.topic}: {e}")
            return
        try:
            if msg.topic.endswith("/command"):
                handle_dashcam_command(client, rec, payload)
            elif msg.topic.endswith("/crash_event"):
                handle_crash_event(rec, payload)
        except Exception as e:
            log.warning(f"Handler for {msg.topic} failed — continuing: {e}")

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    return client

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    client = build_mqtt_client()

    # The recorder publishes through the same client, but the client does not
    # exist until now — so these thunks defer the lookup rather than forcing a
    # back-reference from recorder.py into this module.
    rec = recorder.Recorder(
        on_clip_closed=lambda clip: publish_clip(client, clip),
        on_pruned=lambda ids, reason: publish_pruned(client, ids, reason),
    )
    client.user_data_set(rec)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.critical(f"Cannot connect to MQTT broker at {MQTT_HOST}:{MQTT_PORT}: {e}")
        sys.exit(1)

    client.loop_start()

    # Deliberately NOT started yet — see the clock gate in the loop below.
    source = camera.FrameSource(rec)

    events_published  = 0
    last_heartbeat    = -HEARTBEAT_INTERVAL_S  # publish immediately
    last_step_err_t   = -STEP_ERR_LOG_INTERVAL_S
    clock_was_sane    = False

    def capture() -> dict:
        # Injected into classifier.step() — the only path that touches the
        # camera. ts/frame_id are minted HERE, at capture time: with slow
        # inference, capture and publish drift apart, and capture time is
        # what aligns with OBD readings.
        return {"frame": source.read(), "ts": utc_now_iso(), "frame_id": uuid.uuid4().hex}

    try:
        while True:
            now = time.monotonic()

            # Heartbeat — freshness signal for the Pi bridge's staleness check
            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                if clock_is_sane():
                    cs = classifier.get_status()
                    tracks = " ".join(f"{n}={t['state']}/{t['label']}"
                                      for n, t in cs["tracks"].items())
                    publish_status(client, "connected",
                                   f"source={source.mode} {tracks} "
                                   f"captures={source.frame_count} events={events_published}")
                    publish_dashcam_status(client, rec, f"source={source.mode}")
                else:
                    publish_status(client, "connecting", "waiting for clock sync")
                last_heartbeat = now

            # Clock gate — status only, no frames, until the clock is plausible.
            # Recording is gated too: the Orin boots in 1970, and segments named
            # from a 1970 clock could never be matched to a trip.
            if not clock_is_sane():
                time.sleep(0.5)
                continue
            if not clock_was_sane:
                clock_was_sane = True
                log.info("System clock is sane — frame publishing enabled")
                # Open the camera only now. Starting the recorder before chrony
                # steps the clock would name segments from 1970, and the Pi
                # matches clips to trips by timestamp — those files could never
                # be attributed to anything.
                source.start()

            # Frame-source maintenance: recorder bus/finalize/prune, or camera
            # reopen backoff, depending on which rung we are on.
            source.tick()

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
        # Releases the recorder (EOS + muxer finalize) as well as any cv2
        # handle. Must finish inside the unit's TimeoutStopSec=10.
        source.release()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    log.info("vision_publisher starting")
    run()
