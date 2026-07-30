"""
db/migrate.py
Maverick Telemetry Hub

Creates the SQLite database and all tables if they don't exist.
Safe to run multiple times — uses CREATE IF NOT EXISTS throughout.

Run once before starting any services:
    python db/migrate.py

Run again after schema changes — existing data is preserved.
"""

import os
import sqlite3
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_default_db = Path(__file__).resolve().parent.parent / "maverick_telemetry.db"
DB_PATH = Path(os.environ.get("MAVERICK_DB_PATH", _default_db))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("migrate")

# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
# Each key is a schema version integer; value is the SQL to apply.
# V1 uses CREATE IF NOT EXISTS so it is safe to run on a fresh DB.
# V2+ use ALTER TABLE — SQLite ignores duplicate column errors via
# the try/except in _apply_migration.

VERSION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
"""

MIGRATIONS = {
    1: """
-- one row per ignition cycle
CREATE TABLE IF NOT EXISTS trips (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    duration_seconds INTEGER,
    odometer_start   REAL,
    odometer_end     REAL,
    dtc_count        INTEGER DEFAULT 0,
    notes            TEXT
);

-- raw per-second sensor stream
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         INTEGER NOT NULL REFERENCES trips(id),
    ts              TEXT NOT NULL,
    rpm             REAL,
    speed_mph       REAL,
    coolant_temp_f  REAL,
    throttle_pct    REAL,
    battery_soc_pct REAL,
    ev_mode         INTEGER,
    regen_kw        REAL,
    fuel_rate_gph   REAL
);

-- fault codes are events, not samples
CREATE TABLE IF NOT EXISTS dtcs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id          INTEGER NOT NULL REFERENCES trips(id),
    code             TEXT NOT NULL,
    first_seen_at    TEXT NOT NULL,
    claude_diagnosis TEXT,
    diagnosed_at     TEXT
);

-- computed once on trip close, queried often
CREATE TABLE IF NOT EXISTS trip_summaries (
    trip_id              INTEGER PRIMARY KEY REFERENCES trips(id),
    avg_speed_mph        REAL,
    max_speed_mph        REAL,
    avg_rpm              REAL,
    max_coolant_temp_f   REAL,
    ev_time_pct          REAL,
    total_regen_kwh      REAL,
    avg_fuel_economy_mpg REAL,
    min_battery_soc_pct  REAL
);

CREATE INDEX IF NOT EXISTS idx_readings_trip  ON readings(trip_id, ts);
CREATE INDEX IF NOT EXISTS idx_trips_started  ON trips(started_at);
CREATE INDEX IF NOT EXISTS idx_dtcs_code      ON dtcs(code);
CREATE INDEX IF NOT EXISTS idx_dtcs_trip      ON dtcs(trip_id);
""",

    2: """
-- Raw hybrid sensor columns (Ford Mode 22 PIDs 480B, 480C, 4A15).
-- Used to derive ev_mode and regen_kw in obd_poller stored here for
-- post-trip analysis without re-deriving from the computed fields.
ALTER TABLE readings ADD COLUMN pack_voltage_v    REAL;
ALTER TABLE readings ADD COLUMN battery_current_a REAL;
ALTER TABLE readings ADD COLUMN motor_speed_rpm   INTEGER;
""",

    3: """
-- HV battery pack temperature (Ford BECM Mode 22 DID 4808, byte D = average cell
-- temp, raw-50 = °C). Stored in °F to match coolant_temp_f. SOC and pack voltage
-- already have columns (battery_soc_pct v1, pack_voltage_v v2), now polled too.
ALTER TABLE readings ADD COLUMN hvb_temp_f REAL;
""",

    4: """
-- AI-vision boilerplate: snapshots from the Jetson Orin Nano (direct ethernet).
-- One row per frame received on maverick/vision/frame. Whole-frame scene
-- classification (no bounding boxes) — scene_label/confidence stay NULL until
-- real inference ships. confidence is a 0-1 fraction (ML convention, not _pct).
-- snapshot_path is relative to MAVERICK_SNAPSHOT_DIR. db_writer owns the files
-- as well as the rows (single-writer invariant covers the snapshot dir too).
-- NOTE: _apply_migration splits migrations on semicolons — never put one
-- inside a comment, only at true statement boundaries.
CREATE TABLE IF NOT EXISTS vision_frames (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id       INTEGER NOT NULL REFERENCES trips(id),
    ts            TEXT NOT NULL,
    frame_id      TEXT,
    source        TEXT NOT NULL DEFAULT 'periodic',
    snapshot_path TEXT,
    width_px      INTEGER,
    height_px     INTEGER,
    scene_label   TEXT,
    confidence    REAL
);

CREATE INDEX IF NOT EXISTS idx_vision_frames_trip ON vision_frames(trip_id, ts);
    """,

    5: """
-- Dashcam: footage recorded on the Jetson, plus the crash events that protect it.
--
-- The FOOTAGE ITSELF NEVER LIVES ON THE PI. The Jetson has the disk, so clips
-- stay there and clip_path is relative to its clip root -- the Express bridge
-- proxies range requests to clip_server.py on the Jetson. These rows are
-- metadata and the authoritative trip linkage, nothing more.
--
-- trip_id is NULLABLE here, unlike readings/dtcs/vision_frames. Recording runs
-- whenever the Jetson is powered, so a clip can legitimately exist outside any
-- trip (engine off, or before RPM crosses the threshold that opens one).
-- Dropping those would strand files on disk that the dashboard could neither
-- show nor delete, so they are kept and surfaced as unassigned footage instead.
--
-- state tracks a delete in flight: Express cannot write the DB (single-writer
-- invariant), so a delete is an MQTT command -- the row goes pending_delete
-- immediately and is removed only once the Jetson confirms the file is gone.
-- NOTE: _apply_migration splits migrations on semicolons -- never put one
-- inside a comment, only at true statement boundaries.
CREATE TABLE IF NOT EXISTS dashcam_clips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     TEXT NOT NULL UNIQUE,
    trip_id     INTEGER REFERENCES trips(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    duration_s  REAL,
    size_bytes  INTEGER,
    width_px    INTEGER,
    height_px   INTEGER,
    fps         REAL,
    clip_path   TEXT NOT NULL,
    protected   INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'available',
    created_at  TEXT NOT NULL
);

-- Hard decelerations detected from the OBD speed stream by crash_detector.py.
-- severity is 'hard_brake' (logged only) or 'potential_crash' (also protects
-- the trip's footage from the retention purge).
--
-- source is deliberately a column rather than an assumption: 1Hz OBD speed is a
-- coarse instrument, and an IMU publishing to the same topic later should not
-- need a schema change.
CREATE TABLE IF NOT EXISTS crash_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id          INTEGER REFERENCES trips(id),
    ts               TEXT NOT NULL,
    severity         TEXT NOT NULL,
    source           TEXT NOT NULL DEFAULT 'obd_speed',
    peak_decel_g     REAL,
    speed_before_mph REAL,
    speed_after_mph  REAL,
    detail           TEXT
);

-- Denormalised onto trips so the trip list can badge a crash without a join,
-- mirroring how dtc_count already works.
ALTER TABLE trips ADD COLUMN crash_count INTEGER DEFAULT 0;
ALTER TABLE trips ADD COLUMN footage_protected INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_dashcam_clips_trip    ON dashcam_clips(trip_id, started_at);
CREATE INDEX IF NOT EXISTS idx_dashcam_clips_started ON dashcam_clips(started_at);
CREATE INDEX IF NOT EXISTS idx_crash_events_trip     ON crash_events(trip_id, ts);
""",

    6: """
-- Backfill for the fuel-rate unit bug. obd_poller._to_gph used to store
-- python-obd's FUEL_RATE magnitude straight into fuel_rate_gph, but that
-- decoder returns LITRES per hour, so every row logged before the fix is
-- 3.785411784x too large and every MPG derived from it 3.785x too small.
--
-- ORDERING IS LOAD-BEARING. This divides every existing reading exactly once,
-- and cannot tell a converted row from an unconverted one. Stop db_writer and
-- obd_poller BEFORE running it, and deploy the fixed poller before starting
-- them again -- rows written in litres after this runs stay wrong, and rows
-- written in gallons before it runs get divided a second time. The same trap
-- applies to dev databases: seed.sql has always held true gallons, so seed a
-- fresh DB only AFTER migrating, which is the existing order anyway.
--
-- The summaries are then recomputed from the corrected readings using the same
-- distance-over-fuel formula db_writer now applies at trip close, so historical
-- trips match newly closed ones. Trips whose readings predate fuel_rate logging
-- entirely divide by a NULL sum and correctly stay NULL.
-- NOTE: _apply_migration splits migrations on semicolons -- never put one
-- inside a comment, only at true statement boundaries.
UPDATE readings
   SET fuel_rate_gph = ROUND(fuel_rate_gph / 3.785411784, 3)
 WHERE fuel_rate_gph IS NOT NULL;

UPDATE trip_summaries
   SET avg_fuel_economy_mpg = (
       SELECT ROUND(
           SUM(CASE WHEN r.speed_mph IS NOT NULL AND r.fuel_rate_gph IS NOT NULL
                    THEN r.speed_mph END)
           / NULLIF(
               SUM(CASE WHEN r.speed_mph IS NOT NULL AND r.fuel_rate_gph IS NOT NULL
                        THEN r.fuel_rate_gph END), 0
           ), 1
       )
         FROM readings r
        WHERE r.trip_id = trip_summaries.trip_id
   );
"""
}

CURRENT_VERSION = max(MIGRATIONS)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def get_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(version) as v FROM schema_version"
    ).fetchone()
    return row[0] if row and row[0] is not None else 0


def _apply_migration(conn: sqlite3.Connection, version: int, sql: str) -> None:
    log.info(f"Applying migration v{version}...")
    for statement in sql.strip().split(";"):
        statement = statement.strip()
        if not statement:
            continue
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as e:
            # ALTER TABLE ADD COLUMN fails if the column already exists;
            # treat that as a no-op so re-runs are safe.
            if "duplicate column" in str(e).lower():
                log.debug(f"Column already exists, skipping: {e}")
            else:
                raise
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
        (version,),
    )
    conn.commit()
    log.info(f"Migration v{version} applied")


def run() -> None:
    log.info(f"Database path: {DB_PATH}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(VERSION_TABLE)

        current = get_schema_version(conn)
        log.info(f"Current schema version: {current}")

        pending = [v for v in sorted(MIGRATIONS) if v > current]
        if not pending:
            log.info("Schema is up to date — nothing to do")
            return

        for version in pending:
            _apply_migration(conn, version, MIGRATIONS[version])

        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        log.info(f"Tables: {[t[0] for t in tables]}")

    except sqlite3.Error as e:
        log.critical(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    log.info("migrate.py starting")
    run()


