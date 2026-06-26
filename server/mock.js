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
        notes: 'Morning commute',
        avg_speed_mph: 24.3, max_speed_mph: 54.5, avg_rpm: 882.1, max_coolant_temp_f: 203.0,
        ev_time_pct: 26.3, total_regen_kwh: 0.0083, avg_fuel_economy_mpg: 38.4, min_battery_soc_pct: 70.9,
    },
    {
        id: 2, started_at: '2026-05-27T14:20:00+00:00', ended_at: '2026-05-27T14:32:00+00:00',
        duration_seconds: 720, odometer_start: 4809.1, odometer_end: 4814.8, dtc_count: 1,
        notes: 'Grocery run',
        avg_speed_mph: 14.5, max_speed_mph: 28.5, avg_rpm: 257.7, max_coolant_temp_f: 191.0,
        ev_time_pct: 69.2, total_regen_kwh: 0.0024, avg_fuel_economy_mpg: 52.1, min_battery_soc_pct: 78.5,
    },
    {
        id: 3, started_at: '2026-05-26T17:00:00+00:00', ended_at: '2026-05-26T17:35:00+00:00',
        duration_seconds: 2100, odometer_start: 4774.2, odometer_end: 4809.1, dtc_count: 0,
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
