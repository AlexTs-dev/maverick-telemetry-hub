# Jetson Orin Nano — vision + dashcam companion

The Jetson connects to the Raspberry Pi 5 by a **direct ethernet cable** and does
two jobs off one camera:

1. **Speed-limit sign detection** — a two-stage YOLO pipeline whose confirmed
   detections are published to the Pi's MQTT broker as JPEG snapshots.
2. **Dashcam** — continuous 1080p30 H.264 recording, segmented, retained ~30
   days, and served back to the Pi on request.

One camera, one pipeline: a second `cv2.VideoCapture` on the same `/dev/video*`
fails on essentially every V4L2 device, so a GStreamer `tee` feeds the encoder
and the inference branch from a single source.

**Footage never leaves the Jetson unless asked for.** The Pi stores only clip
metadata and streams the bytes over HTTP on demand — video is never put on MQTT.

## Files

| File | Purpose |
|---|---|
| `vision_publisher.py` | The main process: owns the camera, runs inference, publishes vision + dashcam metadata |
| `camera.py` | Frame-source arbiter — recorder appsink → `cv2.VideoCapture` → test pattern |
| `recorder.py` | GStreamer pipeline: tee → H.264 → segmented fragmented MP4 |
| `clipstore.py` | On-disk clip layout, sidecars, protection windows, retention pruner |
| `clip_server.py` | Read-only HTTP server with Range support, so footage can be seeked |
| `classifier.py` | Temporal gating (K-of-M confirmation) over raw model output |
| `speed_limit_model.py` | Two-stage YOLO: sign detector + value classifier |
| `focus_assist.py` | Live sharpness/exposure meter for setting the manual lens over SSH |
| `CAMERA-TUNING.md` | **Blurry frames?** Diagnosis and fix procedure — start there |
| `requirements.txt` | Dev-machine deps (see Jetson venv note below) |
| `deploy/bootstrap.sh` | **One-shot provisioning** — bare Jetson to running publisher |
| `deploy/vision_publisher.service` | systemd unit (installed on the Jetson) |
| `deploy/clip_server.service` | systemd unit for the read-only footage server |
| `deploy/chrony-jetson.conf` | Time sync from the Pi (load-bearing, see below) |
| `deploy/pull-deploy.sh` | Over-the-air updates — code from the release tag, models from the models release |
| `deploy/pull-deploy.service` | systemd oneshot that runs `pull-deploy.sh` |
| `deploy/pull-deploy.timer` | Triggers it 30s after boot, then every 2 minutes |

## MQTT contract (all QoS 1, JSON, no retain)

Published:

- `maverick/vision/status` — `{status, detail, ts}` on connect/change + 5 s
  heartbeat; `status ∈ connected | connecting | disconnected`. An MQTT Last
  Will on the same topic reports `disconnected` if the link drops.
- `maverick/vision/frame` — **one message per confirmed detection** (not on a
  timer): `{ts, frame_id, source, width_px, height_px, jpeg_b64, scene_label,
  confidence}`. `scene_label` prefixed `speed_limit_` came from the sign track.
- `maverick/vision/scene` — the lightweight twin of each `/frame`:
  `{ts, frame_id, scene_label, confidence}`.
- `maverick/dashcam/clip` — one closed video segment: `{clip_id, started_at,
  ended_at, duration_s, size_bytes, width_px, height_px, fps, path}`.
- `maverick/dashcam/status` — recording state + storage counters, on the same
  5 s heartbeat.
- `maverick/dashcam/pruned` — `{clip_ids, reason}` when retention deletes files,
  so the Pi can drop the matching rows.
- `maverick/dashcam/command_result` — confirmation of a command below.

Subscribed:

- `maverick/dashcam/command` — `delete` / `protect` / `unprotect` from the
  dashboard. The client uses `clean_session=False` with a fixed client id, so
  the broker **queues commands issued while the Jetson is offline**.
- `maverick/telemetry/crash_event` — on a `potential_crash` the recorder cuts
  the segment immediately and self-protects a ±2 min window, so incident footage
  survives even if the Pi's `db_writer` is down.

Everything is published **always** (no trip gating): trip events carry no trip
id and are not retained, so the Pi's `db_writer` is the single authority for
attaching frames and clips to trips.

## Dashcam

### Recording

Off by default. `deploy/bootstrap.sh` writes `VISION_RECORD_ENABLED=0` into
`~/.maverick-env`, so landing this code on a Jetson never changes its behaviour
until it is deliberately switched on:

```bash
sed -i 's/^VISION_RECORD_ENABLED=0/VISION_RECORD_ENABLED=1/' ~/.maverick-env
sudo systemctl restart vision_publisher
```

The pipeline is tried as a **ladder** of profiles and the first that reaches
`PLAYING` wins. Falling down the ladder is normal: a USB2 webcam cannot carry
raw 1080p30 (it exceeds the bus) and offers MJPG or H.264 instead.

### This board has NO hardware encoder — pick the camera accordingly

Verified on the device: `nvvideo4linux2` registers exactly one element
(`nvv4l2decoder`) and there is no `/dev/nvhost-msenc`. The Orin Nano SKU
(p3767-0005) has **NVDEC but not NVENC**. Measured here at 15 W, 1080p30:

| Path                        | Throughput                          |
| --------------------------- | ----------------------------------- |
| Camera H.264 passthrough    | no encode at all                    |
| `openh264enc`               | 1.5× realtime                       |
| `x264enc` ultrafast         | **0.65× realtime — cannot keep up** |
| `x264enc` @720p30           | 1.5× realtime                       |
| `x264enc` @1080p15          | 1.3× realtime                       |

The ladder therefore tries **`usb-h264` first**: if the camera encodes H.264
onboard, the recording branch is a straight `h264parse ! splitmuxsink` with no
re-encode, and the inference branch decodes with `nvv4l2decoder` in hardware.
Nothing touches the CPU. **Prefer a UVC camera with onboard H.264** (Logitech
C920/C922 and most business webcams). Otherwise recording falls back to
`openh264enc`, which keeps up at 1080p30 but competes with YOLO for six cores in
a thermally constrained cab.

### CSI image tuning — the defaults are wrong for a moving vehicle

An unconfigured `nvarguscamerasrc` suits a camera sitting still in a room:
auto-exposure will choose 1/30s in overcast light or a tunnel, and temporal noise
reduction blends consecutive frames. Both look fine on a static scene and both
smear a moving one — so the sign detector gets unreadable frames at exactly the
moment a sign passes, which is the only moment that matters.

`recorder.py` therefore caps the shutter at 1/250s, disables TNR and edge
enhancement, and turns `aeantibanding` off (it quantises exposure to the mains
period, putting an 8.3ms floor under a 4ms cap). This is a deliberate trade of
noise for sharpness: a noisy sharp frame can be read, a clean smeared one cannot.

**USB profiles are untouched by all of this** — it is Argus-only. The ladder
carries a `csi-untuned` rung directly beneath the tuned one, so a value Argus
rejects costs image quality rather than footage.

Everything is overridable in `~/.maverick-env`; `VISION_CSI_TUNING=0` restores
stock behaviour without a code change. **See
[CAMERA-TUNING.md](CAMERA-TUNING.md)** for the full variable list, and for the
procedure to tell defocus from motion blur from a plain lack of pixels on
target — they all present as "blurry" and have three different fixes.

`focus_assist.py` is the instrument for that: a live sharpness meter you can run
over SSH to set a manual lens, since the IMX477 has no autofocus.

### Fragmented MP4 is not optional — and it is set inline

`_record_tail()` configures `splitmuxsink` **inside the pipeline description**,
not with `set_property()` afterwards. This is deliberate and load-bearing:
splitmuxsink builds its muxer at construction (`async-finalize` defaults to
false), so setting `muxer-factory`/`muxer-properties` after `Gst.parse_launch()`
is silently ignored — the property even reads back correctly, which is what
makes it so easy to miss.

Measured, killing the recorder mid-segment with `SIGKILL`:

| Configuration            | Atoms                     | Result after a power cut |
| ------------------------ | ------------------------- | ------------------------ |
| `fragment-duration=1000` | `ftyp moov moof mdat …`   | **13 of 15s recovered**  |
| without it               | `ftyp free mdat(to-eof)`  | **unplayable, all lost** |

`_configure_splitmux()` asserts fragmentation is actually active at startup and
logs an error if it is not, so this cannot regress silently.

Segments are **fragmented MP4** with ~1 s fragments. Power is ignition-switched,
so an ungraceful cut mid-segment is the normal way this process dies; a plain
MP4 would lose its `moov` atom and be unrecoverable, whereas a fragmented file
still plays up to its last complete fragment.

**If recording cannot start, inference keeps running.** Missing PyGObject, no
NVENC, caps that will not negotiate, a pipeline that errors out mid-drive — each
demotes the frame source one rung and leaves the sign detector working. A
dashcam that cannot record is a degraded system; a Jetson that no longer detects
speed limit signs is a broken one.

### Storage

```
$MAVERICK_DASHCAM_ROOT/clips/YYYY/MM/DD/<clip_id>.mp4        the footage
                                        <clip_id>.json       exact end/duration/dims
                                        <clip_id>.protected  exempt from pruning
$MAVERICK_DASHCAM_ROOT/protected.json                        protection windows
```

The **filesystem is the source of truth**: a clip's start time is in its
filename and its end time is recoverable from mtime, so metadata survives a
power cut. There is deliberately no central index to fall out of sync with disk.

Retention deletes unprotected clips older than `MAVERICK_DASHCAM_RETENTION_DAYS`
(30), then oldest-first until under `MAVERICK_DASHCAM_MAX_BYTES` and above
`MAVERICK_DASHCAM_MIN_FREE_BYTES`. **Protected clips are never auto-deleted** —
if protected footage alone blows the budget, the pruner raises
`storage_pressure` and stops rather than destroying crash evidence to reclaim
space.

Exercise the pruner without a Jetson:

```bash
python clipstore.py --root /tmp/t --synth 500 --synth-days 45
python clipstore.py --root /tmp/t --protect trip-000001 --protect-from 2026-06-14T00:00:00+00:00
python clipstore.py --root /tmp/t --prune --retention-days 30
python clipstore.py --root /tmp/t --stats
```

### Serving

`clip_server.py` listens on `:8088` and is the Jetson's **only inbound
listener**. It implements GET and HEAD only — every other method gets a 501 —
and the unit adds `ReadOnlyPaths=` on the clip root, so read-only is enforced by
systemd rather than by the code staying correct. Deletion is the recorder's job.

Range support is the whole point: without it a `<video>` element can only play a
clip from the start, never seek. `http.server`'s `SimpleHTTPRequestHandler` does
not implement Range, which is why this is hand-rolled.

Config: `MAVERICK_DASHCAM_ROOT`, `MAVERICK_CLIP_SERVER_BIND`,
`MAVERICK_CLIP_SERVER_PORT`.

## Setup on the Jetson

### Quick start (recommended)

`deploy/bootstrap.sh` automates every step below and then hands ongoing updates
to the pull-deploy timer. Idempotent — safe to re-run.

```bash
sudo apt install -y git
git clone https://github.com/AlexTs-dev/maverick-telemetry-hub.git ~/maverick-telemetry-hub
~/maverick-telemetry-hub/jetson/deploy/bootstrap.sh
```

If the Jetson reaches the Pi over WiFi rather than the direct ethernet cable,
pass the Pi's LAN address — it sets **both** the chrony server and `MQTT_HOST`:

```bash
MAVERICK_PI_HOST=10.0.0.18 ~/maverick-telemetry-hub/jetson/deploy/bootstrap.sh
```

That writes `~/.maverick-env` (`MQTT_HOST=…`), which is **not** in git, so a
per-device address survives every deploy. `vision_publisher.service` reads it
after its baked-in `Environment=` lines, so it wins.

### WiFi must stay disabled

**Do not enable WiFi on the Jetson.** The out-of-tree Realtek driver
(`rtl8822ce` v5.14.0.4, JetPack R39) corrupts an hrtimer rbtree, producing a
kernel Oops in `rb_next ← __remove_hrtimer ← hrtimer_cancel ← timerfd_release ←
__arm64_sys_close` from NetworkManager. Because `panic_on_oops=1`, that panics
the board — observed **12 reboots in ~2.5 hours**, at 5–40 minute intervals.

It is invisible in the journal: the board resets before journald can flush, so
it looks like spontaneous reboots or "flaky WiFi". The evidence survives only in
`pstore`:

```bash
sudo cat /sys/fs/pstore/dmesg-ramoops-0        # or /var/lib/systemd/pstore/
```

The driver is blacklisted in `/etc/modprobe.d/blacklist-maverick-wifi.conf`.
Deleting that file re-introduces the crash. Use the direct ethernet link
instead — which is the intended topology anyway — and let the Pi NAT for
internet access (see [deploy/README.md](../deploy/README.md)).

### Over-the-air updates

The Jetson keeps itself current exactly as the Pi does — polling GitHub
Releases every 2 minutes over outbound HTTPS only, with two independent tracks:

- **Code** — `jetson/` checked out at the latest `deploy-*` release tag. There
  is no build artifact to download; the Jetson runs pure Python.
- **Models** — `*.pt` from the fixed `models-latest` release, re-downloaded only
  when the assets' fingerprint changes (the tag is fixed, so the tag name says
  nothing about whether the payload moved). New weights also retire any stale
  `.engine`, since an engine built from the old weights would silently keep
  serving the previous model.

Retraining therefore ships without a code commit, and a code release doesn't
reship 8 MB of identical weights. Publish new weights from the desktop with
`tools/publish-models.sh`. No models present is a supported state.

```bash
journalctl -u pull-deploy -f          # OTA activity
cat ~/.maverick-deployed-tag          # currently deployed release
```

### Manual setup

The steps below are what `bootstrap.sh` automates; follow them only when
provisioning by hand or debugging.

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

3. **OpenCV** — prefer NVIDIA's JetPack build over Ubuntu's generic one; it
   carries the Jetson GStreamer integration the CSI path needs:

   ```bash
   sudo apt install -y nvidia-opencv libopencv-python
   # Ubuntu's python3-opencv installs cv2 into /usr/lib/python3/dist-packages
   # and SHADOWS NVIDIA's bindings — remove it if present, or `import cv2`
   # silently keeps returning the generic build.
   sudo apt remove -y python3-opencv
   python3 -c "import cv2; print(cv2.__version__, cv2.__file__)"
   ```

   > **CUDA note:** stock JetPack OpenCV is *not* built with the `cv2.cuda`
   > module, so `cv2.cuda.getCudaEnabledDeviceCount()` returning `0` is normal
   > and not a fault. It doesn't affect this system — `vision_publisher` uses
   > cv2 only for capture and JPEG encode (hardware paths come from
   > GStreamer/NVMM) and never calls `cv2.cuda.*`. Only a hand-built OpenCV
   > with `-DWITH_CUDA=ON` reports otherwise.

4. **Python venv** — use `--system-site-packages` so the system OpenCV is
   visible, and install only paho:

   ```bash
   cd ~/maverick-telemetry-hub/jetson
   python3 -m venv --system-site-packages venv
   ./venv/bin/pip install paho-mqtt
   ./venv/bin/python -c "import cv2; print(cv2.__version__)"
   ```

   Never `pip install opencv-python` into this venv — that CPU-only wheel would
   shadow the system build. `requirements.txt` is for dev machines only.

5. **systemd unit**:

   ```bash
   sudo cp deploy/vision_publisher.service /etc/systemd/system/
   sudo cp deploy/clip_server.service /etc/systemd/system/
   sudo cp deploy/pull-deploy.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now vision_publisher clip_server pull-deploy.timer
   journalctl -u vision_publisher -f
   ```

6. **Passwordless restart** — `pull-deploy.sh` restarts the publisher
   unattended, with no TTY to type a password into:

   ```bash
   # /etc/sudoers.d/maverick  (edit with: sudo visudo -f /etc/sudoers.d/maverick)
   jetson ALL=(root) NOPASSWD: /usr/bin/systemctl restart vision_publisher
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
