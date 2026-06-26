# Maverick Telemetry Hub — agent conventions

> Offline-first vehicle telemetry on a Raspberry Pi 5: Python sensor/trip/DB
> processes over MQTT, an Express+WebSocket bridge, and a React+Tauri dashboard.
> This file is the self-contained source of truth — keep it updated when you
> learn something durable. See README.md for the full architecture.

## Bleeding-edge versions — verify before you code
This repo pins versions that differ from most training data. Don't assume older
APIs: **Tailwind v4** (CSS-first config, no `tailwind.config.js`), **React 19**,
**TypeScript 6**, **Vite 8**, **Tauri 2** (WebKitGTK, not Chromium). Check the
installed version before using a pattern you "remember."

## Hard architectural invariants (do not violate)
- **Process isolation via MQTT.** `obd_poller.py`, `trip_manager.py`, and
  `db_writer.py` each have one responsibility and communicate *only* over MQTT
  topics (`maverick/telemetry/*`). Don't add direct calls between them.
- **Single-writer DB.** `db_writer.py` is the ONLY process that writes SQLite;
  the Express bridge (`server/`) is the ONLY process that reads it. Never add a
  second writer or let a Python poller touch the DB directly.
- **Power-loss resilience is load-bearing.** Trip data is committed
  reading-by-reading; `db_writer` recovers unclosed trips on boot. Don't batch
  writes in a way that loses data on an ungraceful power cut.
- **Offline-first.** Core telemetry must run with zero network. AI/DTC features
  degrade gracefully — never make logging depend on connectivity or the Claude API.

## Layout
- `*.py` (root) + `db/` — Python edge processes and SQLite migrations.
- `server/` — Node/Express bridge (npm, better-sqlite3, ws). REST + WebSocket.
- `client/` — React + Vite + Tailwind v4 + shadcn/radix dashboard (npm).
- `client/src-tauri/` — Tauri 2 native shell for the in-cab display.
- `deploy/` — systemd units; boot order is mosquitto → db_writer → trip_manager
  → obd_poller, and express_bridge → kiosk.

## Naming (client/)
- **PascalCase** — components & component files (`TripDetail.tsx`).
- **camelCase** — hooks and utils (`useLiveReadings.ts`, `cn.ts`).
- Match the export to the filename.

## Working norms
- **Surgical edits only — no `sed`/bulk auto-replace. No mocking — implement fully.**
- Track multi-step work in **phases**; commit at phase boundaries.
- **Never `git commit` without explicit user confirmation and a user-supplied
  message.**
- **Verify every change** before calling it done:
  - `client/`: `npm run build` (runs `tsc -b`) and `npm run lint`.
  - `server/`: start it and hit `/api/health`.
  - Python: run the affected process and confirm it publishes/consumes MQTT.
- **Don't trust truncated tool output** — read full lint/build output, not just
  the summary count.
- **Lint suppressions** must always say why:
  `// eslint-disable-next-line <rule> -- <reason this is correct here>`. Never
  bare, never file-wide.
