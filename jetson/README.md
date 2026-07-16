# Jetson Orin Nano — vision companion

AI-vision boilerplate for the Maverick Telemetry Hub. The Jetson connects to
the Raspberry Pi 5 by a **direct ethernet cable** and publishes camera
snapshots + a status heartbeat to the Pi's MQTT broker. The Pi side
(`db_writer.py`) persists frames, attaches them to the active trip, and the
dashboard lines them up with OBD-II readings by timestamp.

**No inference yet** — `classifier.py` is a stub that returns
`{"scene_label": None, "confidence": None}`. Real scene classification
(TensorRT/PyTorch on the Orin) drops into that contract later.

## Files

| File | Purpose |
|---|---|
| `vision_publisher.py` | The only process: camera → JPEG → MQTT, plus heartbeat |
| `classifier.py` | Scene-classification stub pinning the future inference contract |
| `requirements.txt` | Dev-machine deps (see Jetson venv note below) |
| `deploy/vision_publisher.service` | systemd unit (installed on the Jetson) |
| `deploy/chrony-jetson.conf` | Time sync from the Pi (load-bearing, see below) |

## MQTT contract (all QoS 1, JSON, no retain)

- `maverick/vision/status` — `{status, detail, ts}` on connect/change + 5 s
  heartbeat; `status ∈ connected | connecting | disconnected`. An MQTT Last
  Will on the same topic reports `disconnected` if the link drops.
- `maverick/vision/frame` — every `VISION_SNAPSHOT_INTERVAL` s (default 10):
  `{ts, frame_id, source: "periodic"|"event", width_px, height_px, jpeg_b64,
  scene_label, confidence}`. The stub always sends null scene fields.
- `maverick/vision/scene` — reserved for future scene-change events; the
  stub never publishes it.

Frames are published **always** (no trip gating): trip events carry no trip
id and are not retained, so the Pi's `db_writer` is the single authority for
attaching frames to trips (and drops frames outside a trip, same as OBD
readings).

## Setup on the Jetson

1. **Static IP on the ethernet link** (Pi is `192.168.100.1`):

   ```bash
   sudo nmcli con add type ethernet ifname eth0 con-name maverick-pi \
       ipv4.method manual ipv4.addresses 192.168.100.2/24 ipv6.method disabled
   sudo nmcli con up maverick-pi
   ping 192.168.100.1
   ```

2. **Time sync from the Pi** (no internet in the truck — the Pi is the only
   time source, and frame/reading alignment depends on it):

   ```bash
   sudo apt install chrony
   sudo cp deploy/chrony-jetson.conf /etc/chrony/conf.d/maverick.conf
   sudo systemctl restart chrony
   chronyc tracking   # Reference ID must show 192.168.100.1
   ```

   Until the clock syncs, `vision_publisher` publishes status
   `connecting / waiting for clock sync` and **no frames** (the Orin has no
   battery-backed RTC and boots in 1970).

3. **Python venv** — use `--system-site-packages` so JetPack's
   CUDA-accelerated OpenCV is used, and install only paho:

   ```bash
   cd ~/maverick-telemetry-hub/jetson
   python3 -m venv --system-site-packages venv
   ./venv/bin/pip install paho-mqtt
   ./venv/bin/python -c "import cv2; print(cv2.getBuildInformation())"  # expect CUDA: YES
   ```

4. **systemd unit**:

   ```bash
   sudo cp deploy/vision_publisher.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now vision_publisher
   journalctl -u vision_publisher -f
   ```

The camera source defaults to `auto` (first V4L2 device). For the CSI camera,
set a GStreamer pipeline in the unit, e.g.:

```
Environment=VISION_SOURCE=nvarguscamerasrc ! video/x-raw(memory:NVMM),width=1280,height=720 ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! appsink
```

## Dev machine (no Jetson, no camera)

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
MQTT_HOST=localhost VISION_SOURCE=test python vision_publisher.py
```

`VISION_SOURCE=test` publishes a generated moving test pattern with the
timestamp burned in — the full pipeline works end-to-end with no hardware.
Watch it with:

```bash
mosquitto_sub -t 'maverick/vision/#' -v
```

Note: the Pi's broker only listens on the ethernet/LAN after
`deploy/mosquitto-maverick.conf` is installed (see `deploy/README.md`); for
pure dev work run a local mosquitto instead.
