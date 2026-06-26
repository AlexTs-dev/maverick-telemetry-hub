/**
 * index.js
 * Maverick Telemetry Hub — Express Bridge
 *
 * Entry point. Wires together:
 *   - Express HTTP server (REST API + static React build)
 *   - WebSocket server (live telemetry stream)
 *   - MQTT subscriber (Mosquitto broker)
 *   - SQLite (via route handlers)
 *
 * Start with: node index.js
 * Or via systemd: see deploy/express_bridge.service
 */

require('dotenv').config();

const express                        = require('express');
const { createServer }               = require('http');
const path                           = require('path');
const { mqttClient, onMessage, getRecentMessages } = require('./mqtt');
const { createWebSocketServer }      = require('./websocket');
const tripsRouter                    = require('./routes/trips');
const dtcsRouter                     = require('./routes/dtcs');
const versionRouter                  = require('./routes/version');
const { startPolling }               = require('./version');

const PORT = process.env.PORT || 3000;

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------
const app = express();
app.use(express.json());

// ---------------------------------------------------------------------------
// API routes
// ---------------------------------------------------------------------------
app.use('/api/trips',   tripsRouter);
app.use('/api/dtcs',    dtcsRouter);
app.use('/api/version', versionRouter);

// Begin polling GitHub for the latest release in the background
startPolling();

// Health check — used by systemd watchdog and for debugging
app.get('/api/health', (req, res) => {
    res.json({
        status:    'ok',
        timestamp: new Date().toISOString(),
        mqtt:      mqttClient.connected ? 'connected' : 'disconnected',
    });
});

// ---------------------------------------------------------------------------
// Serve React build
// The dashboard is built with `npm run build` in the client/ directory.
// Express serves the static output here so any device on the local
// WiFi network can access it at http://<pi-ip>:3000
// ---------------------------------------------------------------------------
const CLIENT_BUILD = path.join(__dirname, '../client/dist');
app.use(express.static(CLIENT_BUILD));

// Catch-all — return index.html for any non-API route so React
// Router can handle client-side navigation
app.get('/{*splat}', (req, res) => {
    res.sendFile(path.join(CLIENT_BUILD, 'index.html'));
});

// ---------------------------------------------------------------------------
// HTTP + WebSocket server
// Both run on the same port — WebSocket upgrades are handled
// automatically by the ws library via the shared http.Server instance
// ---------------------------------------------------------------------------
const server = createServer(app);
const { broadcast } = createWebSocketServer(server, getRecentMessages);

// ---------------------------------------------------------------------------
// Wire MQTT → WebSocket broadcast
// Every MQTT message received is forwarded to all connected
// WebSocket clients in real time
// ---------------------------------------------------------------------------
onMessage((entry) => {
    broadcast({
        type:    'live',
        ...entry,
    });
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
server.listen(PORT, () => {
    console.log(`[server] Listening on port ${PORT}`);
    console.log(`[server] Dashboard: http://localhost:${PORT}`);
    console.log(`[server] API:       http://localhost:${PORT}/api`);
    console.log(`[server] Health:    http://localhost:${PORT}/api/health`);
});

// ---------------------------------------------------------------------------
// Graceful shutdown
// Allows systemd to stop the process cleanly without killing
// in-flight requests or leaving MQTT in a bad state
// ---------------------------------------------------------------------------
function shutdown(signal) {
    console.log(`[server] ${signal} received — shutting down`);
    server.close(() => {
        console.log('[server] HTTP server closed');
        mqttClient.end(false, () => {
            console.log('[mqtt] Client disconnected');
            process.exit(0);
        });
    });

    // Force exit after 10s if graceful shutdown stalls
    setTimeout(() => {
        console.error('[server] Shutdown timed out — forcing exit');
        process.exit(1);
    }, 10000);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT',  () => shutdown('SIGINT'));