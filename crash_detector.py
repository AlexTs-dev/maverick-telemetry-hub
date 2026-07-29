"""
crash_detector.py
Maverick Telemetry Hub

Watches the OBD speed stream for hard decelerations and publishes them as
events. Two severities:

    hard_brake      ~0.55g — logged only. Ordinary panic braking.
    potential_crash ~0.90g and an actual stop — logged AND protects the trip's
                    dashcam footage from the 30-day retention purge.

Subscribes: maverick/telemetry/reading
Publishes:  maverick/telemetry/crash_event

A separate process rather than a branch of trip_manager, per the isolation
invariant: trip_manager owns the trip lifecycle, this owns event detection, and
they share nothing but the topic they both read.

ON THE LIMITS OF THIS SIGNAL: obd_poller samples at ~1Hz, and a real impact
lasts well under 100ms — so a collision is smeared across a sample rather than
resolved by one, and these thresholds are a starting point to be calibrated
against real hard_brake data, not a finished result. That is precisely why
hard_brake is recorded even though it protects nothing: it is the dataset for
tuning the crash threshold. If this proves too blunt, crash_events.source and
this topic are already shaped so an IMU can publish alongside without a schema
or UI change.

Managed by systemd — see deploy/crash_detector.service
"""

import json
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST       = "localhost"
MQTT_PORT       = 1883
MQTT_TOPIC_BASE = "maverick/telemetry"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# 1 mph/s = 0.44704 m/s^2, and 1g = 9.80665 m/s^2.
MPH_PER_S_TO_G = 0.44704 / 9.80665  # 0.04559

# potential_crash: violent deceleration that actually ended in a stop. The
# "came to a stop" clause is what separates a collision from hard braking in
# traffic — a panic stop that trails off to 20 mph is not a crash.
CRASH_DECEL_G     = _env_float("MAVERICK_CRASH_DECEL_G", 0.90)
CRASH_MIN_SPEED   = _env_float("MAVERICK_CRASH_MIN_SPEED_MPH", 15.0)
CRASH_STOP_SPEED  = _env_float("MAVERICK_CRASH_STOP_MPH", 5.0)

# hard_brake: notable but unremarkable. Logged for threshold calibration.
BRAKE_DECEL_G     = _env_float("MAVERICK_BRAKE_DECEL_G", 0.55)
BRAKE_MIN_SPEED   = _env_float("MAVERICK_BRAKE_MIN_SPEED_MPH", 10.0)

# A silent poller is NOT a deceleration. If the previous sample is older than
# this, the window resets — without this guard, every OBD dropout and every
# reconnect after the adapter drops out looks like the car stopped instantly.
# This is the single most important false-positive guard in the file.
MAX_GAP_S = _env_float("MAVERICK_CRASH_MAX_GAP_S", 3.0)

# One event per severity per this window, so a single stop does not emit a
# burst of rows as the car settles.
COOLDOWN_S = _env_float("MAVERICK_CRASH_COOLDOWN_S", 10.0)

# How many prior samples to compare against. At 1Hz a hard stop can straddle a
# sample boundary (45 -> 40 -> 0), so the worst deceleration is not always
# between adjacent readings.
LOOKBACK_SAMPLES = 2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("crash_detector")

# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class CrashDetector:
    """Rolling deceleration analysis over the 1Hz speed stream."""

    def __init__(self):
        self._samples = deque(maxlen=LOOKBACK_SAMPLES + 1)  # (epoch_s, mph)
        self._last_event_at = {}  # severity -> epoch_s

    @staticmethod
    def _parse_ts(value):
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    def observe(self, payload: dict):
        """Feed one reading. Returns an event dict, or None."""
        speed = payload.get("speed_mph")
        if speed is None:
            return None            # a failed PID is not a data point
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return None

        ts = self._parse_ts(payload.get("ts"))
        if ts is None:
            return None

        # Out-of-order or duplicate samples would produce nonsense dt values.
        if self._samples and ts <= self._samples[-1][0]:
            return None

        # Gap guard: drop the history rather than measure across the hole.
        if self._samples and (ts - self._samples[-1][0]) > MAX_GAP_S:
            log.debug(f"OBD gap of {ts - self._samples[-1][0]:.1f}s — resetting window")
            self._samples.clear()

        self._samples.append((ts, speed))
        return self._evaluate()

    def _evaluate(self):
        if len(self._samples) < 2:
            return None
        now_ts, now_speed = self._samples[-1]

        peak_g, speed_before = 0.0, None
        for prev_ts, prev_speed in list(self._samples)[:-1]:
            dt = now_ts - prev_ts
            if dt <= 0 or dt > MAX_GAP_S * LOOKBACK_SAMPLES:
                continue
            g = ((prev_speed - now_speed) / dt) * MPH_PER_S_TO_G
            if g > peak_g:
                peak_g, speed_before = g, prev_speed

        if speed_before is None:
            return None

        severity = None
        if (peak_g >= CRASH_DECEL_G
                and speed_before >= CRASH_MIN_SPEED
                and now_speed <= CRASH_STOP_SPEED):
            severity = "potential_crash"
        elif peak_g >= BRAKE_DECEL_G and speed_before >= BRAKE_MIN_SPEED:
            severity = "hard_brake"

        if severity is None:
            return None

        # A potential_crash supersedes a hard_brake in the same moment: the
        # cooldown is checked per severity, so the more serious event still
        # gets out even if braking was already reported a second earlier.
        last = self._last_event_at.get(severity, 0.0)
        if now_ts - last < COOLDOWN_S:
            return None
        self._last_event_at[severity] = now_ts

        return {
            "ts":               datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
            "severity":         severity,
            "source":           "obd_speed",
            "peak_decel_g":     round(peak_g, 3),
            "speed_before_mph": round(speed_before, 1),
            "speed_after_mph":  round(now_speed, 1),
            "detail":           f"{speed_before:.1f} -> {now_speed:.1f} mph "
                                f"({peak_g:.2f}g)",
        }

# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

def build_mqtt_client(detector: CrashDetector) -> mqtt.Client:
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="crash_detector")
    except AttributeError:
        client = mqtt.Client(client_id="crash_detector")  # paho-mqtt < 2.0

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected — subscribing to readings")
            client.subscribe(f"{MQTT_TOPIC_BASE}/reading", qos=1)
        else:
            log.error(f"MQTT connection failed — rc={rc}")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError as e:
            log.warning(f"Bad JSON on {msg.topic}: {e}")
            return

        try:
            event = detector.observe(payload)
        except Exception as e:
            log.warning(f"Detector failed on a reading — continuing: {e}")
            return

        if event is None:
            return

        client.publish(f"{MQTT_TOPIC_BASE}/crash_event", json.dumps(event), qos=1)
        logger = log.warning if event["severity"] == "potential_crash" else log.info
        logger(f"{event['severity']}: {event['detail']}")

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning(f"MQTT unexpected disconnect — rc={rc}")

    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect
    return client

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    log.info(f"Thresholds — crash: {CRASH_DECEL_G}g from >={CRASH_MIN_SPEED}mph to "
             f"<={CRASH_STOP_SPEED}mph | brake: {BRAKE_DECEL_G}g from >={BRAKE_MIN_SPEED}mph")

    detector = CrashDetector()
    client = build_mqtt_client(detector)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.critical(f"Cannot connect to MQTT broker: {e}")
        sys.exit(1)

    client.loop_start()
    log.info("crash_detector running — watching for hard decelerations")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    log.info("crash_detector starting")
    run()
