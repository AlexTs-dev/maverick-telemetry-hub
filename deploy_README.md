# Deployment — Raspberry Pi 5

## Prerequisites

### 1. Install Mosquitto
```bash
sudo apt update
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
```

### 2. Set up Python virtual environment
```bash
cd /home/pi/maverick-telemetry-hub
python3 -m venv venv
source venv/bin/activate
pip install obd paho-mqtt
```

### 3. Initialize the database
```bash
source venv/bin/activate
python db/migrate.py
```

### 4. Grant USB access for OBDLink EX
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

## Installing the services

Copy all three service files to systemd and enable them:

```bash
sudo cp deploy/db_writer.service    /etc/systemd/system/
sudo cp deploy/trip_manager.service /etc/systemd/system/
sudo cp deploy/obd_poller.service   /etc/systemd/system/

sudo systemctl daemon-reload

sudo systemctl enable db_writer
sudo systemctl enable trip_manager
sudo systemctl enable obd_poller

sudo systemctl start db_writer
sudo systemctl start trip_manager
sudo systemctl start obd_poller
```

---

## Checking service status

```bash
sudo systemctl status db_writer
sudo systemctl status trip_manager
sudo systemctl status obd_poller
```

## Live logs

Each service logs to journald. Follow logs in real time:

```bash
journalctl -u db_writer    -f
journalctl -u trip_manager -f
journalctl -u obd_poller   -f
```

Follow all three at once:
```bash
journalctl -u db_writer -u trip_manager -u obd_poller -f
```

---

## Boot order

Services start in this order automatically via systemd dependencies:

```
mosquitto → db_writer → trip_manager → obd_poller
```

If any service fails, systemd restarts it after 5 seconds.
`obd_poller` does not require `trip_manager` to be running —
it will keep publishing readings to MQTT regardless. Boot order
is a best-effort courtesy, not a hard dependency.

---

## Stopping everything

```bash
sudo systemctl stop obd_poller
sudo systemctl stop trip_manager
sudo systemctl stop db_writer
```

## Disabling on boot

```bash
sudo systemctl disable obd_poller trip_manager db_writer
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
