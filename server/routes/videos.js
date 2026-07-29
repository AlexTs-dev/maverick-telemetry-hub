/**
 * routes/videos.js
 * Maverick Telemetry Hub — dashcam footage
 *
 * Footage lives on the JETSON, not here — it has the disk, and a month of
 * 1080p is far more than the Pi should carry. SQLite holds only metadata and
 * the trip linkage; the bytes are streamed from clip_server.py on the Jetson
 * and proxied through this process so the browser sees a single origin (which
 * also keeps the kiosk's CSP simple).
 *
 * DELETES GO OUT OVER MQTT, they are not applied here. db_writer.py is the only
 * process that writes SQLite (CLAUDE.md), and the Jetson is the only process
 * that deletes footage. So a delete publishes a command, db_writer marks the
 * row pending_delete, the Jetson removes the file and confirms, and only then
 * does the row disappear. That is why these endpoints answer 202, not 200.
 */

const express = require('express');
const http    = require('http');
const https   = require('https');
const { URL } = require('url');
const router  = express.Router();
const db      = require('../db');
const { publish } = require('../mqtt');

const JETSON_CLIP_URL = process.env.MAVERICK_JETSON_CLIP_URL
    || 'http://192.168.100.2:8088';

// The Jetson is one ethernet hop away; if it has not answered in this long it
// is not going to. Keeps a dead Jetson from hanging the dashboard.
const PROXY_TIMEOUT_MS = 10000;

// 20260729T143005Z_a1b2c3d4
const CLIP_ID_RE = /^\d{8}T\d{6}Z_[0-9a-f]{8}$/;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let requestCounter = 0;
function nextRequestId(prefix) {
    requestCounter += 1;
    return `${prefix}-${Date.now()}-${requestCounter}`;
}

/**
 * clip_path is a filesystem fact on the Jetson; the URL is a routing fact —
 * derive it here so the DB never hardcodes routes, and so the client never
 * learns the Jetson's address. Same discipline as snapshot_url in trips.js.
 */
function withVideoUrl(clip) {
    return { ...clip, video_url: `/api/videos/${clip.clip_id}/stream` };
}

/**
 * Ask the Jetson to delete these clips. Returns the number of clips addressed.
 * db_writer sees the same message and flips the rows to pending_delete.
 */
function requestDelete(clipIds, extra = {}) {
    const valid = clipIds.filter((id) => CLIP_ID_RE.test(id));
    if (valid.length === 0) return 0;
    publish('maverick/dashcam/command', {
        request_id: nextRequestId('delete'),
        action:     'delete',
        clip_ids:   valid,
        ...extra,
    });
    return valid.length;
}

// ---------------------------------------------------------------------------
// GET /api/videos
// All clips, newest first. ?trip_id=N or ?unassigned=1 to filter.
// ---------------------------------------------------------------------------
router.get('/', (req, res) => {
    try {
        let where = '';
        const params = [];
        if (req.query.unassigned === '1') {
            where = 'WHERE trip_id IS NULL';
        } else if (req.query.trip_id) {
            where = 'WHERE trip_id = ?';
            params.push(req.query.trip_id);
        }

        const clips = db.prepare(`
            SELECT id, clip_id, trip_id, started_at, ended_at, duration_s,
                   size_bytes, width_px, height_px, fps, protected, state
            FROM dashcam_clips
            ${where}
            ORDER BY started_at DESC
        `).all(...params);

        res.json(clips.map(withVideoUrl));
    } catch (error) {
        console.error('GET /videos error:', error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// DELETE /api/videos/unassigned
// Footage that matched no trip. Without this the dashboard has no way to reach
// it — the trip detail page is organised by trip, and these belong to none.
//
// Declared BEFORE /:clipId so "unassigned" is not parsed as a clip id.
// ---------------------------------------------------------------------------
router.delete('/unassigned', (req, res) => {
    try {
        const clips = db.prepare(
            `SELECT clip_id FROM dashcam_clips WHERE trip_id IS NULL AND protected = 0`
        ).all();

        const requested = requestDelete(clips.map((c) => c.clip_id));
        res.status(202).json({ requested, status: 'pending' });
    } catch (error) {
        console.error('DELETE /videos/unassigned error:', error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// GET /api/videos/:clipId/stream
// Range-forwarding proxy to the Jetson.
//
// Range support is the whole point: without it a <video> element can only play
// a clip from the start, never seek. Hand-rolled rather than pulling in a proxy
// dependency — it is ~40 lines and the header handling needs to be exact.
// ---------------------------------------------------------------------------
router.get('/:clipId/stream', (req, res) => {
    const { clipId } = req.params;
    if (!CLIP_ID_RE.test(clipId)) {
        return res.status(400).json({ error: 'Malformed clip id' });
    }

    let clip;
    try {
        clip = db.prepare(
            'SELECT clip_path, state FROM dashcam_clips WHERE clip_id = ?'
        ).get(clipId);
    } catch (error) {
        console.error(`GET /videos/${clipId}/stream error:`, error.message);
        return res.status(500).json({ error: error.message });
    }

    if (!clip) return res.status(404).json({ error: 'Clip not found' });
    if (clip.state === 'deleted') {
        return res.status(410).json({ error: 'Clip has been deleted' });
    }

    let target;
    try {
        target = new URL(`/clips/${clip.clip_path}`, JETSON_CLIP_URL);
    } catch {
        return res.status(500).json({ error: 'Bad MAVERICK_JETSON_CLIP_URL' });
    }

    const transport = target.protocol === 'https:' ? https : http;
    const headers = {};
    // Forwarding Range is what makes seeking work; the Jetson answers 206.
    if (req.headers.range) headers.Range = req.headers.range;

    const upstream = transport.request(
        target,
        { method: 'GET', headers, timeout: PROXY_TIMEOUT_MS },
        (jetsonRes) => {
            res.status(jetsonRes.statusCode || 502);
            for (const header of ['content-type', 'content-length',
                                  'content-range', 'accept-ranges', 'cache-control']) {
                if (jetsonRes.headers[header]) {
                    res.setHeader(header, jetsonRes.headers[header]);
                }
            }
            jetsonRes.pipe(res);
        },
    );

    upstream.on('timeout', () => upstream.destroy(new Error('Jetson timed out')));

    upstream.on('error', (err) => {
        console.error(`GET /videos/${clipId}/stream upstream error:`, err.message);
        if (!res.headersSent) {
            // 502 rather than 500: the dashboard shows "dashcam unreachable"
            // instead of implying the footage is gone.
            res.status(502).json({ error: 'Dashcam unreachable' });
        } else {
            res.destroy();
        }
    });

    // The browser abandons in-flight ranges constantly while scrubbing.
    res.on('close', () => upstream.destroy());

    upstream.end();
});

// ---------------------------------------------------------------------------
// DELETE /api/videos/:clipId
// ---------------------------------------------------------------------------
router.delete('/:clipId', (req, res) => {
    const { clipId } = req.params;
    if (!CLIP_ID_RE.test(clipId)) {
        return res.status(400).json({ error: 'Malformed clip id' });
    }

    try {
        const clip = db.prepare(
            'SELECT clip_id, protected FROM dashcam_clips WHERE clip_id = ?'
        ).get(clipId);
        if (!clip) return res.status(404).json({ error: 'Clip not found' });

        // Protected footage is what a crash left behind — deleting it needs to
        // be deliberate, so it takes an explicit ?force=1 rather than riding
        // along with a bulk delete.
        if (clip.protected && req.query.force !== '1') {
            return res.status(409).json({
                error: 'Clip is protected by a crash event. Retry with ?force=1 '
                     + 'or unprotect the trip first.',
            });
        }

        requestDelete([clipId]);
        res.status(202).json({ requested: 1, status: 'pending' });
    } catch (error) {
        console.error(`DELETE /videos/${clipId} error:`, error.message);
        res.status(500).json({ error: error.message });
    }
});

// ---------------------------------------------------------------------------
// GET /api/videos/status  (mounted at /api/dashcam/status too — see index.js)
// ---------------------------------------------------------------------------
function dashcamStatus(getDashcamStatus) {
    return (req, res) => {
        try {
            const row = db.prepare(`
                SELECT COUNT(*)                                    AS clip_count,
                       COALESCE(SUM(size_bytes), 0)                AS bytes_used,
                       COALESCE(SUM(protected), 0)                 AS protected_count,
                       COALESCE(SUM(CASE WHEN protected = 1 THEN size_bytes ELSE 0 END), 0)
                                                                   AS protected_bytes,
                       COALESCE(SUM(CASE WHEN trip_id IS NULL THEN 1 ELSE 0 END), 0)
                                                                   AS unassigned_count,
                       COALESCE(SUM(CASE WHEN trip_id IS NULL THEN size_bytes ELSE 0 END), 0)
                                                                   AS unassigned_bytes,
                       MIN(started_at)                             AS oldest_clip_at,
                       MAX(started_at)                             AS newest_clip_at
                FROM dashcam_clips
                WHERE state != 'deleted'
            `).get();

            // Disk figures come from the Jetson's heartbeat — the Pi cannot see
            // the Jetson's filesystem, and would report its own if it tried.
            res.json({ ...row, jetson: getDashcamStatus() });
        } catch (error) {
            console.error('GET /dashcam/status error:', error.message);
            res.status(500).json({ error: error.message });
        }
    };
}

module.exports = { router, dashcamStatus, requestDelete, nextRequestId, CLIP_ID_RE };
