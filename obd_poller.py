"""
obd_poller.py
Maverick Telemetry Hub

Connects to the OBDLink EX via USB, polls sensor PIDs at 1Hz,
and publishes readings to the local Mosquitto MQTT broker.

This process knows nothing about SQLite. It only reads from the
car and publishes to MQTT. db_writer.py handles persistence.

Managed by systemd — see deploy/obd_poller.service
"""

import obd
import paho.mqtt.client as mqtt
import json
import os
import time
import logging
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST       = "localhost"
MQTT_PORT       = 1883
MQTT_TOPIC_BASE = "maverick/telemetry"

# USB device path for OBDLink EX. Set OBD_PORT env var to override.
# Leave unset (or set to empty string) to let python-obd auto-detect.
OBD_PORT        = os.environ.get("OBD_PORT") or None   # None → auto-scan
OBD_BAUDRATE    = int(os.environ.get("OBD_BAUDRATE", "0"))  # 0 → auto-detect rate
POLL_INTERVAL   = 1.0  # seconds

# Reconnect backoff — doubles each attempt up to MAX_BACKOFF
INITIAL_BACKOFF = 2    # seconds
MAX_BACKOFF     = 60   # seconds

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("obd_poller")

# ---------------------------------------------------------------------------
# PID definitions
# ---------------------------------------------------------------------------
# Each entry: (obd.commands key, output field name, unit conversion fn)
# Hybrid-specific PIDs (battery_soc, ev_mode, regen) may return None
# on non-hybrid vehicles or if Ford locks the PID. Handled gracefully.

def _to_rpm(v):    return round(v.magnitude, 1) if v else None
def _to_mph(v):    return round(v.to("mph").magnitude, 1) if v else None
def _to_f(v):      return round(v.to("degF").magnitude, 1) if v else None
def _to_pct(v):    return round(v.magnitude, 1) if v else None
def _to_gph(v):    return round(v.magnitude, 3) if v else None
def _passthrough(v): return v.magnitude if v else None
def _raw(v):       return v  # decoder already returns a plain number

STANDARD_PIDS = [
    (obd.commands.RPM,              "rpm",            _to_rpm),
    (obd.commands.SPEED,            "speed_mph",      _to_mph),
    (obd.commands.COOLANT_TEMP,     "coolant_temp_f", _to_f),
    (obd.commands.THROTTLE_POS,     "throttle_pct",   _to_pct),
    (obd.commands.FUEL_RATE,        "fuel_rate_gph",  _to_gph),
]

# ---------------------------------------------------------------------------
# Ford Mode 22 (proprietary) hybrid PIDs
# ---------------------------------------------------------------------------
# Decoders receive a list of Message objects; data is a bytearray where:
#   data[0]    = 0x62 (mode 22 positive response)
#   data[1:3]  = PID echo bytes
#   data[3:]   = payload

def _m22_word_div100(messages):
    d = messages[0].data
    return round((d[3] * 256 + d[4]) / 100, 1)

def _m22_word_div10(messages):
    d = messages[0].data
    return round((d[3] * 256 + d[4]) / 10, 2)

def _m22_signed_div10(messages):
    d = messages[0].data
    return round(((d[3] * 256 + d[4]) - 32768) / 10, 2)

def _m22_signed(messages):
    d = messages[0].data
    return (d[3] * 256 + d[4]) - 32768


_CMD_HV_SOC    = obd.OBDCommand("HV_SOC",  "HV State of Charge",       b"224811", 2, _m22_word_div100)
_CMD_PACK_V    = obd.OBDCommand("PACK_V",  "HV Pack Voltage",           b"22480B", 2, _m22_word_div10)
_CMD_BATT_I    = obd.OBDCommand("BATT_I",  "HV Battery Current",        b"22480C", 2, _m22_signed_div10)
_CMD_MOTOR_RPM = obd.OBDCommand("MG2_RPM", "Traction Motor Speed (MG2)", b"224A15", 2, _m22_signed)

# battery_soc_pct is a direct read.
# ev_mode and regen_kw are derived in poll_once from the raw sensor fields.
HYBRID_PIDS = [
    (_CMD_HV_SOC,    "battery_soc_pct",   _raw),
    (_CMD_PACK_V,    "pack_voltage_v",     _raw),
    (_CMD_BATT_I,    "battery_current_a",  _raw),
    (_CMD_MOTOR_RPM, "motor_speed_rpm",    _raw),
]

ALL_PIDS = STANDARD_PIDS + HYBRID_PIDS

# ---------------------------------------------------------------------------
# MQTT helpers
# ---------------------------------------------------------------------------

def build_mqtt_client() -> mqtt.Client:
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="obd_poller")
    except AttributeError:
        client = mqtt.Client(client_id="obd_poller")  # paho-mqtt < 2.0

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected")
        else:
            log.error(f"MQTT connection failed — rc={rc}")

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning(f"MQTT unexpected disconnect — rc={rc}")

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    return client


def publish_reading(client: mqtt.Client, payload: dict) -> None:
    topic = f"{MQTT_TOPIC_BASE}/reading"
    client.publish(topic, json.dumps(payload), qos=1)


def publish_status(client: mqtt.Client, status: str, detail: str = "") -> None:
    topic = f"{MQTT_TOPIC_BASE}/poller_status"
    payload = {
        "status": status,
        "detail": detail,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    client.publish(topic, json.dumps(payload), qos=1)

# ---------------------------------------------------------------------------
# OBD connection with retry/backoff
# ---------------------------------------------------------------------------

def connect_obd(backoff: float) -> obd.OBD:
    """
    Attempt to connect to the OBDLink EX. On failure, waits `backoff`
    seconds and raises so the caller can double the backoff and retry.
    """
    log.info(f"Connecting to OBD on {OBD_PORT or 'auto-detected port'}...")
    connection = obd.OBD(
        portstr=OBD_PORT,
        baudrate=OBD_BAUDRATE or None,  # None triggers auto-detection
        fast=False,       # required for Ford vehicles
        timeout=30,
        check_voltage=False,
    )
    if not connection.is_connected():
        raise ConnectionError(f"OBD connection failed — is OBDLink EX plugged in?")
    try:
        baud = connection.interface._ELM327__port.baudrate
        log.info(f"OBD connected — port={OBD_PORT or 'auto'} baudrate={baud}")
    except Exception:
        log.info("OBD connected")
    return connection


def poll_once(connection: obd.OBD) -> dict:
    """
    Poll all configured PIDs. Returns a dict of field: value pairs.
    Missing or errored PIDs produce None values — never raises.
    """
    reading = {"ts": datetime.now(timezone.utc).isoformat()}
    for command, field, converter in ALL_PIDS:
        try:
            response = connection.query(command)
            reading[field] = converter(response.value) if not response.is_null() else None
        except Exception as e:
            log.debug(f"PID error ({field}): {e}")
            reading[field] = None

    # Derive ev_mode and regen_kw from the raw hybrid sensor readings
    pack_v    = reading.get("pack_voltage_v")
    batt_i    = reading.get("battery_current_a")
    motor_rpm = reading.get("motor_speed_rpm")

    # ev_mode: engine off (RPM near zero) while traction motor is spinning
    engine_rpm = reading.get("rpm") or 0
    reading["ev_mode"] = (
        1 if (motor_rpm is not None and abs(motor_rpm) > 50 and engine_rpm < 100) else 0
    )

    # regen_kw: battery current is negative when the pack is charging (regen)
    if pack_v is not None and batt_i is not None and batt_i < 0:
        reading["regen_kw"] = round(pack_v * abs(batt_i) / 1000, 3)
    else:
        reading["regen_kw"] = 0.0

    return reading

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    # Connect to MQTT first — if this fails we can't do anything useful
    mqtt_client = build_mqtt_client()
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.critical(f"Cannot connect to MQTT broker: {e}")
        sys.exit(1)

    mqtt_client.loop_start()

    backoff = INITIAL_BACKOFF
    connection = None

    while True:
        # --- Ensure OBD connection ---
        if connection is None or not connection.is_connected():
            publish_status(mqtt_client, "connecting")
            try:
                connection = connect_obd(backoff)
                backoff = INITIAL_BACKOFF  # reset on success
                publish_status(mqtt_client, "connected")
            except Exception as e:
                log.warning(f"OBD connect failed: {e} — retrying in {backoff}s")
                publish_status(mqtt_client, "disconnected", str(e))
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

        # --- Poll and publish ---
        try:
            reading = poll_once(connection)
            publish_reading(mqtt_client, reading)
            log.debug(f"Published: {reading}")
        except Exception as e:
            log.warning(f"Poll error: {e} — marking disconnected")
            connection = None  # triggers reconnect on next iteration

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    log.info("obd_poller starting")
    try:
        run()
    except KeyboardInterrupt:
        log.info("Stopped by user")
