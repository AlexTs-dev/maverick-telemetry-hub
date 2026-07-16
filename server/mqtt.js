/**
 * mqtt.js
 * Maverick Telemetry Hub
 *
 * Handles MQTT broker connection and subscription only.
 * Does not create an HTTP server — that lives in index.js.
 *
 * Exports:
 *   mqttClient       — the connected mqtt client instance
 *   onMessage        — register a callback for incoming messages
 *   getRecentMessages — ring buffer for WebSocket catch-up
 *   getVisionStatus  — Jetson liveness for /api/health
 */

const mqtt = require('mqtt');

const MQTT_URL   = process.env.MQTT_URL  || 'mqtt://localhost:1883';
// Topics forwarded to the ring buffer + every WebSocket client.
// maverick/vision/status and /scene are subscribed individually and
// DELIBERATELY exclude maverick/vision/frame — frame payloads carry
// ~130 KB of base64 JPEG each. Never "simplify" this to maverick/# or
// maverick/vision/#: everything subscribed here is rebroadcast to all
// WS clients and stored 500-deep in the catch-up buffer.
const MQTT_TOPICS = [
    'maverick/telemetry/#',
    'maverick/vision/status',
    'maverick/vision/scene',
];

// ---------------------------------------------------------------------------
// Last known Jetson vision status — powers the `vision` field in /api/health.
// The Jetson heartbeats maverick/vision/status every 5s. Its LWT also flips
// status to 'disconnected', but the broker only fires an LWT after the MQTT
// keepalive times out (~90s for keepalive 60) — so the staleness check in
// getVisionStatus() is the fast path for a yanked ethernet cable.
// ---------------------------------------------------------------------------
const VISION_STALE_MS  = 15000;  // 3 missed heartbeats
let   lastVisionStatus = null;   // { status: string, receivedAt: number } | null



// ---------------------------------------------------------------------------
// In-memory store of recent messages
// Lets new WebSocket clients catch up on the last known state
// without querying SQLite.
// ---------------------------------------------------------------------------
const MAX_MESSAGES   = 500;
const recentMessages = [];

// Registered message callbacks — populated by index.js
const messageHandlers = [];

// ---------------------------------------------------------------------------
// MQTT client
// ---------------------------------------------------------------------------
const mqttClient = mqtt.connect(MQTT_URL, {
    clientId:     'express_bridge',
    reconnectPeriod: 2000,   // retry every 2s on disconnect
});

mqttClient.on('connect', () => {
    console.log(`[mqtt] Connected to broker at ${MQTT_URL}`);
    mqttClient.subscribe(MQTT_TOPICS, (err) => {
        if (err) console.error('[mqtt] Subscribe error:', err);
        else     console.log(`[mqtt] Subscribed to ${MQTT_TOPICS.join(', ')}`);
    });
});

mqttClient.on('message', (topic, payload) => {
    let parsed;
    try {
        parsed = JSON.parse(payload.toString());
    } catch {
        parsed = payload.toString();
    }

    if (topic === 'maverick/vision/status') {
        lastVisionStatus = {
            status:     typeof parsed === 'object' && parsed !== null && typeof parsed.status === 'string'
                            ? parsed.status
                            : 'unknown',
            receivedAt: Date.now(),
        };
    }

    const entry = {
        topic,
        message:    parsed,
        receivedAt: new Date().toISOString(),
    };

    // Store in ring buffer
    recentMessages.push(entry);
    if (recentMessages.length > MAX_MESSAGES) recentMessages.shift();

    // Notify all registered handlers
    messageHandlers.forEach(fn => fn(entry));
});

mqttClient.on('error', (err) => {
    console.error('[mqtt] Client error:', err);
});

mqttClient.on('disconnect', () => {
    console.warn('[mqtt] Disconnected from broker — reconnecting...');
});

// ---------------------------------------------------------------------------
// Register a callback for incoming MQTT messages
// Called by index.js to wire MQTT → WebSocket broadcast
// ---------------------------------------------------------------------------
function onMessage(fn) {
    messageHandlers.push(fn);
}

// ---------------------------------------------------------------------------
// Get recent messages for new WebSocket clients on connect
// ---------------------------------------------------------------------------
function getRecentMessages(limit = 50) {
    return recentMessages.slice(-limit);
}

// ---------------------------------------------------------------------------
// Current vision liveness for /api/health.
//   'unknown'      — no status message since the bridge started
//   'disconnected' — last heartbeat is stale (or the LWT/publisher said so)
//   otherwise      — whatever the Jetson last reported (connected/connecting)
// ---------------------------------------------------------------------------
function getVisionStatus() {
    if (!lastVisionStatus) return 'unknown';
    if (Date.now() - lastVisionStatus.receivedAt > VISION_STALE_MS) return 'disconnected';
    return lastVisionStatus.status;
}

module.exports = { mqttClient, onMessage, getRecentMessages, getVisionStatus };