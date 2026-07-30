/**
 * server/mock.js
 * Maverick Telemetry Hub — development mock server
 *
 * Replaces index.js during local dev. No MQTT, no SQLite, no Pi needed.
 * Streams fake live readings via WebSocket at 1Hz and serves stub API routes.
 *
 * Start with: node server/mock.js
 * Then run:   npm run dev  (in client/)
 */

const express  = require('express');
const http     = require('http');
const { WebSocketServer } = require('ws');

const PORT = process.env.PORT || 3000;

// ---------------------------------------------------------------------------
// Fake data generator
// Simulates a Maverick FHEV driving loop: accelerate → cruise → regen → stop
// ---------------------------------------------------------------------------

let tick = 0;

function nextReading() {
    const t       = tick++;
    const phase   = t % 60                      // 60-second drive cycle
    const evPhase = phase >= 45 && phase < 55   // EV/regen window

    // Engine
    const rpm       = evPhase ? 0 : Math.round(800 + Math.sin(t / 8) * 600 + phase * 28)
    const speed_mph = evPhase
        ? Math.max(0, 35 - (phase - 45) * 3.5)
        : Math.min(65, phase * 1.1 + Math.sin(t / 5) * 3)

    // Temperatures
    const coolant_temp_f = 185 + Math.sin(t / 30) * 8
    const throttle_pct   = evPhase ? 0 : Math.max(0, 15 + Math.sin(t / 6) * 12)

    // Hybrid battery
    const battery_soc_pct  = 65 + Math.sin(t / 120) * 8
    const pack_voltage_v    = 238 + Math.sin(t / 40) * 4
    const hvb_temp_f        = 92 + Math.sin(t / 90) * 6   // HV pack avg temp

    // Current: negative = regen/charging, positive = discharging (EV assist)
    const battery_current_a = evPhase
        ? -(20 + Math.sin(t / 3) * 15)
        : (rpm > 0 ? 15 + Math.sin(t / 7) * 10 : 3)

    const motor_speed_rpm = evPhase
        ? Math.round(speed_mph * 32)
        : Math.round(rpm * 0.65)

    // Derived
    const ev_mode  = (motor_speed_rpm > 50 && rpm < 100) ? 1 : 0
    const regen_kw = battery_current_a < 0
        ? Math.round(pack_voltage_v * Math.abs(battery_current_a) / 1000 * 1000) / 1000
        : 0
    const fuel_rate_gph = evPhase ? 0 : Math.max(0, 0.3 + (rpm / 4000) * 1.4)

    return {
        ts:               new Date().toISOString(),
        rpm:              Math.round(rpm),
        speed_mph:        Math.round(speed_mph * 10) / 10,
        coolant_temp_f:   Math.round(coolant_temp_f * 10) / 10,
        throttle_pct:     Math.round(throttle_pct * 10) / 10,
        battery_soc_pct:  Math.round(battery_soc_pct * 10) / 10,
        ev_mode,
        regen_kw,
        fuel_rate_gph:    Math.round(fuel_rate_gph * 1000) / 1000,
        pack_voltage_v:   Math.round(pack_voltage_v * 10) / 10,
        battery_current_a: Math.round(battery_current_a * 10) / 10,
        motor_speed_rpm,
        hvb_temp_f:       Math.round(hvb_temp_f * 10) / 10,
    }
}

// Pre-fill a catchup buffer so new clients see a populated chart immediately
const CATCHUP_SECONDS = 60
const catchupBuffer = []
for (let i = 0; i < CATCHUP_SECONDS; i++) nextReading() // warm up phase
for (let i = 0; i < CATCHUP_SECONDS; i++) {
    catchupBuffer.push({
        topic:      'maverick/telemetry/reading',
        message:    nextReading(),
        receivedAt: new Date(Date.now() - (CATCHUP_SECONDS - i) * 1000).toISOString(),
    })
}

// ---------------------------------------------------------------------------
// Static mock data — mirrors db/seed.sql
// ---------------------------------------------------------------------------

const TRIPS = [
    {
        id: 1, started_at: '2026-05-28T07:45:00+00:00', ended_at: '2026-05-28T08:03:00+00:00',
        duration_seconds: 1080, odometer_start: 4821.3, odometer_end: 4829.6, dtc_count: 0,
        crash_count: 0, footage_protected: 0, clip_count: 3,
        notes: 'Morning commute',
        avg_speed_mph: 24.3, max_speed_mph: 54.5, avg_rpm: 882.1, max_coolant_temp_f: 203.0,
        ev_time_pct: 26.3, total_regen_kwh: 0.0083, avg_fuel_economy_mpg: 38.4, min_battery_soc_pct: 70.9,
    },
    {
        id: 2, started_at: '2026-05-27T14:20:00+00:00', ended_at: '2026-05-27T14:32:00+00:00',
        duration_seconds: 720, odometer_start: 4809.1, odometer_end: 4814.8, dtc_count: 1,
        crash_count: 0, footage_protected: 0, clip_count: 0,
        notes: 'Grocery run',
        avg_speed_mph: 14.5, max_speed_mph: 28.5, avg_rpm: 257.7, max_coolant_temp_f: 191.0,
        ev_time_pct: 69.2, total_regen_kwh: 0.0024, avg_fuel_economy_mpg: 52.1, min_battery_soc_pct: 78.5,
    },
    {
        id: 3, started_at: '2026-05-26T17:00:00+00:00', ended_at: '2026-05-26T17:35:00+00:00',
        duration_seconds: 2100, odometer_start: 4774.2, odometer_end: 4809.1, dtc_count: 0,
        crash_count: 1, footage_protected: 1, clip_count: 3,
        notes: 'Highway to trailhead',
        avg_speed_mph: 52.8, max_speed_mph: 72.0, avg_rpm: 1939.3, max_coolant_temp_f: 205.0,
        ev_time_pct: 7.1, total_regen_kwh: 0.0073, avg_fuel_economy_mpg: 34.2, min_battery_soc_pct: 61.5,
    },
]

const DTCS = [
    {
        id: 1, trip_id: 2, code: 'P0D0B',
        first_seen_at: '2026-05-27T14:24:00+00:00',
        claude_diagnosis: 'P0D0B — High Voltage Battery Pack Deterioration. Indicates HV battery capacity has dropped below expected threshold. Urgency: LOW — monitor SOC trends.',
        diagnosed_at: '2026-05-27T14:35:00+00:00',
        trip_started_at: '2026-05-27T14:20:00+00:00',
    },
]

// Dashcam clips — mirrors db/seed.sql. Trip 1 has plain footage, trip 3 has
// footage protected by a crash, trip 2 has none (empty state), and one clip is
// unassigned (recorded outside any trip, which is a normal case). The streams
// 404 in dev, which is deliberate — it exercises the player's error state.
let CLIPS = [
    { id: 1, clip_id: '20260528T074500Z_11aa22bb', trip_id: 1, started_at: '2026-05-28T07:45:00+00:00', ended_at: '2026-05-28T07:46:00+00:00', duration_s: 60, size_bytes: 62914560, width_px: 1920, height_px: 1080, fps: 30, protected: 0, state: 'available' },
    { id: 2, clip_id: '20260528T074600Z_22bb33cc', trip_id: 1, started_at: '2026-05-28T07:46:00+00:00', ended_at: '2026-05-28T07:47:00+00:00', duration_s: 60, size_bytes: 63438848, width_px: 1920, height_px: 1080, fps: 30, protected: 0, state: 'available' },
    { id: 3, clip_id: '20260528T074700Z_33cc44dd', trip_id: 1, started_at: '2026-05-28T07:47:00+00:00', ended_at: '2026-05-28T07:48:00+00:00', duration_s: 60, size_bytes: 61865984, width_px: 1920, height_px: 1080, fps: 30, protected: 0, state: 'available' },
    { id: 4, clip_id: '20260526T171500Z_44dd55ee', trip_id: 3, started_at: '2026-05-26T17:15:00+00:00', ended_at: '2026-05-26T17:16:00+00:00', duration_s: 60, size_bytes: 64487424, width_px: 1920, height_px: 1080, fps: 30, protected: 1, state: 'available' },
    { id: 5, clip_id: '20260526T171600Z_55ee66ff', trip_id: 3, started_at: '2026-05-26T17:16:00+00:00', ended_at: '2026-05-26T17:16:42+00:00', duration_s: 42, size_bytes: 44040192, width_px: 1920, height_px: 1080, fps: 30, protected: 1, state: 'available' },
    { id: 6, clip_id: '20260526T171642Z_66ff7700', trip_id: 3, started_at: '2026-05-26T17:16:42+00:00', ended_at: '2026-05-26T17:17:42+00:00', duration_s: 60, size_bytes: 62914560, width_px: 1920, height_px: 1080, fps: 30, protected: 1, state: 'available' },
    { id: 7, clip_id: '20260529T120000Z_77008811', trip_id: null, started_at: '2026-05-29T12:00:00+00:00', ended_at: '2026-05-29T12:01:00+00:00', duration_s: 60, size_bytes: 60817408, width_px: 1920, height_px: 1080, fps: 30, protected: 0, state: 'available' },
]

const CRASH_EVENTS = [
    { id: 1, trip_id: 3, ts: '2026-05-26T17:16:42+00:00', severity: 'potential_crash', source: 'obd_speed', peak_decel_g: 1.34, speed_before_mph: 52.0, speed_after_mph: 0.0, detail: '52.0 -> 0.0 mph (1.34g)' },
    { id: 2, trip_id: 1, ts: '2026-05-28T07:52:10+00:00', severity: 'hard_brake', source: 'obd_speed', peak_decel_g: 0.62, speed_before_mph: 38.0, speed_after_mph: 11.0, detail: '38.0 -> 11.0 mph (0.62g)' },
]

const withVideoUrl = (c) => ({ ...c, video_url: `/api/videos/${c.clip_id}/stream` })

// Vision detections from the Jetson. A scene_label prefixed "speed_limit_"
// came from the YOLO sign pipeline; anything else is a whole-frame scene label.
// snapshot_url deliberately points at files that don't exist in dev — that
// exercises the label-only fallback rather than a broken-image icon.
const VISION = [
    { id: 1, trip_id: 1, ts: '2026-05-28T07:46:30+00:00', frame_id: 'a1b2c3d4', source: 'event', width_px: 1280, height_px: 720, scene_label: 'speed_limit_35', confidence: 0.88, snapshot_path: 'trip_000001/20260528T074630000Z_a1b2c3d4_event.jpg' },
    { id: 2, trip_id: 1, ts: '2026-05-28T07:52:10+00:00', frame_id: 'b2c3d4e5', source: 'event', width_px: 1280, height_px: 720, scene_label: 'speed_limit_50', confidence: 0.79, snapshot_path: 'trip_000001/20260528T075210000Z_b2c3d4e5_event.jpg' },
    { id: 3, trip_id: 1, ts: '2026-05-28T07:58:02+00:00', frame_id: 'c3d4e5f6', source: 'event', width_px: 1280, height_px: 720, scene_label: 'residential', confidence: 0.71, snapshot_path: 'trip_000001/20260528T075802000Z_c3d4e5f6_event.jpg' },
    { id: 4, trip_id: 3, ts: '2026-05-26T17:08:00+00:00', frame_id: 'd4e5f607', source: 'event', width_px: 1280, height_px: 720, scene_label: 'speed_limit_65', confidence: 0.93, snapshot_path: 'trip_000003/20260526T170800000Z_d4e5f607_event.jpg' },
    { id: 5, trip_id: 3, ts: '2026-05-26T17:12:30+00:00', frame_id: 'e5f60718', source: 'event', width_px: 1280, height_px: 720, scene_label: 'highway', confidence: 0.85, snapshot_path: 'trip_000003/20260526T171230000Z_e5f60718_event.jpg' },
]

// ---------------------------------------------------------------------------
// Express — API routes
// ---------------------------------------------------------------------------

const app    = express();
const server = http.createServer(app);

app.use(express.json())

app.get('/api/health', (req, res) => {
    res.json({ status: 'ok', timestamp: new Date().toISOString(), mqtt: 'mock' })
})

// Version + update check — dev stub. Pretends a newer release exists so the
// update banner is visible; POST /api/version/update clears it after a moment.
let mockUpdateAvailable = true
app.get('/api/version', (req, res) => {
    res.json({
        current:         'deploy-99860a3',
        latest:          mockUpdateAvailable ? 'deploy-a1b2c3d' : 'deploy-99860a3',
        updateAvailable: mockUpdateAvailable,
        checkedAt:       new Date().toISOString(),
    })
})
app.post('/api/version/update', (req, res) => {
    setTimeout(() => { mockUpdateAvailable = false }, 3000)
    res.json({ status: 'started' })
})

app.get('/api/trips', (req, res) => res.json(TRIPS))

app.get('/api/trips/:id', (req, res) => {
    const trip = TRIPS.find(t => t.id === Number(req.params.id))
    return trip ? res.json(trip) : res.status(404).json({ error: 'Trip not found' })
})

app.get('/api/trips/:id/readings', (req, res) => res.json([]))

app.get('/api/trips/:id/dtcs', (req, res) => {
    res.json(DTCS.filter(d => d.trip_id === Number(req.params.id)))
})

app.get('/api/dtcs', (req, res) => res.json(DTCS))

app.post('/api/dtcs/:id/diagnose', (req, res) => {
    const dtc = DTCS.find(d => d.id === Number(req.params.id))
    if (!dtc) return res.status(404).json({ error: 'DTC not found' })
    res.json({ code: dtc.code, diagnosis: dtc.claude_diagnosis, diagnosed_at: dtc.diagnosed_at, cached: true })
})

// ---------------------------------------------------------------------------
// Dashcam — stubs for every real route.
//
// Deletes here apply immediately, unlike production, where they are an MQTT
// round trip through db_writer and the Jetson. The client sees the same 202 in
// both cases, so its optimistic-update path is still what gets exercised.
// ---------------------------------------------------------------------------

app.get('/api/trips/:id/videos', (req, res) => {
    res.json(CLIPS.filter(c => c.trip_id === Number(req.params.id)).map(withVideoUrl))
})

app.get('/api/trips/:id/crash-events', (req, res) => {
    res.json(CRASH_EVENTS.filter(e => e.trip_id === Number(req.params.id)))
})

// Mirrors the real route: snapshot_path is a filesystem fact, snapshot_url is
// a routing fact derived here so the DB never hardcodes routes.
app.get('/api/trips/:id/vision', (req, res) => {
    res.json(VISION
        .filter(v => v.trip_id === Number(req.params.id))
        .map(({ snapshot_path, ...frame }) => ({
            ...frame,
            snapshot_url: snapshot_path ? '/api/snapshots/' + snapshot_path : null,
        })))
})

app.get('/api/videos', (req, res) => {
    let out = CLIPS
    if (req.query.unassigned === '1') out = out.filter(c => c.trip_id === null)
    else if (req.query.trip_id) out = out.filter(c => c.trip_id === Number(req.query.trip_id))
    res.json(out.map(withVideoUrl))
})

// No real footage in dev — 502 is what the UI shows as "dashcam unreachable",
// which is the state worth being able to see while building the player.
app.get('/api/videos/:clipId/stream', (req, res) => {
    res.status(502).json({ error: 'Dashcam unreachable (mock server has no footage)' })
})

app.delete('/api/videos/unassigned', (req, res) => {
    const doomed = CLIPS.filter(c => c.trip_id === null && !c.protected)
    CLIPS = CLIPS.filter(c => !doomed.includes(c))
    res.status(202).json({ requested: doomed.length, status: 'pending' })
})

app.delete('/api/videos/:clipId', (req, res) => {
    const clip = CLIPS.find(c => c.clip_id === req.params.clipId)
    if (!clip) return res.status(404).json({ error: 'Clip not found' })
    if (clip.protected && req.query.force !== '1') {
        return res.status(409).json({ error: 'Clip is protected by a crash event. Retry with ?force=1.' })
    }
    CLIPS = CLIPS.filter(c => c !== clip)
    res.status(202).json({ requested: 1, status: 'pending' })
})

app.delete('/api/trips/:id/videos', (req, res) => {
    const tripId = Number(req.params.id)
    const clips = CLIPS.filter(c => c.trip_id === tripId)
    const blocked = clips.filter(c => c.protected).length
    if (blocked > 0 && req.query.force !== '1') {
        return res.status(409).json({
            error: `${blocked} clip(s) are protected by a crash event. Retry with ?force=1 to delete them anyway.`,
            protected_count: blocked,
        })
    }
    CLIPS = CLIPS.filter(c => c.trip_id !== tripId)
    res.status(202).json({ requested: clips.length, status: 'pending' })
})

app.post('/api/trips/:id/videos/protect', (req, res) => {
    const wanted = req.body?.protected
    if (typeof wanted !== 'boolean') {
        return res.status(400).json({ error: 'Body must be { protected: boolean }' })
    }
    CLIPS = CLIPS.map(c => c.trip_id === Number(req.params.id)
        ? { ...c, protected: wanted ? 1 : 0 } : c)
    res.status(202).json({ protected: wanted, status: 'pending' })
})

app.get('/api/dashcam/status', (req, res) => {
    const live = CLIPS.filter(c => c.state !== 'deleted')
    const sum = (f) => live.reduce((a, c) => a + (f(c) ? c.size_bytes : 0), 0)
    res.json({
        clip_count:       live.length,
        bytes_used:       sum(() => true),
        protected_count:  live.filter(c => c.protected).length,
        protected_bytes:  sum(c => c.protected),
        unassigned_count: live.filter(c => c.trip_id === null).length,
        unassigned_bytes: sum(c => c.trip_id === null),
        oldest_clip_at:   live.length ? live.map(c => c.started_at).sort()[0] : null,
        newest_clip_at:   live.length ? live.map(c => c.started_at).sort().at(-1) : null,
        jetson: {
            status: 'recording', recording: true, storage_pressure: false, stale: false,
            disk_total_bytes: 250 * 2 ** 30, disk_free_bytes: 141 * 2 ** 30,
            received_at: new Date().toISOString(),
        },
    })
})

// ---------------------------------------------------------------------------
// WebSocket server
// ---------------------------------------------------------------------------

const wss = new WebSocketServer({ server })

wss.on('connection', (ws) => {
    console.log('[mock-ws] Client connected')

    // Send trip_open so the dashboard knows a trip is active
    ws.send(JSON.stringify({
        type:    'live',
        topic:   'maverick/telemetry/trip_open',
        message: { id: 1, started_at: new Date().toISOString() },
    }))

    // Send catchup buffer so charts aren't empty on load
    ws.send(JSON.stringify({ type: 'catchup', messages: catchupBuffer }))

    ws.on('close', () => console.log('[mock-ws] Client disconnected'))
    ws.on('error', (err) => console.error('[mock-ws] Error:', err))
})

// Broadcast a new reading to all connected clients every second
setInterval(() => {
    if (wss.clients.size === 0) return
    const payload = JSON.stringify({
        type:    'live',
        topic:   'maverick/telemetry/reading',
        message: nextReading(),
    })
    wss.clients.forEach(ws => {
        if (ws.readyState === ws.OPEN) ws.send(payload)
    })
}, 1000)

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

server.listen(PORT, () => {
    console.log(`[mock] Server running on http://localhost:${PORT}`)
    console.log('[mock] WebSocket streaming live readings at 1Hz')
    console.log('[mock] Start the Vite dev server in client/ with: npm run dev')
})
