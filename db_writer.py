"""
db_writer.py
Maverick Telemetry Hub

Subscribes to all MQTT telemetry topics and writes data to SQLite.
This is the only process that touches the database directly.

Handles:
- readings        → INSERT into readings table immediately
- trip_open       → INSERT into trips table
- trip_close      → UPDATE trips, compute and INSERT trip_summaries
- dtcs            → INSERT into dtcs table

Write failures retry up to 3 times with brief backoff, then skip
the record and log the failure. Process stays alive regardless.

Managed by systemd — see deploy/db_writer.service
"""

import os
import paho.mqtt.client as mqtt
import sqlite3
import json
import time
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST       = "localhost"
MQTT_PORT       = 1883
MQTT_TOPIC_BASE = "maverick/telemetry"

_default_db = Path(__file__).resolve().parent / "maverick_telemetry.db"
DB_PATH     = Path(os.environ.get("MAVERICK_DB_PATH", _default_db))

MAX_RETRIES     = 3
RETRY_DELAY     = 0.5  # seconds between retries

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("db_writer")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode — allows reads while writing, better for concurrent access
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def with_retry(fn, *args, **kwargs):
    """
    Execute fn(*args, **kwargs) up to MAX_RETRIES times.
    Logs each failure. After all retries exhausted, logs and returns None.
    Never raises — process stays alive.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as e:
            log.warning(f"DB write failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    log.error("DB write failed after all retries — skipping record")
    return None

# ---------------------------------------------------------------------------
# Active trip tracking
# ---------------------------------------------------------------------------
# db_writer needs to know the current trip_id so it can attach readings
# to the right trip. Stored in memory — if db_writer restarts mid-trip,
# it will miss the trip_open event and readings will be unattached until
# the next trip starts. Acceptable tradeoff for this architecture.

_state_lock    = threading.Lock()
_active_trip_id = None  # int or None


def get_active_trip_id():
    with _state_lock:
        return _active_trip_id


def set_active_trip_id(trip_id):
    global _active_trip_id
    with _state_lock:
        _active_trip_id = trip_id

# ---------------------------------------------------------------------------
# Write handlers
# ---------------------------------------------------------------------------

def handle_reading(conn: sqlite3.Connection, payload: dict) -> None:
    trip_id = get_active_trip_id()
    if trip_id is None:
        log.debug("Reading received but no active trip — skipping")
        return

    def _write():
        conn.execute(
            """
            INSERT INTO readings (
                trip_id, ts, rpm, speed_mph, coolant_temp_f,
                throttle_pct, battery_soc_pct, ev_mode, regen_kw,
                fuel_rate_gph, pack_voltage_v, battery_current_a,
                motor_speed_rpm
            ) VALUES (
                :trip_id, :ts, :rpm, :speed_mph, :coolant_temp_f,
                :throttle_pct, :battery_soc_pct, :ev_mode, :regen_kw,
                :fuel_rate_gph, :pack_voltage_v, :battery_current_a,
                :motor_speed_rpm
            )
            """,
            {
                "trip_id":          trip_id,
                "ts":               payload.get("ts"),
                "rpm":              payload.get("rpm"),
                "speed_mph":        payload.get("speed_mph"),
                "coolant_temp_f":   payload.get("coolant_temp_f"),
                "throttle_pct":     payload.get("throttle_pct"),
                "battery_soc_pct":  payload.get("battery_soc_pct"),
                "ev_mode":          payload.get("ev_mode"),
                "regen_kw":         payload.get("regen_kw"),
                "fuel_rate_gph":    payload.get("fuel_rate_gph"),
                "pack_voltage_v":   payload.get("pack_voltage_v"),
                "battery_current_a": payload.get("battery_current_a"),
                "motor_speed_rpm":  payload.get("motor_speed_rpm"),
            },
        )
        conn.commit()

    with_retry(_write)


def handle_trip_open(conn: sqlite3.Connection, payload: dict) -> None:
    started_at = payload.get("started_at", datetime.now(timezone.utc).isoformat())

    def _write():
        cursor = conn.execute(
            "INSERT INTO trips (started_at) VALUES (?)",
            (started_at,),
        )
        conn.commit()
        return cursor.lastrowid

    trip_id = with_retry(_write)
    if trip_id:
        set_active_trip_id(trip_id)
        log.info(f"Trip opened — id={trip_id} started_at={started_at}")


def handle_trip_close(conn: sqlite3.Connection, payload: dict) -> None:
    trip_id = get_active_trip_id()
    if trip_id is None:
        log.warning("trip_close received but no active trip — ignoring")
        return

    ended_at = payload.get("ended_at", datetime.now(timezone.utc).isoformat())
    reason   = payload.get("reason", "unknown")

    def _close():
        # Count DTCs for this trip
        dtc_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM dtcs WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()
        dtc_count = dtc_row["cnt"] if dtc_row else 0

        # Compute duration
        started_row = conn.execute(
            "SELECT started_at FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

        duration = None
        if started_row:
            try:
                start = datetime.fromisoformat(started_row["started_at"])
                end   = datetime.fromisoformat(ended_at)
                duration = int((end - start).total_seconds())
            except Exception:
                pass

        conn.execute(
            """
            UPDATE trips
            SET ended_at = ?, duration_seconds = ?, dtc_count = ?
            WHERE id = ?
            """,
            (ended_at, duration, dtc_count, trip_id),
        )
        conn.commit()

    with_retry(_close)

    # Compute and store trip summary
    with_retry(lambda: compute_trip_summary(conn, trip_id))

    log.info(f"Trip closed — id={trip_id} reason={reason}")
    set_active_trip_id(None)


def compute_trip_summary(conn: sqlite3.Connection, trip_id: int) -> None:
    """
    Aggregate readings for the closed trip and write to trip_summaries.
    Called once per trip close — never at query time.
    """
    row = conn.execute(
        """
        SELECT
            AVG(speed_mph)                          AS avg_speed_mph,
            MAX(speed_mph)                          AS max_speed_mph,
            AVG(rpm)                                AS avg_rpm,
            MAX(coolant_temp_f)                     AS max_coolant_temp_f,
            -- % of readings where ev_mode = 1
            ROUND(
                100.0 * SUM(CASE WHEN ev_mode = 1 THEN 1 ELSE 0 END)
                / NULLIF(COUNT(ev_mode), 0), 1
            )                                       AS ev_time_pct,
            -- regen_kw * (1/3600) hours per second = kWh per reading
            ROUND(SUM(COALESCE(regen_kw, 0)) / 3600.0, 4)
                                                    AS total_regen_kwh,
            -- MPH / (GPH * 1) = MPG (instantaneous avg)
            ROUND(
                AVG(CASE
                    WHEN fuel_rate_gph > 0
                    THEN speed_mph / fuel_rate_gph
                    ELSE NULL
                END), 1
            )                                       AS avg_fuel_economy_mpg,
            MIN(battery_soc_pct)                    AS min_battery_soc_pct
        FROM readings
        WHERE trip_id = ?
        """,
        (trip_id,),
    ).fetchone()

    if not row:
        log.warning(f"No readings found for trip {trip_id} — skipping summary")
        return

    conn.execute(
        """
        INSERT OR REPLACE INTO trip_summaries (
            trip_id, avg_speed_mph, max_speed_mph, avg_rpm,
            max_coolant_temp_f, ev_time_pct, total_regen_kwh,
            avg_fuel_economy_mpg, min_battery_soc_pct
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trip_id,
            row["avg_speed_mph"],
            row["max_speed_mph"],
            row["avg_rpm"],
            row["max_coolant_temp_f"],
            row["ev_time_pct"],
            row["total_regen_kwh"],
            row["avg_fuel_economy_mpg"],
            row["min_battery_soc_pct"],
        ),
    )
    conn.commit()
    log.info(f"Trip summary written for trip {trip_id}")


def handle_dtc(conn: sqlite3.Connection, payload: dict) -> None:
    trip_id = get_active_trip_id()
    if trip_id is None:
        log.warning("DTC received but no active trip — skipping")
        return

    def _write():
        conn.execute(
            """
            INSERT INTO dtcs (trip_id, code, first_seen_at)
            VALUES (?, ?, ?)
            """,
            (
                trip_id,
                payload.get("code"),
                payload.get("first_seen_at", datetime.now(timezone.utc).isoformat()),
            ),
        )
        conn.commit()

    with_retry(_write)
    log.info(f"DTC recorded: {payload.get('code')} for trip {trip_id}")

# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

def build_mqtt_client(conn: sqlite3.Connection) -> mqtt.Client:
    client = mqtt.Client(client_id="db_writer")

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected — subscribing to topics")
            client.subscribe(f"{MQTT_TOPIC_BASE}/reading",    qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/trip_open",  qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/trip_close", qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/dtc",        qos=1)
        else:
            log.error(f"MQTT connection failed — rc={rc}")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except json.JSONDecodeError as e:
            log.warning(f"Bad JSON on {msg.topic}: {e}")
            return

        topic = msg.topic
        if topic.endswith("/reading"):
            handle_reading(conn, payload)
        elif topic.endswith("/trip_open"):
            handle_trip_open(conn, payload)
        elif topic.endswith("/trip_close"):
            handle_trip_close(conn, payload)
        elif topic.endswith("/dtc"):
            handle_dtc(conn, payload)

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
    if not DB_PATH.exists():
        log.critical(
            f"Database not found at {DB_PATH}. "
            "Run db/migrate.py first."
        )
        sys.exit(1)

    conn = get_connection()
    log.info(f"SQLite connected — {DB_PATH}")

    mqtt_client = build_mqtt_client(conn)

    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.critical(f"Cannot connect to MQTT broker: {e}")
        sys.exit(1)

    log.info("db_writer running — listening for telemetry events")
    mqtt_client.loop_start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        conn.close()


if __name__ == "__main__":
    log.info("db_writer starting")
    run()
