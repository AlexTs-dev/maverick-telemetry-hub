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
from obd.protocols import ECU
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

def _to_rpm(v):    return round(v.magnitude, 1)          if v is not None else None
def _to_mph(v):    return round(v.to("mph").magnitude, 1) if v is not None else None
def _to_f(v):      return round(v.to("degF").magnitude, 1) if v is not None else None
def _to_pct(v):    return round(v.magnitude, 1)           if v is not None else None
def _to_gph(v):    return round(v.magnitude, 3)           if v is not None else None
def _to_v(v):      return round(v.to("volt").magnitude, 1) if v is not None else None
def _raw(v):       return v  # decoder already returns a plain number

STANDARD_PIDS = [
    (obd.commands.RPM,              "rpm",            _to_rpm),
    (obd.commands.SPEED,            "speed_mph",      _to_mph),
    (obd.commands.COOLANT_TEMP,     "coolant_temp_f", _to_f),
    (obd.commands.THROTTLE_POS,     "throttle_pct",   _to_pct),
    (obd.commands.FUEL_RATE,        "fuel_rate_gph",  _to_gph),
]

# ---------------------------------------------------------------------------
# Proprietary Ford Mode 22 PIDs — BECM (Battery Energy Control Module)
# ---------------------------------------------------------------------------
# HV battery data is NOT on the standard PCM. It lives in the BECM, which answers
# UDS service 0x22 (ReadDataByIdentifier) on CAN header 7E4 (response 7EC). These
# DIDs + scalings were reverse-engineered on-bus and verified against the dash on
# a 2026 Maverick FHEV (VIN 3FTTW8J34TRA11073, June 2026). See PORTABLE_SPEC.
#
# python-obd has no built-in Ford PIDs, so each is a custom OBDCommand:
#   header=BECM_HEADER → python-obd issues `AT SH 7E4` before the request
#   _bytes=0           → don't truncate/pad the multi-byte payload
#   ecu=ECU.ALL        → BECM replies are tagged ECU.UNKNOWN; ECU.ALL keeps them
#   fast=False         → required for Ford (also set on the connection)
# These aren't in the auto-detected supported list, so poll_once queries them with
# force=True. The decoder gets a list of Messages; messages[0].data is the full UDS
# payload "62 <DID_hi> <DID_lo> <data...>" — strip the 3 echo bytes for the data.

BECM_HEADER = b"7E4"


def _becm_command(name, desc, request, decoder):
    """Build a custom Mode 22 OBDCommand targeting the BECM (header 7E4)."""
    return obd.OBDCommand(
        name, desc, request, 0, decoder,
        ecu=ECU.ALL, fast=False, header=BECM_HEADER,
    )


def _decode_soc(messages):
    # DID 4801 → 62 48 01 A B; SOC% = (A*256 + B) / 500.0  (verified 0–100%).
    data = messages[0].data
    if len(data) < 5:
        return None
    return round((data[3] * 256 + data[4]) / 500.0, 2) * obd.Unit.percent


def _decode_pack_temp(messages):
    # DID 4808 → 62 48 08 A B C D; bytes = max/min/range/avg cell temp, each raw-50°C.
    # Byte D (index 6) is the average pack temp. NOTE: the HV pack uses a -50 offset,
    # NOT the -40 used for cabin/coolant temps. Returned in °C; _to_f converts to °F.
    data = messages[0].data
    if len(data) < 7:
        return None
    return obd.Unit.Quantity(data[6] - 50, obd.Unit.celsius)


def _decode_pack_voltage(messages):
    # DID 480D → 62 48 0D A B; Volts = (A*256 + B) / 100.0  (terminal voltage).
    data = messages[0].data
    if len(data) < 5:
        return None
    return round((data[3] * 256 + data[4]) / 100.0, 1) * obd.Unit.volt


# Same (command, output field, converter) shape as STANDARD_PIDS.
CUSTOM_PIDS = [
    (_becm_command("HVB_SOC",   "HV battery SOC",   b"224801", _decode_soc),          "battery_soc_pct", _to_pct),
    (_becm_command("HVB_TEMP",  "HV pack avg temp", b"224808", _decode_pack_temp),    "hvb_temp_f",      _to_f),
    (_becm_command("HVB_VOLTS", "HV pack voltage",  b"22480D", _decode_pack_voltage), "pack_voltage_v",  _to_v),
]

# Standard PIDs are auto-detected (force=False); custom BECM PIDs aren't in the
# supported list and must be forced. Normalize both to (command, field, converter, force).
ALL_PIDS = (
    [(cmd, field, conv, False) for cmd, field, conv in STANDARD_PIDS]
    + [(cmd, field, conv, True) for cmd, field, conv in CUSTOM_PIDS]
)

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
    for command, field, converter, force in ALL_PIDS:
        try:
            response = connection.query(command, force=force)
            reading[field] = converter(response.value) if not response.is_null() else None
        except Exception as e:
            log.warning(f"PID error ({field}): {e}")
            reading[field] = None

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
