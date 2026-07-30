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
- vision frames   → write JPEG to MAVERICK_SNAPSHOT_DIR, INSERT into vision_frames
- dashcam clips   → INSERT into dashcam_clips, resolving the trip by timestamp
- crash events    → INSERT into crash_events, protecting footage on a crash

Write failures retry up to 3 times with brief backoff, then skip
the record and log the failure. Process stays alive regardless.

DASHCAM FOOTAGE LIVES ON THE JETSON, not here — these rows are metadata and the
authoritative trip linkage. That makes db_writer the one process that knows
which footage belongs to which trip, and therefore which footage a crash must
protect, so this is also the only place that PUBLISHES: trip-scoped protect
commands go back out on maverick/dashcam/command. It stays the single DB
writer — the Express bridge asks for deletes over MQTT rather than writing rows
itself.

Managed by systemd — see deploy/db_writer.service
"""

import base64
import os
import paho.mqtt.client as mqtt
import re
import sqlite3
import json
import time
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST       = "localhost"
MQTT_PORT       = 1883
MQTT_TOPIC_BASE = "maverick/telemetry"
MQTT_VISION_TOPIC_BASE  = "maverick/vision"
MQTT_DASHCAM_TOPIC_BASE = "maverick/dashcam"
_default_snapshots = Path(__file__).resolve().parent / "snapshots"
SNAPSHOT_DIR = Path(os.environ.get("MAVERICK_SNAPSHOT_DIR", _default_snapshots))

_default_db = Path(__file__).resolve().parent / "maverick_telemetry.db"
DB_PATH     = Path(os.environ.get("MAVERICK_DB_PATH", _default_db))

MAX_RETRIES     = 3
RETRY_DELAY     = 0.5  # seconds between retries

# ~2.6 GB at ~130 KB/frame — the snapshot store must be self-bounding
# (no SSH into the truck to clean up a full disk).
MAX_SNAPSHOTS = int(os.environ.get("MAVERICK_MAX_SNAPSHOTS", "20000"))

# Slack when matching a dashcam clip to a trip.
#   LEAD  — recording starts at Jetson boot, but a trip only opens once RPM
#           crosses the threshold, so footage legitimately precedes trip_open.
#   TRAIL — trip_manager closes a trip 5 min after the last reading (the
#           zero-RPM timeout), and footage recorded during that idle tail
#           belongs to the drive that just ended.
TRIP_MATCH_LEAD_S  = 60
TRIP_MATCH_TRAIL_S = 5 * 60

# 20260729T143005Z_a1b2c3d4 — anchored. The broker accepts anonymous
# publishers, so a clip id is untrusted input until it matches this.
_CLIP_ID_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{8}$")

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
# and vision frames to the right trip. Stored in memory — if db_writer
# restarts mid-trip, it will miss the trip_open event and both are dropped
# until the next trip starts. Acceptable tradeoff for this architecture.

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
                motor_speed_rpm, hvb_temp_f
            ) VALUES (
                :trip_id, :ts, :rpm, :speed_mph, :coolant_temp_f,
                :throttle_pct, :battery_soc_pct, :ev_mode, :regen_kw,
                :fuel_rate_gph, :pack_voltage_v, :battery_current_a,
                :motor_speed_rpm, :hvb_temp_f
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
                "hvb_temp_f":       payload.get("hvb_temp_f"),
            },
        )
        conn.commit()

    with_retry(_write)


def handle_vision_frame(conn: sqlite3.Connection, payload: dict) -> None:
    trip_id = get_active_trip_id()
    if trip_id is None:
        log.debug("Vision frame received but no active trip — skipping")
        return

    # ts names the snapshot file and drives alignment with OBD readings —
    # a frame without a parseable timestamp is useless, skip it.
    try:
        ts_dt = datetime.fromisoformat(payload["ts"])
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"Vision frame with missing/bad ts — skipping: {e}")
        return

    try:
        jpeg_bytes = base64.b64decode(payload["jpeg_b64"], validate=True)
    except (KeyError, TypeError, ValueError) as e:
        log.warning(f"Vision frame with missing/bad jpeg_b64 — skipping: {e}")
        return

    # frame_id and source go into the filename; the broker accepts anonymous
    # publishers, so strip anything that could escape SNAPSHOT_DIR.
    frame_id = "".join(c for c in str(payload.get("frame_id") or "noid") if c.isalnum())[:8] or "noid"
    source   = payload.get("source") if payload.get("source") in ("periodic", "event") else "periodic"

    ts_compact = ts_dt.strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"
    # POSIX separator in the DB value — the Express bridge serves
    # /api/snapshots/<snapshot_path> relative to SNAPSHOT_DIR verbatim.
    rel_path = f"trip_{trip_id:06d}/{ts_compact}_{frame_id}_{source}.jpg"

    # File before row: a power cut may orphan a snapshot on disk, but must
    # never leave a row pointing at a file that doesn't exist.
    try:
        abs_path = SNAPSHOT_DIR / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = abs_path.with_suffix(".tmp")
        tmp_path.write_bytes(jpeg_bytes)
        os.replace(tmp_path, abs_path)
    except OSError as e:
        log.warning(f"Snapshot write failed for {rel_path} — skipping frame: {e}")
        return

    def _write():
        conn.execute(
            """
            INSERT INTO vision_frames (
                trip_id, ts, frame_id, source, snapshot_path,
                width_px, height_px, scene_label, confidence
            ) VALUES (
                :trip_id, :ts, :frame_id, :source, :snapshot_path,
                :width_px, :height_px, :scene_label, :confidence
            )
            """,
            {
                "trip_id":       trip_id,
                "ts":            payload.get("ts"),
                "frame_id":      payload.get("frame_id"),
                "source":        source,
                "snapshot_path": rel_path,
                "width_px":      payload.get("width_px"),
                "height_px":     payload.get("height_px"),
                "scene_label":   payload.get("scene_label"),
                "confidence":    payload.get("confidence"),
            },
        )
        conn.commit()

    with_retry(_write)
    log.debug(f"Vision frame recorded: {rel_path} for trip {trip_id}")


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

    # Now that ended_at is known, claim any footage that arrived before this
    # trip existed or while it had no end — the clip's own timestamps decide,
    # not whichever trip happened to be active when it was delivered.
    with_retry(lambda: reconcile_unassigned_clips(conn, trip_id))

    # And re-send the protect window, this time bounded, superseding the
    # open-ended one published when the crash was detected.
    row = conn.execute("SELECT footage_protected FROM trips WHERE id = ?",
                       (trip_id,)).fetchone()
    if row and row["footage_protected"]:
        publish_trip_protect_window(conn, trip_id)

    set_active_trip_id(None)

    # Trip close is the quiet moment to enforce the snapshot cap
    prune_snapshots(conn)


def reconcile_unassigned_clips(conn: sqlite3.Connection, trip_id: int) -> int:
    """Attach unassigned clips that overlap a just-closed trip.

    Same spirit as recover_unclosed_trips: the steady-state path can miss things
    (a clip closing before trip_open lands, or arriving while ended_at was still
    NULL), so there is a sweep that repairs it from the timestamps afterwards.
    """
    row = conn.execute("SELECT started_at, ended_at, footage_protected FROM trips WHERE id = ?",
                       (trip_id,)).fetchone()
    if not row:
        return 0
    trip_start = _parse_dt(row["started_at"])
    trip_end   = _parse_dt(row["ended_at"])
    if trip_start is None or trip_end is None:
        return 0

    window_start = trip_start - timedelta(seconds=TRIP_MATCH_LEAD_S)
    window_end   = trip_end + timedelta(seconds=TRIP_MATCH_TRAIL_S)
    protected = 1 if row["footage_protected"] else 0

    orphans = conn.execute(
        "SELECT clip_id, started_at, ended_at FROM dashcam_clips WHERE trip_id IS NULL"
    ).fetchall()

    claimed = []
    for clip in orphans:
        clip_start = _parse_dt(clip["started_at"])
        clip_end   = _parse_dt(clip["ended_at"]) or clip_start
        if clip_start is None:
            continue
        if (min(clip_end, window_end) - max(clip_start, window_start)).total_seconds() > 0:
            claimed.append(clip["clip_id"])

    if not claimed:
        return 0

    conn.executemany(
        "UPDATE dashcam_clips SET trip_id = ?, protected = MAX(protected, ?) WHERE clip_id = ?",
        [(trip_id, protected, c) for c in claimed])
    conn.commit()
    log.info(f"Claimed {len(claimed)} previously unassigned clip(s) for trip {trip_id}")
    return len(claimed)


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
            -- Fuel economy is total distance / total fuel, NOT the mean of
            -- per-reading MPG. Averaging instantaneous values weights a minute
            -- spent idling at a light (0 mpg) the same as a minute cruising,
            -- and skipping rows where fuel_rate_gph = 0 discarded every EV-mode
            -- reading — precisely the distance a hybrid covers on no fuel at
            -- all. Readings are a fixed 1 Hz, so each covers the same 1/3600 h
            -- and that interval cancels out of the ratio, leaving
            -- SUM(mph) / SUM(gph). Rows missing either field are excluded from
            -- both sums so numerator and denominator span the same readings.
            ROUND(
                SUM(CASE WHEN speed_mph IS NOT NULL AND fuel_rate_gph IS NOT NULL
                         THEN speed_mph END)
                / NULLIF(
                    SUM(CASE WHEN speed_mph IS NOT NULL AND fuel_rate_gph IS NOT NULL
                             THEN fuel_rate_gph END), 0
                ), 1
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
# Dashcam
# ---------------------------------------------------------------------------
# The Jetson records continuously and cannot know a trip id — trips.id is minted
# here from cursor.lastrowid and never published, and trip_open is not retained.
# So clips arrive stamped only with times, and this side does the linking.

def _parse_dt(value):
    """ISO-8601 → aware UTC datetime, or None."""
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def resolve_trip_for_clip(conn: sqlite3.Connection, clip_start, clip_end):
    """The trip whose window overlaps this clip the most, or None.

    Overlap matching rather than "whatever trip is active right now" is what
    makes backfill correct: when the Jetson reconnects after a cable drop it
    flushes every segment it recorded while offline, and those belong to the
    trips they were filmed during, not to the one happening at delivery time.
    """
    # Coarse SQL filter, exact arithmetic in Python. Comparing ISO strings is
    # only safe at day granularity here because fractional-second precision
    # varies between publishers — a one-day margin swamps that difference.
    lo = (clip_start - timedelta(days=1)).isoformat()
    hi = (clip_end + timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT id, started_at, ended_at FROM trips "
        "WHERE (started_at BETWEEN ? AND ?) OR ended_at IS NULL",
        (lo, hi),
    ).fetchall()

    best_id, best_overlap = None, 0.0
    for row in rows:
        trip_start = _parse_dt(row["started_at"])
        if trip_start is None:
            continue
        trip_end = _parse_dt(row["ended_at"]) or clip_end
        window_start = trip_start - timedelta(seconds=TRIP_MATCH_LEAD_S)
        window_end   = trip_end + timedelta(seconds=TRIP_MATCH_TRAIL_S)
        overlap = (min(clip_end, window_end) - max(clip_start, window_start)).total_seconds()
        if overlap > best_overlap:
            best_id, best_overlap = row["id"], overlap
    return best_id


def _clip_rel_path(clip_id: str) -> str:
    """Derive the Jetson-relative path from the clip id.

    DERIVED, not taken from the payload: the id already encodes the date, the
    layout is ours on both sides, and deriving means a hostile or corrupt `path`
    can never reach the URL the bridge builds. Mismatches are logged so a real
    layout change is visible rather than silent.
    """
    return f"{clip_id[0:4]}/{clip_id[4:6]}/{clip_id[6:8]}/{clip_id}.mp4"


def handle_dashcam_clip(conn: sqlite3.Connection, payload: dict) -> None:
    clip_id = str(payload.get("clip_id") or "")
    if not _CLIP_ID_RE.match(clip_id):
        log.warning(f"Dashcam clip with malformed clip_id {clip_id!r} — skipping")
        return

    started = _parse_dt(payload.get("started_at"))
    ended   = _parse_dt(payload.get("ended_at"))
    if started is None or ended is None:
        log.warning(f"Dashcam clip {clip_id} with missing/bad timestamps — skipping")
        return
    if ended < started:
        ended = started

    rel_path = _clip_rel_path(clip_id)
    if payload.get("path") and payload["path"] != rel_path:
        log.warning(f"Clip {clip_id} reported path {payload['path']!r} but the layout "
                    f"implies {rel_path!r} — using the derived path")

    trip_id = resolve_trip_for_clip(conn, started, ended)

    # A clip recorded during an already-flagged trip inherits its protection:
    # footage keeps arriving for minutes after the crash that protected the trip.
    protected = 0
    if trip_id is not None:
        row = conn.execute(
            "SELECT footage_protected FROM trips WHERE id = ?", (trip_id,)
        ).fetchone()
        protected = 1 if row and row["footage_protected"] else 0

    def _write():
        conn.execute(
            """
            INSERT INTO dashcam_clips (
                clip_id, trip_id, started_at, ended_at, duration_s, size_bytes,
                width_px, height_px, fps, clip_path, protected, state, created_at
            ) VALUES (
                :clip_id, :trip_id, :started_at, :ended_at, :duration_s, :size_bytes,
                :width_px, :height_px, :fps, :clip_path, :protected, 'available', :created_at
            )
            ON CONFLICT(clip_id) DO UPDATE SET
                trip_id    = excluded.trip_id,
                ended_at   = excluded.ended_at,
                duration_s = excluded.duration_s,
                size_bytes = excluded.size_bytes,
                protected  = MAX(dashcam_clips.protected, excluded.protected)
            """,
            {
                "clip_id":    clip_id,
                "trip_id":    trip_id,
                "started_at": started.isoformat(),
                "ended_at":   ended.isoformat(),
                "duration_s": payload.get("duration_s"),
                "size_bytes": payload.get("size_bytes"),
                "width_px":   payload.get("width_px"),
                "height_px":  payload.get("height_px"),
                "fps":        payload.get("fps"),
                "clip_path":  rel_path,
                "protected":  protected,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        conn.commit()

    with_retry(_write)
    log.info(f"Dashcam clip {clip_id} -> trip {trip_id if trip_id else 'unassigned'}"
             f"{' (protected)' if protected else ''}")


def handle_dashcam_pruned(conn: sqlite3.Connection, payload: dict) -> None:
    """The Jetson's retention pruner deleted files — drop the matching rows.

    The Jetson announcing its own deletions is what keeps the two sides in sync
    without a full inventory exchange: it is the only thing that deletes footage,
    so its announcements are complete by construction.
    """
    clip_ids = [c for c in (payload.get("clip_ids") or [])
                if isinstance(c, str) and _CLIP_ID_RE.match(c)]
    if not clip_ids:
        return

    def _write():
        conn.executemany("DELETE FROM dashcam_clips WHERE clip_id = ?",
                         [(c,) for c in clip_ids])
        conn.commit()

    with_retry(_write)
    log.info(f"Removed {len(clip_ids)} pruned clip row(s) ({payload.get('reason')})")


def handle_dashcam_command(conn: sqlite3.Connection, payload: dict) -> None:
    """Reflect a command the bridge issued.

    db_writer subscribes to the SAME topic Express publishes on, so Express can
    stay entirely out of the write path (single-writer invariant) while the UI
    still updates immediately: the row goes pending_delete here and is removed
    for real only once the Jetson confirms the file is gone.
    """
    action = str(payload.get("action", "")).lower()
    clip_ids = [c for c in (payload.get("clip_ids") or [])
                if isinstance(c, str) and _CLIP_ID_RE.match(c)]
    trip_id = payload.get("trip_id")

    if action == "delete":
        if not clip_ids:
            return

        def _write():
            conn.executemany(
                "UPDATE dashcam_clips SET state = 'pending_delete' WHERE clip_id = ?",
                [(c,) for c in clip_ids])
            conn.commit()

        with_retry(_write)
        log.info(f"{len(clip_ids)} clip(s) marked pending_delete")

    elif action in ("protect", "unprotect"):
        protected = 1 if action == "protect" else 0

        def _write():
            if trip_id is not None:
                conn.execute("UPDATE trips SET footage_protected = ? WHERE id = ?",
                             (protected, trip_id))
                conn.execute("UPDATE dashcam_clips SET protected = ? WHERE trip_id = ?",
                             (protected, trip_id))
            elif clip_ids:
                conn.executemany("UPDATE dashcam_clips SET protected = ? WHERE clip_id = ?",
                                 [(protected, c) for c in clip_ids])
            conn.commit()

        with_retry(_write)
        log.info(f"Footage {action}ed for "
                 f"{'trip ' + str(trip_id) if trip_id is not None else f'{len(clip_ids)} clip(s)'}")


def handle_dashcam_command_result(conn: sqlite3.Connection, payload: dict) -> None:
    """The Jetson confirmed a command. A confirmed delete is when the rows go."""
    if str(payload.get("action", "")).lower() != "delete" or not payload.get("ok"):
        return
    clip_ids = [c for c in (payload.get("clip_ids") or [])
                if isinstance(c, str) and _CLIP_ID_RE.match(c)]
    if not clip_ids:
        return

    def _write():
        conn.executemany("DELETE FROM dashcam_clips WHERE clip_id = ?",
                         [(c,) for c in clip_ids])
        conn.commit()

    with_retry(_write)
    log.info(f"{len(clip_ids)} clip(s) deleted on the Jetson — rows removed")


def handle_crash_event(conn: sqlite3.Connection, payload: dict) -> None:
    """Record a hard deceleration, and protect the trip's footage on a crash.

    trip_id may be NULL: crash_detector publishes live, so a db_writer that
    restarted mid-trip has no active trip to attribute it to. The event is still
    worth keeping — losing the record of a possible collision because of a
    process restart would be the wrong trade.
    """
    severity = str(payload.get("severity", ""))
    if severity not in ("hard_brake", "potential_crash"):
        log.warning(f"Crash event with unknown severity {severity!r} — skipping")
        return

    trip_id = get_active_trip_id()
    ts = payload.get("ts") or datetime.now(timezone.utc).isoformat()

    def _write():
        conn.execute(
            """
            INSERT INTO crash_events (
                trip_id, ts, severity, source, peak_decel_g,
                speed_before_mph, speed_after_mph, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip_id, ts, severity,
                payload.get("source", "obd_speed"),
                payload.get("peak_decel_g"),
                payload.get("speed_before_mph"),
                payload.get("speed_after_mph"),
                payload.get("detail"),
            ),
        )
        conn.commit()

    with_retry(_write)
    log.warning(f"Crash event: {severity} {payload.get('peak_decel_g')}g "
                f"for trip {trip_id if trip_id else 'unassigned'}")

    if severity != "potential_crash":
        return  # hard braking is logged for threshold tuning, but protects nothing
    if trip_id is None:
        log.error("Potential crash with no active trip — footage cannot be "
                  "protected automatically. Protect it by hand from the dashboard.")
        return

    def _protect():
        conn.execute(
            "UPDATE trips SET crash_count = COALESCE(crash_count, 0) + 1, "
            "footage_protected = 1 WHERE id = ?", (trip_id,))
        conn.execute("UPDATE dashcam_clips SET protected = 1 WHERE trip_id = ?", (trip_id,))
        conn.commit()

    with_retry(_protect)
    publish_trip_protect_window(conn, trip_id)


def publish_trip_protect_window(conn: sqlite3.Connection, trip_id: int) -> None:
    """Tell the Jetson to exempt this trip's footage from the retention purge.

    Sent as a time WINDOW rather than a clip list because footage keeps arriving
    after the crash — a list would only cover what already exists. `to` is null
    while the trip is open (protect everything from here on) and is re-sent
    bounded at trip_close.
    """
    row = conn.execute("SELECT started_at, ended_at FROM trips WHERE id = ?",
                       (trip_id,)).fetchone()
    if not row:
        return
    started = _parse_dt(row["started_at"])
    if started is None:
        return
    ended = _parse_dt(row["ended_at"])

    window = {
        "from": (started - timedelta(seconds=TRIP_MATCH_LEAD_S)).isoformat(),
        "to":   (ended + timedelta(seconds=TRIP_MATCH_TRAIL_S)).isoformat() if ended else None,
    }
    publish_dashcam_command({
        "request_id": f"protect-trip-{trip_id}",
        "action":     "protect",
        "trip_id":    trip_id,
        # Stable per trip so a repeat protect replaces the window rather than
        # accumulating one per crash event.
        "window_id":  f"trip-{trip_id:06d}",
        "window":     window,
    })
    log.info(f"Protect window published for trip {trip_id} "
             f"({window['from']} .. {window['to'] or 'open'})")

# ---------------------------------------------------------------------------
# MQTT setup
# ---------------------------------------------------------------------------

# db_writer is normally a pure subscriber. It publishes exactly one thing:
# dashcam protect windows, because it is the only process that knows which
# footage belongs to which trip.
_mqtt_client = None


def publish_dashcam_command(command: dict) -> None:
    if _mqtt_client is None:
        log.warning("No MQTT client — dashcam command dropped")
        return
    try:
        _mqtt_client.publish(f"{MQTT_DASHCAM_TOPIC_BASE}/command",
                             json.dumps(command), qos=1)
    except Exception as e:
        log.warning(f"Could not publish dashcam command: {e}")

def build_mqtt_client(conn: sqlite3.Connection) -> mqtt.Client:
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="db_writer")
    except AttributeError:
        client = mqtt.Client(client_id="db_writer")  # paho-mqtt < 2.0

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected — subscribing to topics")
            client.subscribe(f"{MQTT_TOPIC_BASE}/reading",    qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/trip_open",  qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/trip_close", qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/dtc",        qos=1)
            client.subscribe(f"{MQTT_TOPIC_BASE}/crash_event", qos=1)
            client.subscribe(f"{MQTT_VISION_TOPIC_BASE}/frame", qos=1)
            # Dashcam topics carry metadata only — never video bytes.
            client.subscribe(f"{MQTT_DASHCAM_TOPIC_BASE}/clip",           qos=1)
            client.subscribe(f"{MQTT_DASHCAM_TOPIC_BASE}/pruned",         qos=1)
            client.subscribe(f"{MQTT_DASHCAM_TOPIC_BASE}/command",        qos=1)
            client.subscribe(f"{MQTT_DASHCAM_TOPIC_BASE}/command_result", qos=1)
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
        elif topic.endswith("/crash_event"):
            handle_crash_event(conn, payload)
        elif topic.endswith("/vision/frame"):
            handle_vision_frame(conn, payload)
        # Ordered before /command so the longer suffix wins — "/command_result"
        # also ends with "command_result", but "/command" would not match it.
        elif topic.endswith("/dashcam/command_result"):
            handle_dashcam_command_result(conn, payload)
        elif topic.endswith("/dashcam/command"):
            handle_dashcam_command(conn, payload)
        elif topic.endswith("/dashcam/clip"):
            handle_dashcam_clip(conn, payload)
        elif topic.endswith("/dashcam/pruned"):
            handle_dashcam_pruned(conn, payload)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning(f"MQTT unexpected disconnect — rc={rc}")

    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect
    return client

def prune_snapshots(conn: sqlite3.Connection) -> None:
    """
    Keep vision_frames (and their JPEG files) bounded at MAX_SNAPSHOTS rows.
    Called at boot and after each trip_close — never per frame. Oldest rows
    go first (id order = insertion order under AUTOINCREMENT).

    Files are unlinked before their rows are deleted — the mirror image of
    handle_vision_frame's file-before-row insert. A crash mid-prune leaves
    some rows pointing at missing files, but the next run re-selects those
    same oldest rows and finishes the job (unlink tolerates already-missing
    files), so nothing leaks and nothing needs manual repair.
    """
    def _prune():
        row = conn.execute("SELECT COUNT(*) AS cnt FROM vision_frames").fetchone()
        excess = (row["cnt"] if row else 0) - MAX_SNAPSHOTS
        if excess <= 0:
            return

        doomed = conn.execute(
            "SELECT id, snapshot_path FROM vision_frames ORDER BY id ASC LIMIT ?",
            (excess,),
        ).fetchall()

        trip_dirs = set()
        for r in doomed:
            if r["snapshot_path"]:
                path = SNAPSHOT_DIR / r["snapshot_path"]
                try:
                    path.unlink(missing_ok=True)
                    trip_dirs.add(path.parent)
                except OSError as e:
                    # Undeletable file — leak it rather than let the DB grow.
                    log.warning(f"Could not delete snapshot {path}: {e}")

        # doomed is the oldest `excess` rows, so its last id is an upper
        # bound covering exactly that set — one statement, no giant IN list.
        conn.execute("DELETE FROM vision_frames WHERE id <= ?", (doomed[-1]["id"],))
        conn.commit()

        # Best-effort removal of now-empty per-trip directories
        for d in trip_dirs:
            try:
                d.rmdir()
            except OSError:
                pass  # not empty — still holds newer frames

        log.info(f"Pruned {len(doomed)} old snapshots (cap {MAX_SNAPSHOTS})")

    with_retry(_prune)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def recover_unclosed_trips(conn: sqlite3.Connection) -> None:
    """
    Close any trips left open by an unexpected shutdown (e.g., engine cutting
    power to the Pi before trip_manager could publish trip_close).
    Uses the last committed reading's timestamp as ended_at and computes
    the trip summary from whatever readings were saved.
    """
    unclosed = conn.execute(
        "SELECT id, started_at FROM trips WHERE ended_at IS NULL"
    ).fetchall()

    for row in unclosed:
        trip_id    = row["id"]
        started_at = row["started_at"]

        last = conn.execute(
            "SELECT ts FROM readings WHERE trip_id = ? ORDER BY ts DESC LIMIT 1",
            (trip_id,),
        ).fetchone()

        ended_at = last["ts"] if last else started_at

        duration = None
        try:
            start    = datetime.fromisoformat(started_at)
            end      = datetime.fromisoformat(ended_at)
            duration = int((end - start).total_seconds())
        except Exception:
            pass

        dtc_row   = conn.execute(
            "SELECT COUNT(*) as cnt FROM dtcs WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        dtc_count = dtc_row["cnt"] if dtc_row else 0

        conn.execute(
            "UPDATE trips SET ended_at = ?, duration_seconds = ?, dtc_count = ? WHERE id = ?",
            (ended_at, duration, dtc_count, trip_id),
        )
        conn.commit()

        compute_trip_summary(conn, trip_id)
        log.info(f"Recovered unclosed trip {trip_id} — ended_at={ended_at}")


def run() -> None:
    if not DB_PATH.exists():
        log.critical(
            f"Database not found at {DB_PATH}. "
            "Run db/migrate.py first."
        )
        sys.exit(1)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    log.info(f"SQLite connected — {DB_PATH}")

    recover_unclosed_trips(conn)
    prune_snapshots(conn)

    global _mqtt_client
    mqtt_client = build_mqtt_client(conn)
    _mqtt_client = mqtt_client

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
