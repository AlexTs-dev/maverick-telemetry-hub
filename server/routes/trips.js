const express = require('express');
const router  = express.Router();
const db      = require('../db');
const { publish } = require('../mqtt');
const { requestDelete, nextRequestId } = require('./videos');
 
// ---------------------------------------------------------------------------
// GET /api/trips
// All trips, most recent first, with summary stats joined in.
// Returns enough for a trip list view without a second request.
// ---------------------------------------------------------------------------
router.get('/', (req, res) => {
    try {
        const trips = db.prepare(`
            SELECT
                t.id,
                t.started_at,
                t.ended_at,
                t.duration_seconds,
                t.odometer_start,
                t.odometer_end,
                t.dtc_count,
                t.crash_count,
                t.footage_protected,
                t.notes,
                -- Clip count, so the trip list can flag footage without a
                -- second request. Cheap: idx_dashcam_clips_trip covers it.
                (SELECT COUNT(*) FROM dashcam_clips c
                  WHERE c.trip_id = t.id AND c.state != 'deleted') AS clip_count,
                -- summary columns (null if trip never closed cleanly)
                s.avg_speed_mph,
                s.max_speed_mph,
                s.avg_rpm,
                s.max_coolant_temp_f,
                s.ev_time_pct,
                s.total_regen_kwh,
                s.avg_fuel_economy_mpg,
                s.min_battery_soc_pct
            FROM trips t
            LEFT JOIN trip_summaries s ON s.trip_id = t.id
            ORDER BY t.started_at DESC
        `).all();
 
        res.json(trips);
    } catch (error) {
        console.error('GET /trips error:', error.message);
        res.status(500).json({ error: error.message });
    }
});
 
// ---------------------------------------------------------------------------
// GET /api/trips/:id
// Single trip with summary — same join as above but one row.
// ---------------------------------------------------------------------------
router.get('/:id', (req, res) => {
    try {
        const trip = db.prepare(`
            SELECT
                t.id,
                t.started_at,
                t.ended_at,
                t.duration_seconds,
                t.odometer_start,
                t.odometer_end,
                t.dtc_count,
                t.crash_count,
                t.footage_protected,
                t.notes,
                -- Clip count, so the trip list can flag footage without a
                -- second request. Cheap: idx_dashcam_clips_trip covers it.
                (SELECT COUNT(*) FROM dashcam_clips c
                  WHERE c.trip_id = t.id AND c.state != 'deleted') AS clip_count,
                s.avg_speed_mph,
                s.max_speed_mph,
                s.avg_rpm,
                s.max_coolant_temp_f,
                s.ev_time_pct,
                s.total_regen_kwh,
                s.avg_fuel_economy_mpg,
                s.min_battery_soc_pct
            FROM trips t
            LEFT JOIN trip_summaries s ON s.trip_id = t.id
            WHERE t.id = ?
        `).get(req.params.id);
 
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }
 
        res.json(trip);
    } catch (error) {
        console.error(`GET /trips/${req.params.id} error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});
 
// ---------------------------------------------------------------------------
// GET /api/trips/:id/readings
// All sensor readings for a trip, chronological.
// Can be large for long trips — consider adding ?limit and ?offset
// pagination later if the dashboard becomes slow to load.
// ---------------------------------------------------------------------------
router.get('/:id/readings', (req, res) => {
    try {
        // Confirm trip exists first
        const trip = db.prepare('SELECT id FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }
 
        const readings = db.prepare(`
            SELECT
                id,
                ts,
                rpm,
                speed_mph,
                coolant_temp_f,
                throttle_pct,
                fuel_rate_gph,
                battery_soc_pct,
                hvb_temp_f,
                pack_voltage_v
            FROM readings
            WHERE trip_id = ?
            ORDER BY ts ASC
        `).all(req.params.id);
 
        res.json(readings);
    } catch (error) {
        console.error(`GET /trips/${req.params.id}/readings error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});
 
// ---------------------------------------------------------------------------
// GET /api/trips/:id/dtcs
// Fault codes recorded during this trip.
// Includes claude_diagnosis if it has already been fetched.
// ---------------------------------------------------------------------------
router.get('/:id/dtcs', (req, res) => {
    try {
        const trip = db.prepare('SELECT id FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }
 
        const dtcs = db.prepare(`
            SELECT
                id,
                code,
                first_seen_at,
                claude_diagnosis,
                diagnosed_at
            FROM dtcs
            WHERE trip_id = ?
            ORDER BY first_seen_at ASC
        `).all(req.params.id);
 
        res.json(dtcs);
    } catch (error) {
        console.error(`GET /trips/${req.params.id}/dtcs error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// GET /api/trips/:id/vision
// All vision events for a trip, chronological.
// ---------------------------------------------------------------------------
router.get('/:id/vision', (req, res) => {
    try {
        // Confirm trip exists first
        const trip = db.prepare('SELECT id FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }
 
        const visionFrames = db.prepare(`
            SELECT
                id, 
                ts, 
                frame_id, 
                source, 
                width_px, 
                height_px, 
                scene_label, 
                confidence, 
                snapshot_path
            FROM vision_frames
            WHERE trip_id = ?
            ORDER BY ts ASC
        `).all(req.params.id);

        // snapshot_path is a filesystem fact (relative to MAVERICK_SNAPSHOT_DIR);
        // the URL is a routing fact — derive it here so the DB never hardcodes routes.
        res.json(visionFrames.map(({ snapshot_path, ...frame }) => ({
            ...frame,
            snapshot_url: snapshot_path ? '/api/snapshots/' + snapshot_path : null,
        })));
 
    } catch (error) {
        console.error(`GET /trips/${req.params.id}/vision error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// GET /api/trips/:id/videos
// Dashcam clips recorded during this trip, chronological.
// ---------------------------------------------------------------------------
router.get('/:id/videos', (req, res) => {
    try {
        const trip = db.prepare('SELECT id FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }

        const clips = db.prepare(`
            SELECT
                id,
                clip_id,
                started_at,
                ended_at,
                duration_s,
                size_bytes,
                width_px,
                height_px,
                fps,
                protected,
                state
            FROM dashcam_clips
            WHERE trip_id = ? AND state != 'deleted'
            ORDER BY started_at ASC
        `).all(req.params.id);

        // The file lives on the Jetson; the client only ever sees a Pi URL that
        // this bridge proxies. Derived here for the same reason as snapshot_url.
        res.json(clips.map((clip) => ({
            ...clip,
            video_url: `/api/videos/${clip.clip_id}/stream`,
        })));
    } catch (error) {
        console.error(`GET /trips/${req.params.id}/videos error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// GET /api/trips/:id/crash-events
// Hard decelerations detected during this trip. severity is 'hard_brake'
// (logged only) or 'potential_crash' (also protects this trip's footage).
// ---------------------------------------------------------------------------
router.get('/:id/crash-events', (req, res) => {
    try {
        const trip = db.prepare('SELECT id FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }

        const events = db.prepare(`
            SELECT
                id,
                ts,
                severity,
                source,
                peak_decel_g,
                speed_before_mph,
                speed_after_mph,
                detail
            FROM crash_events
            WHERE trip_id = ?
            ORDER BY ts ASC
        `).all(req.params.id);

        res.json(events);
    } catch (error) {
        console.error(`GET /trips/${req.params.id}/crash-events error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// DELETE /api/trips/:id/videos
// All footage for this trip. Protected footage needs ?force=1 — a crash is
// exactly the case where an accidental bulk delete would be unrecoverable.
//
// 202, not 200: the row is not gone yet. Express cannot write SQLite (the
// single-writer invariant), so this publishes a command — db_writer marks the
// rows pending_delete, the Jetson removes the files and confirms, and the rows
// disappear then.
// ---------------------------------------------------------------------------
router.delete('/:id/videos', (req, res) => {
    try {
        const trip = db.prepare('SELECT id FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }

        const force = req.query.force === '1';
        const clips = db.prepare(`
            SELECT clip_id, protected FROM dashcam_clips
            WHERE trip_id = ? AND state != 'deleted'
        `).all(req.params.id);

        const blocked = clips.filter((c) => c.protected).length;
        if (blocked > 0 && !force) {
            return res.status(409).json({
                error: `${blocked} clip(s) are protected by a crash event. `
                     + 'Retry with ?force=1 to delete them anyway.',
                protected_count: blocked,
            });
        }

        const requested = requestDelete(clips.map((c) => c.clip_id), {
            trip_id: Number(req.params.id),
        });
        res.status(202).json({ requested, status: 'pending' });
    } catch (error) {
        console.error(`DELETE /trips/${req.params.id}/videos error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// POST /api/trips/:id/videos/protect   body: { protected: boolean }
// Manual protect / unprotect. Unprotecting is how footage a crash saved gets
// released back to the retention purge, so it is the necessary counterpart to
// automatic protection rather than a convenience.
// ---------------------------------------------------------------------------
router.post('/:id/videos/protect', (req, res) => {
    try {
        const trip = db.prepare('SELECT id, started_at, ended_at FROM trips WHERE id = ?')
                       .get(req.params.id);
        if (!trip) {
            return res.status(404).json({ error: 'Trip not found' });
        }

        const wanted = req.body?.protected;
        if (typeof wanted !== 'boolean') {
            return res.status(400).json({ error: 'Body must be { protected: boolean }' });
        }

        const tripId = Number(req.params.id);
        const windowId = `trip-${String(tripId).padStart(6, '0')}`;

        if (wanted) {
            // A window, not a clip list: footage recorded after this call must
            // be protected too, and a list could only cover what exists now.
            const lead = 60 * 1000;
            const trail = 5 * 60 * 1000;
            const from = new Date(new Date(trip.started_at).getTime() - lead).toISOString();
            const to = trip.ended_at
                ? new Date(new Date(trip.ended_at).getTime() + trail).toISOString()
                : null;
            publish('maverick/dashcam/command', {
                request_id: nextRequestId('protect'),
                action:     'protect',
                trip_id:    tripId,
                window_id:  windowId,
                window:     { from, to },
            });
        } else {
            publish('maverick/dashcam/command', {
                request_id: nextRequestId('unprotect'),
                action:     'unprotect',
                trip_id:    tripId,
                window_id:  windowId,
            });
        }

        res.status(202).json({ protected: wanted, status: 'pending' });
    } catch (error) {
        console.error(`POST /trips/${req.params.id}/videos/protect error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

module.exports = router;