# Deployment — Raspberry Pi 5

## Prerequisites

### 1. Install Mosquitto
```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

### 2. Install Node.js
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

### 3. Install Rust
```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
```

### 4. Install Tauri system dependencies (WebKitGTK)
```bash
sudo apt install -y \
  libwebkit2gtk-4.1-dev \
  libgtk-3-dev \
  librsvg2-dev \
  libjavascriptcoregtk-4.1-dev \
  build-essential \
  curl \
  wget \
  file \
  libxdo-dev \
  libssl-dev \
  libayatana-appindicator3-dev
```

### 5. Set up Python virtual environment
```bash
cd /home/pi/maverick-telemetry-hub
python3 -m venv venv
source venv/bin/activate
pip install obd paho-mqtt
```

### 6. Initialize the database
```bash
source venv/bin/activate
# MAVERICK_DB_PATH must match the value in the systemd *.service files,
# otherwise the DB is created at migrate.py's repo-relative default and
# the services won't find it.
MAVERICK_DB_PATH=/home/pi/maverick_telemetry.db python db/migrate.py
```

### 7. Install Node dependencies and build the Tauri app
```bash
cd server && npm install && cd ..
cd client && npm install && npm run tauri:build && cd ..
```

The compiled binary will be at `client/src-tauri/target/release/maverick-telemetry`.

### 8. Create the environment file
```bash
echo "ANTHROPIC_API_KEY=your_key_here" > server/.env
```

### 9. Grant USB access for OBDLink EX
The poller runs as the `pi` user, not root. Add pi to the dialout
group so it can access /dev/ttyUSB0 without sudo:
```bash
sudo usermod -a -G dialout pi
# Log out and back in for the group change to take effect
```

Confirm the OBDLink EX is visible after plugging in:
```bash
ls /dev/ttyUSB*
# Should show /dev/ttyUSB0
```

---

## Continuous deployment (pull-based)

On every push to `main`, GitHub Actions builds the Tauri app on an ARM64
runner and publishes a GitHub Release with the binary and React dist as assets.
The Pi polls the releases API every 2 minutes and applies the update itself —
no inbound SSH, no public IP, no secrets on GitHub.

### One-time Pi setup

Enable passwordless service restarts. The path must match what `pull-deploy.sh`
actually invokes — it calls bare `systemctl`, which resolves to
`/usr/bin/systemctl`, so use that exact path here:
```bash
echo "pi ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart express_bridge kiosk" \
  | sudo tee /etc/sudoers.d/maverick-deploy
sudo chmod 440 /etc/sudoers.d/maverick-deploy
sudo visudo -c   # validate before relying on it
```
This is load-bearing: without it, `pull-deploy.sh`'s final
`sudo systemctl restart` fails and the whole deploy silently loops (see
Troubleshooting below).

Make the script executable and install the timer:
```bash
chmod +x /home/pi/maverick-telemetry-hub/deploy/pull-deploy.sh

sudo cp deploy/pull-deploy.service /etc/systemd/system/
sudo cp deploy/pull-deploy.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pull-deploy.timer
```

### Optional: GitHub token for higher API rate limits

Unauthenticated requests are capped at 60/hour. At 2-minute polling that's
fine, but a token raises the limit to 5000/hour:

```bash
echo "GITHUB_TOKEN=ghp_yourtoken" > /home/pi/.maverick-env
# Uncomment the EnvironmentFile line in pull-deploy.service, then:
sudo systemctl daemon-reload
```

### Check deploy status

```bash
# See the last deploy run
journalctl -u pull-deploy -n 50

# Watch live
journalctl -u pull-deploy -f

# Check timer schedule
systemctl status pull-deploy.timer
```

### Troubleshooting: deploy "runs" but the app never updates

Symptom: the timer fires every 2 min and the logs show
`New release: deploy-XXX (was: ...)`, the new binary's mtime updates on disk,
**but** `express_bridge`/`kiosk` keep their old `ActiveEnterTimestamp` and
`~/.maverick-deployed-tag` never advances — so the same release re-downloads
forever.

Cause: the final `sudo systemctl restart express_bridge kiosk` fails (look for
`pam_unix(sudo:auth)` / "a password is required" in
`journalctl -u pull-deploy`). Because `pull-deploy.sh` runs `set -euo pipefail`
and writes the deployed-tag file *after* the restart, it dies before recording
success — files land on disk but services are never restarted. Almost always the
`/etc/sudoers.d/maverick-deploy` NOPASSWD entry is missing or has the wrong
`systemctl` path (see One-time Pi setup). Reinstall it, then run
`~/maverick-telemetry-hub/deploy/pull-deploy.sh` once to confirm it ends with
`Deployed deploy-XXX successfully`.

---

## Installing the services

Copy all service files to systemd and enable them:

```bash
sudo cp deploy/db_writer.service       /etc/systemd/system/
sudo cp deploy/trip_manager.service    /etc/systemd/system/
sudo cp deploy/obd_poller.service      /etc/systemd/system/
sudo cp deploy/express_bridge.service  /etc/systemd/system/
sudo cp deploy/kiosk.service           /etc/systemd/system/
sudo cp deploy/pull-deploy.service     /etc/systemd/system/
sudo cp deploy/pull-deploy.timer       /etc/systemd/system/

sudo systemctl daemon-reload

sudo systemctl enable db_writer trip_manager obd_poller express_bridge kiosk pull-deploy.timer
sudo systemctl start  db_writer trip_manager obd_poller express_bridge kiosk pull-deploy.timer
```

The `kiosk.service` launches the Tauri binary in place of Chromium.

---

## Kiosk display rotation

The in-cab DSI panel runs under **labwc** with **kanshi** managing outputs. Its
rotation and mode are version-controlled in [`kanshi.config`](kanshi.config);
`pull-deploy.sh` installs it to `~/.config/kanshi/config` (and mirrors it to
`config.init`) on every deploy, so the setting survives a reimage and changes
flow through git like everything else. Pi-5 KMS ignores the legacy
`display_rotate` in `config.txt`, so kanshi is the mechanism here.

To rotate the screen, edit the `transform` value in `deploy/kanshi.config`
(`normal` | `90` | `180` for upside-down | `270`), commit, and push. The Pi
applies it within ~2 minutes (live via `wlr-randr` when a session is up, and on
every boot when kanshi reads the config). Only `transform` is applied live;
changing `mode`, `scale`, or `position` takes effect on the next reboot when
kanshi re-reads the full profile. Do **not** hand-edit `~/.config/kanshi/config`
on the Pi — the next deploy overwrites it.

A 180° flip also inverts the `ft5x06` touchscreen automatically: wlroots applies
the output transform to touch input, so no calibration matrix is needed.

On a **fresh image**, before `pull-deploy` has run once, install it by hand:
```bash
mkdir -p ~/.config/kanshi
cp deploy/kanshi.config ~/.config/kanshi/config
cp deploy/kanshi.config ~/.config/kanshi/config.init
```

---

## Checking service status

```bash
sudo systemctl status db_writer
sudo systemctl status trip_manager
sudo systemctl status obd_poller
sudo systemctl status express_bridge
sudo systemctl status kiosk
```

## Live logs

Each service logs to journald. Follow logs in real time:

```bash
journalctl -u db_writer      -f
journalctl -u trip_manager   -f
journalctl -u obd_poller     -f
journalctl -u express_bridge -f
journalctl -u kiosk          -f
```

Follow all at once:
```bash
journalctl -u db_writer -u trip_manager -u obd_poller -u express_bridge -f
```

---

## Boot order

Services start in this order automatically via systemd dependencies:

```
mosquitto → db_writer → trip_manager → obd_poller
                     → express_bridge → kiosk
```

If any service fails, systemd restarts it after 5 seconds.

---

## Updating

After pulling new code:

```bash
git pull

# Rebuild the Tauri app if client files changed
cd client && npm run tauri:build && cd ..

sudo systemctl restart kiosk
```

If only the Express server changed:
```bash
sudo systemctl restart express_bridge
```

If Python dependencies changed: `pip install -r requirements.txt`
If service files changed: re-copy and run `sudo systemctl daemon-reload`

---

## Stopping everything

```bash
sudo systemctl stop obd_poller trip_manager db_writer express_bridge kiosk
```

## Disabling on boot

```bash
sudo systemctl disable obd_poller trip_manager db_writer express_bridge kiosk
```

---

## Troubleshooting

**OBDLink EX not found at /dev/ttyUSB0**
- Unplug and replug the adapter
- Run `dmesg | tail -20` to see USB enumeration events
- Confirm pi is in the dialout group: `groups pi`

**MQTT connection refused**
- Check Mosquitto is running: `sudo systemctl status mosquitto`
- Test manually: `mosquitto_sub -t 'maverick/#' -v`

**Readings not appearing in database**
- Check db_writer logs: `journalctl -u db_writer -f`
- Confirm database exists: `ls -lh /home/pi/maverick_telemetry.db`
- Check WAL files aren't corrupted: `sqlite3 /home/pi/maverick_telemetry.db "PRAGMA integrity_check;"`

**Trip has no summary stats (all dashes)**
- If the Pi lost power mid-trip, db_writer will recover the trip automatically on next boot
- Check logs for "Recovered unclosed trip": `journalctl -u db_writer -b | grep Recovered`

**Tauri window not opening**
- Check kiosk service logs: `journalctl -u kiosk -f`
- Make sure the binary exists: `ls client/src-tauri/target/release/maverick-telemetry`
- Confirm WebKitGTK is installed: `dpkg -l libwebkit2gtk-4.1-dev`

**Dashboard not loading in Tauri window**
- Confirm express_bridge is running before kiosk starts: `sudo systemctl status express_bridge`
- Check server logs: `journalctl -u express_bridge -f`
