# Deployment — Maverick Telemetry Hub

Installation, kiosk, display, and over-the-air update setup for running the
hub on a Raspberry Pi 5. For the project overview, architecture, and API
reference, see the [main README](../README.md).

Everything below assumes the repo is checked out at
`/home/pi/maverick-telemetry-hub` and the service user is `pi`. The systemd
units hardcode these paths — adjust them if your layout differs.

---

## Contents of `deploy/`

| File | Purpose |
| --- | --- |
| `db_writer.service` | systemd unit — MQTT → SQLite writer |
| `trip_manager.service` | systemd unit — trip lifecycle state machine |
| `obd_poller.service` | systemd unit — OBD-II sensor poller |
| `express_bridge.service` | systemd unit — Express REST + WebSocket bridge |
| `kiosk.service` | systemd unit — launches the Tauri dashboard fullscreen |
| `kiosk-start.sh` | Detects the Wayland socket, disables the WebKit DMABUF renderer, launches the Tauri binary |
| `kanshi.config` | Version-controlled DSI panel rotation/mode profile |
| `pull-deploy.sh` | Polls GitHub Releases and applies new builds |
| `pull-deploy.service` | systemd oneshot that runs `pull-deploy.sh` |
| `pull-deploy.timer` | Triggers the oneshot 30s after boot, then every 2 minutes |

---

## Prerequisites

- Raspberry Pi 5 running Raspberry Pi OS (Bookworm, 64-bit) with the **labwc**
  Wayland session (the default in-cab display stack; `kanshi` manages outputs).
- Node.js (system `node` at `/usr/bin/node`) and `npm`.
- Python 3 with `venv`.
- An OBDLink EX on USB. `lsusb` should show it as `0403:6015` (FTDI),
  typically `/dev/ttyUSB0`.
- A Rust toolchain **if you build the Tauri binary on the Pi** (not required if
  you deploy prebuilt release binaries via the OTA flow below).

---

## 1. System dependencies

```bash
sudo apt update && sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```

### USB access for the OBDLink EX

The poller runs as `pi`, so the user needs non-root serial access:

```bash
sudo usermod -a -G dialout pi   # log out/in (or reboot) for it to take effect
```

The `obd_poller.service` unit expects the adapter at `/dev/ttyUSB0` and
`115200` baud (`OBD_PORT` / `OBD_BAUDRATE`). If your adapter enumerates on a
different port, add a `udev` rule or update the unit's environment.

---

## 2. Python environment

```bash
cd /home/pi/maverick-telemetry-hub
python3 -m venv venv && source venv/bin/activate
pip install obd paho-mqtt
```

---

## 3. Database

The live database lives at `/home/pi/maverick_telemetry.db`. This path is set
via `MAVERICK_DB_PATH` in the `db_writer`, `trip_manager`, and
`express_bridge` units — **it must match across all of them and the migration
command**, or services will read a different database than the one being
migrated.

```bash
MAVERICK_DB_PATH=/home/pi/maverick_telemetry.db python db/migrate.py
```

`migrate.py` is idempotent and only applies pending schema versions, so it is
safe to re-run on every deploy.

---

## 4. Express bridge

```bash
cd /home/pi/maverick-telemetry-hub/server
npm install --omit=dev
```

Create `server/.env` with the API key and (optionally) the port:

```bash
ANTHROPIC_API_KEY=your_key_here
PORT=3000
```

`express_bridge.service` loads this file and also sets `NODE_ENV=production`
and `MAVERICK_DB_PATH`. The DTC interpreter is the only feature that needs the
key; the bridge runs fine without it (diagnosis requests just fail gracefully).

The unit's `ExecStartPre` asserts that `client/dist/index.html` exists before
starting, so build the client (next step) first.

---

## 5. React client

```bash
cd /home/pi/maverick-telemetry-hub/client
npm install
npm run build        # outputs client/dist, served by the Express bridge
```

The bridge serves this build at `http://<pi-ip>:3000`. Rebuild after any
frontend change (or let the OTA flow ship a prebuilt `client-dist.tar.gz`).

---

## 6. Tauri in-cab display

The in-cab screen runs the dashboard as a native Tauri app (WebKitGTK), not a
browser. Build the release binary on the Pi:

```bash
cd /home/pi/maverick-telemetry-hub/client
npm run tauri build
# → client/src-tauri/target/release/maverick-telemetry
```

`kiosk.service` launches it through `kiosk-start.sh`, which:

- auto-detects the Wayland socket (`wayland-0` / `wayland-1`) under
  `/run/user/1000`, falling back to `DISPLAY=:0`;
- exports `WEBKIT_DISABLE_DMABUF_RENDERER=1` — the DMABUF renderer corrupts the
  display on the Pi's V3D GPU (colored-pixel artifacts over black); disabling it
  forces a stable path while keeping accelerated compositing on;
- waits (via the unit's `ExecStartPre`) for `GET /api/health` to return before
  opening the window, retrying for up to 30 seconds.

### Display rotation

The DSI panel's orientation and mode are version-controlled in
`deploy/kanshi.config`:

```
profile {
	output DSI-2 enable scale 1.000000 mode 800x480@60.029 position 0,0 transform 180
}
```

`transform` accepts `normal | 90 | 180 (upside-down) | 270`. `pull-deploy.sh`
installs this file to `~/.config/kanshi/config` (and `config.init`) on every
deploy and applies it live via `wlr-randr` when a Wayland session is up; kanshi
reapplies it on every boot. **Do not hand-edit `~/.config/kanshi/config` on the
Pi — the next deploy overwrites it.** Change rotation by editing
`deploy/kanshi.config` and deploying.

---

## 7. Install and start the services

```bash
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now db_writer trip_manager obd_poller express_bridge kiosk
```

### Boot order

systemd dependencies enforce this order:

```
mosquitto → db_writer → trip_manager → obd_poller
                     → express_bridge → kiosk
```

`db_writer` comes up first so trip-open events are never missed; `obd_poller`
starts last so it only publishes once consumers are ready. The bridge and
kiosk start in parallel with the pollers. Every long-running unit restarts
automatically (`Restart=always`/`on-failure`, `RestartSec=5`).

Tail logs with:

```bash
journalctl -u db_writer -f       # or trip_manager / obd_poller / express_bridge / kiosk
```

---

## 8. Over-the-air updates (pull-deploy)

The Pi keeps itself current by polling GitHub Releases — no inbound SSH and no
self-hosted runner; the only requirement is outbound HTTPS to GitHub.

`pull-deploy.sh` (run by `pull-deploy.timer` 30s after boot, then every 2
minutes) compares the latest release tag against `~/.maverick-deployed-tag`,
and on a new tag it:

1. downloads the `maverick-telemetry` binary and `client-dist.tar.gz` assets;
2. refreshes the Python/server/deploy files from `origin/main` via `git`;
3. installs the Tauri binary and unpacks the React build;
4. reinstalls the managed `kanshi.config`;
5. runs `db/migrate.py` against the live database;
6. `npm install --omit=dev` in `server/`;
7. `sudo systemctl restart express_bridge kiosk`;
8. records the new tag.

Install the timer:

```bash
sudo cp deploy/pull-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pull-deploy.timer
```

### Passwordless restart (required)

Step 7 runs `sudo systemctl restart …` unattended (also when triggered from the
dashboard's `POST /api/version/update`). Grant the `pi` user passwordless rights
for just those restarts:

```bash
# /etc/sudoers.d/maverick  (edit with: sudo visudo -f /etc/sudoers.d/maverick)
pi ALL=(root) NOPASSWD: /usr/bin/systemctl restart express_bridge, /usr/bin/systemctl restart kiosk
```

### Optional: GitHub token for higher rate limits

Unauthenticated GitHub API access is capped at 60 requests/hour. To raise it to
5000/hour, provide a token:

```bash
echo 'GITHUB_TOKEN=ghp_xxx' > /home/pi/.maverick-env
```

Then uncomment the `EnvironmentFile=/home/pi/.maverick-env` line in
`pull-deploy.service` and `daemon-reload`.

> Note: `pull-deploy.service` is a `oneshot` with `TimeoutStartSec=600` so a
> slow first `npm install` / download can't hang and freeze the timer. If you
> edit the unit, re-`cp` it and `daemon-reload`.

#### Publishing a release

The OTA flow expects each GitHub Release to carry two assets:

- `maverick-telemetry` — the Tauri release binary (built for the Pi's
  `aarch64` target).
- `client-dist.tar.gz` — a gzip tarball of `client/dist`.

The deployed tag is tracked in `~/.maverick-deployed-tag`; delete it to force a
redeploy of the current release.

---

## Verifying a deployment

```bash
curl -s http://localhost:3000/api/health      # { status: "ok", mqtt: "connected" }
curl -s http://localhost:3000/api/version     # { current, latest, updateAvailable }
systemctl --no-pager status db_writer trip_manager obd_poller express_bridge kiosk
```

The dashboard should be reachable at `http://<pi-ip>:3000` from any device on
the same WiFi network, and running fullscreen on the in-cab display.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Poller can't open the port | `pi` in `dialout` group? Adapter at `/dev/ttyUSB0`? (`lsusb`, `dmesg`) |
| Bridge won't start | Does `client/dist/index.html` exist? Is the DB path consistent? `journalctl -u express_bridge` |
| Kiosk shows artifacts / black screen | Confirm `WEBKIT_DISABLE_DMABUF_RENDERER=1` is set in `kiosk-start.sh` |
| Kiosk never opens | Is `/api/health` returning? Is a Wayland session up? `journalctl -u kiosk` |
| Screen orientation wrong | Edit `deploy/kanshi.config` and redeploy — don't hand-edit on the Pi |
| OTA not updating | `journalctl -u pull-deploy`; check sudoers entry and (if used) GitHub token/rate limit |
| Update from dashboard 409s | No newer release published, or an update is already running |
