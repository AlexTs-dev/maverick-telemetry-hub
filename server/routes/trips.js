const express = require('express');
const router  = express.Router();
const db      = require('../db');
 
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
                t.notes,
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
                t.notes,
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
 
module.exports = router;