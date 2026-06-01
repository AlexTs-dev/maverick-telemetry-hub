# Maverick Telemetry Hub

> An offline-first, AI-enhanced vehicle telemetry system built for a 2026 Ford Maverick Hybrid — running on a Raspberry Pi 5, mounted in the cab.

![Status](https://img.shields.io/badge/status-in%20progress-yellow)
![Stack](https://img.shields.io/badge/stack-Python%20%7C%20MQTT%20%7C%20Node.js%20%7C%20React-blue)
![Hardware](https://img.shields.io/badge/hardware-Raspberry%20Pi%205%20%7C%20OBDLink%20EX-teal)
![License](https://img.shields.io/badge/license-MIT-green)

---

## In the car

![Mounted display showing live telemetry](docs/PXL_20260531_2334220612.jpg)

*5" touchscreen mounted in the cab — live speed and RPM traces, coolant temp, throttle, and fuel rate.*

---

## Dashboard

![Trip list](docs/Screenshot%202026-05-31%20210531.png)

*Trip history with MPG, average speed, and DTC badge for any trip with fault codes.*

![Trip detail](docs/Screenshot%202026-05-31%20210608.png)

*Per-trip detail: summary stats, AI-interpreted fault code (P0D0B diagnosed by Claude), and trip notes.*

---

## What this is

A full-stack edge telemetry system that reads live OBD-II data from a 2026 Ford Maverick Hybrid, processes it locally on a Raspberry Pi 5, and persists every trip to a local SQLite database — with no cloud dependency.

After each drive, a React dashboard served over local WiFi provides post-trip analysis: speed and RPM traces, fuel economy stats, and trip history. An AI layer interprets any OBD-II fault codes (DTCs) in plain English using the Claude API. Results are cached in SQLite — the API is called once per code, never again.

The system powers on automatically with the ignition and requires no interaction to begin logging.

---

## Architecture

```
2026 Maverick Hybrid (OBD-II)
        |
   OBDLink EX (USB)
        |
   Raspberry Pi 5
   ├── obd_poller.py     polls sensors at 1Hz, publishes to MQTT
   ├── trip_manager.py   detects ignition on/off, manages trip lifecycle
   ├── db_writer.py      subscribes to MQTT, writes all data to SQLite
   └── server/
       └── index.js      Express + WebSocket bridge, serves React dashboard
              |
        React Dashboard (client/)
        ├── Live view     real-time gauges + rolling D3 charts
        ├── Trip list     history with summary stats
        └── Trip detail   per-trip traces, stats, DTCs, notes
              |
        Claude API        DTC fault code interpretation
```

### Process isolation

Each Python process has a single responsibility and communicates only via MQTT. The Express bridge is the only process that reads SQLite. `db_writer.py` is the only process that writes to it. If any process crashes, systemd restarts it independently without affecting the others.

### Power-loss recovery

When the engine cuts power to the Pi mid-trip, processes die without a clean shutdown — `trip_manager` never publishes `trip_close`, so the trip stays open in the database with no summary. On next boot, `db_writer` automatically recovers any unclosed trips: it sets `ended_at` to the last committed reading's timestamp, computes duration, and generates the trip summary from whatever readings were saved. No manual intervention needed.

---

## Features

- **Automatic trip detection** — opens a trip on ignition on (RPM > 10), closes after 5 minutes of zero RPM or OBD disconnect. Accounts for Maverick Hybrid EV stops at red lights.
- **1Hz sensor logging** — RPM, speed, coolant temp, throttle position, and fuel rate written to SQLite every second.
- **Post-trip dashboard** — React UI served over local WiFi. Trip history, speed and RPM traces, MPG, and per-trip notes.
- **AI fault code interpreter** — DTCs sent to Claude for plain-English diagnosis with urgency assessment. Results cached in SQLite.
- **Live kiosk view** — real-time gauges and 5-minute rolling charts via WebSocket. Designed for glanceable display while driving.
- **Offline-first** — core telemetry runs with zero network dependency. AI features degrade gracefully without connectivity.
- **Power-loss resilient** — trip data is committed reading-by-reading; unclosed trips are recovered automatically on reboot.

---

## Hardware

| Component | Details |
|---|---|
| Edge computer | Raspberry Pi 5 (4GB) |
| Storage (v1) | Samsung Pro Endurance 64GB SD card |
| OBD-II adapter | OBDLink EX (USB) |
| Display | Hosyond 5" IPS Capacitive Touchscreen, 800×480, MIPI DSI |

### Planned v2 hardware
- 12V buck converter → USB-C, switched with ignition (hardwired power)
- Raspberry Pi M.2 HAT+
- WD SN740 M.2 2230 NVMe 256GB
- Argon NEO 5 M.2 aluminum enclosure (passive cooling)
- Mounted in Ford Maverick FITS panel

---

## Tech stack

| Layer | Technology |
|---|---|
| Sensor polling | Python, python-obd |
| Message broker | MQTT (Mosquitto) |
| Trip management | Python state machine |
| Database | SQLite (WAL mode, versioned migrations) |
| Backend / bridge | Node.js, Express, WebSockets, better-sqlite3 |
| Frontend | React, TypeScript, Vite, Tailwind CSS |
| Charts | D3 |
| AI integration | Claude API (claude-sonnet-4-6) |

---

## Repository structure

```
maverick-telemetry-hub/
├── db/
│   ├── migrate.py              SQLite schema + versioned migrations
│   └── seed.sql                Development seed data
├── obd_poller.py               OBD-II sensor polling process
├── trip_manager.py             Trip lifecycle state machine
├── db_writer.py                MQTT subscriber → SQLite writer (with boot recovery)
├── server/
│   ├── index.js                Express entry point
│   ├── mqtt.js                 MQTT client and subscription
│   ├── websocket.js            WebSocket server and broadcast
│   ├── db.js                   SQLite connection
│   └── routes/
│       ├── trips.js            Trip list, detail, readings endpoints
│       └── dtcs.js             Fault code endpoints + Claude diagnosis
├── client/                     React dashboard (Vite + Tailwind)
├── docs/                       Screenshots and photos
├── deploy/
│   ├── obd_poller.service      systemd unit
│   ├── trip_manager.service    systemd unit
│   ├── db_writer.service       systemd unit
│   ├── express_bridge.service  systemd unit
│   ├── kiosk.service           systemd unit (Chromium kiosk mode)
│   └── README.md               Deployment instructions
└── README.md
```

---

## API reference

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/trips` | All trips, most recent first, with summary stats |
| GET | `/api/trips/:id` | Single trip with summary |
| GET | `/api/trips/:id/readings` | All sensor readings for a trip |
| GET | `/api/trips/:id/dtcs` | Fault codes for a trip |
| GET | `/api/dtcs` | All fault codes across all trips |
| POST | `/api/dtcs/:id/diagnose` | Fetch Claude diagnosis for a DTC (cached) |
| GET | `/api/health` | Server health + MQTT connection status |

---

## Database schema

Four tables. `trip_summaries` is computed once on trip close (or on boot recovery) — never recalculated at query time.

```
trips           one row per ignition cycle
readings        raw 1Hz sensor stream, foreign key → trips
dtcs            fault code events, foreign key → trips
trip_summaries  aggregated stats, 1:1 with trips
```

Migration versions:
- **v1** — base schema (trips, readings, dtcs, trip_summaries)
- **v2** — adds `pack_voltage_v`, `battery_current_a`, `motor_speed_rpm` to readings (Ford Mode 22 PIDs, not currently polled)

---

## MQTT topic map

| Topic | Publisher | Description |
|---|---|---|
| `maverick/telemetry/reading` | obd_poller | Raw sensor reading, 1Hz |
| `maverick/telemetry/poller_status` | obd_poller | OBD connection state |
| `maverick/telemetry/trip_open` | trip_manager | Trip started |
| `maverick/telemetry/trip_close` | trip_manager | Trip ended |
| `maverick/telemetry/dtc` | obd_poller | Fault code detected |

---

## Boot order

Services start in this order via systemd dependencies:

```
mosquitto → db_writer → trip_manager → obd_poller
                     → express_bridge → kiosk
```

Each service restarts automatically after 5 seconds on failure.

---

## Project status

- [x] SQLite schema and migration script
- [x] `obd_poller.py` — sensor polling with reconnect backoff
- [x] `trip_manager.py` — ignition detection state machine
- [x] `db_writer.py` — MQTT → SQLite with retry logic and boot recovery
- [x] systemd service files for all processes
- [x] Express bridge — REST API + WebSocket server
- [x] React dashboard — trip list, trip detail, sensor charts
- [x] Kiosk mode — Chromium fullscreen on MIPI DSI display
- [x] Claude API DTC interpreter — plain-English fault code diagnosis
- [x] Live WebSocket view — real-time gauges and rolling charts
- [ ] Ford hybrid PID research — Mode 22 PIDs for battery SOC, EV mode, regen on Maverick FHEV
- [ ] Fusion 360 enclosure design (v2)
- [ ] M.2 HAT+ storage migration (v2)
- [ ] FITS panel install (v2)

---

## Setup

See [deploy/README.md](deploy/README.md) for full installation instructions.

**Quick start:**
```bash
# 1. Install system dependencies
sudo apt update && sudo apt install -y mosquitto mosquitto-clients
sudo usermod -a -G dialout pi  # USB access for OBDLink EX

# 2. Python environment
python3 -m venv venv && source venv/bin/activate
pip install obd paho-mqtt

# 3. Initialize database
python db/migrate.py

# 4. Node environment
cd server && npm install && cd ..

# 5. Build React client
cd client && npm install && npm run build && cd ..

# 6. Create environment file
echo "ANTHROPIC_API_KEY=your_key_here" > server/.env

# 7. Install and start services
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now db_writer trip_manager obd_poller express_bridge kiosk
```

Dashboard is available at `http://<pi-ip>:3000` from any device on the same WiFi network.

---

## Why I built this

This project directly extends work I did professionally — building real-time telemetry pipelines and offline-first edge systems for Ford Motor Company field environments. I wanted to apply that same architecture to a personal vehicle, using hardware I own, producing something genuinely useful rather than a contrived demo.

The 2026 Maverick Hybrid presented an interesting challenge: standard OBD-II PIDs cover engine vitals, but hybrid-specific data (battery SOC, EV mode, regen) lives behind Ford proprietary Mode 22 PIDs that required independent research to surface. That gap between the standard spec and what's actually accessible on the CAN bus is the same problem I worked on professionally.

---

## License

MIT
