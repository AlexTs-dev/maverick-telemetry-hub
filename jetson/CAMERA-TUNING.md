# Camera tuning — diagnosing and fixing blurry frames

A field procedure for when the IMX477 is producing soft images and sign
detection accuracy is suffering. Runnable entirely over SSH; the only physical
step is turning the lens ring.

**The IMX477 has no autofocus.** There is no focus actuator in the sensor or the
module — focus lives entirely in the lens barrel and, on the Pi HQ board, the
back-focus ring behind the lens mount. That is not the limitation it sounds
like: everything this system cares about (signs at 20–80 m) is at effective
infinity, so a lens set once and locked is *better* than autofocus, which would
hunt and would lock onto the windscreen, wiper streaks and dash reflections
exactly when a sign enters frame.

Three different faults produce "blurry", and they have three different fixes.
**Work out which one you have before changing anything** — §1 takes two minutes
and saves you from tuning the wrong thing.

| Fault | Looks like | Fixed in |
|---|---|---|
| Defocus | Soft even parked, engine off | §2 — the lens |
| Motion blur | Sharp parked, smeared moving | §3 — the pipeline |
| Not enough pixels on target | Sharp, but signs are tiny | §4 — the lens focal length |

---

## 0. Getting in

The Jetson is on the direct ethernet link behind the Pi:

```bash
ssh -J pi@10.0.0.18 jetson@192.168.100.2
cd ~/maverick-telemetry-hub/jetson
```

**Exactly one process may own the camera.** Every procedure below opens it
directly, so stop the publisher first and remember to start it again:

```bash
sudo systemctl stop vision_publisher      # prompts for a password — the
                                          # NOPASSWD sudoers rule covers
                                          # `restart` only, not `stop`
...
sudo systemctl start vision_publisher
```

---

## 1. Which blur is it?

**Park, engine off, ignition on.** Engine off matters — idle vibration alone can
smear a long exposure and confound the whole test. Aim at something with fine
detail 30–50 m away: a number plate, a road sign, brickwork, a fence line.

Grab a still:

```bash
gst-launch-1.0 nvarguscamerasrc num-buffers=30 \
  ! 'video/x-raw(memory:NVMM),width=1920,height=1080' \
  ! nvvidconv ! video/x-raw,format=I420 \
  ! jpegenc ! multifilesink location=/tmp/still_%03d.jpg
```

`num-buffers=30` is not arbitrary: auto-exposure and auto-white-balance take
about a second to converge, so the first frames are dark and magenta. **Use
`/tmp/still_029.jpg`**, not `000`.

Pull it back and look at it full size:

```bash
scp -J pi@10.0.0.18 jetson@192.168.100.2:/tmp/still_029.jpg .
```

Then read the result:

- **Soft in this still, parked and stationary** → defocus. Go to **§2**. Nothing
  in software will fix it.
- **Sharp here, but blurry in actual driving footage** → motion blur. Go to
  **§3**.
- **Sharp, correctly exposed, but signs occupy a handful of pixels** → you are
  resolution-limited, not blur-limited. Go to **§4**.

If you want the same answer as a number rather than by eye, `focus_assist.py`
(§2) prints one, and it is the more reliable read.

---

## 2. Defocus — setting the lens

### 2a. Check back-focus first

The single most common cause of "blurry no matter how I turn the ring" on the Pi
HQ board is **not** the focus ring — it is the back-focus adjustment ring, the
wide knurled ring between the lens mount and the PCB, held by a small grub screw
on the side. If it has drifted, the lens physically cannot reach infinity focus
and the focus ring will not save it.

You are also adjusting this if you have ever swapped between a C-mount and a
CS-mount lens, or added/removed the 5 mm C-CS adapter ring.

### 2b. Peak the meter

Turning a ring while guessing from a small preview does not get you close
enough. `focus_assist.py` gives you a live number to chase:

```bash
sudo systemctl stop vision_publisher
./venv/bin/python focus_assist.py
```

```
sharp    1842.3  |################----|  97% of peak   luma 118  clip 0.4%  29.1 fps
```

**Aim at something at least 100 m away** — a treeline, a building, the far end of
the street. Then:

1. Loosen the back-focus grub screw.
2. Set the lens focus ring to its infinity mark.
3. Turn the **back-focus** ring slowly until `sharp` peaks. Chase
   `% of peak` — the held peak decays on a 20 s half-life, so overshooting and
   coming back works; you do not have to restart.
4. Lock the grub screw **without letting the barrel rotate**, then re-run and
   confirm the reading held. It is very easy to lose focus while tightening.
5. Fine-tune on the focus ring only if needed.

Because everything of interest is far away, focusing at distance is essentially
optimal. Depth of field then runs from roughly half the hyperfocal distance out
to infinity — with a 12 mm lens at f/2 that is about 12 m onward, which covers
every sign you will read.

Useful flags:

```bash
./venv/bin/python focus_assist.py --grid              # 3x3 sharpness map
./venv/bin/python focus_assist.py --save /tmp/foc     # keep JPEGs
./venv/bin/python focus_assist.py --untuned           # A/B against untuned ISP
./venv/bin/python focus_assist.py --list-modes        # Argus sensor modes
```

`--grid` reads out problems the centre crop cannot see:

- **corners uniformly soft** — the lens; normal on a cheap wide M12, and it does
  not matter, because signs are read near the centre;
- **one edge soft, opposite edge sharp** — sensor tilt or a lens not seated
  square in its mount; reseat it;
- **centre soft, edges sharper** — focus has gone past infinity.

> The metric is variance-of-Laplacian and is contrast-dependent, so the absolute
> number is only comparable against the *same scene*. Do not compare today's
> 1842 against last week's — compare within one session, chasing the peak.

---

## 3. Motion blur — the pipeline

An unconfigured `nvarguscamerasrc` is wrong for a dashcam: its defaults suit a
camera sitting still in a room. Auto-exposure will pick 1/30 s in overcast light
or a tunnel, and temporal noise reduction blends consecutive frames. Both look
fine on a static scene and both smear a moving one — so the detector receives
unreadable frames at exactly the moment a sign passes.

`recorder.py` now sets these. Defaults, all overridable in `~/.maverick-env`:

| Variable | Default | Why |
|---|---|---|
| `VISION_CSI_TUNING` | `1` | Master switch. `0` restores stock behaviour |
| `VISION_CSI_EXPOSURE_MAX_US` | `4000` | Shutter cap, µs. 4000 = 1/250 s |
| `VISION_CSI_EXPOSURE_MIN_US` | `13` | Sensor floor |
| `VISION_CSI_GAIN_MAX` | `16` | Analog gain ceiling |
| `VISION_CSI_ISP_GAIN_MAX` | `8` | Digital gain — brightens noise, does not recover detail |
| `VISION_CSI_TNR_MODE` | `0` | Temporal noise reduction off — it ghosts anything moving |
| `VISION_CSI_EE_MODE` | `0` | Edge enhancement off — draws halos the optics never resolved |
| `VISION_CSI_AEANTIBANDING` | `0` | See below |
| `VISION_CSI_SENSOR_MODE` | `-1` | `-1` lets Argus choose |

Two of these are worth understanding rather than just accepting.

**The exposure cap is a real trade, not a free win.** A shorter shutter costs
noise, because AE compensates with gain. That is the right trade here — a noisy
sharp frame can be read, a clean smeared one cannot — but it does mean night
footage gets grainier. If night detection suffers more than daytime improves,
raise `VISION_CSI_EXPOSURE_MAX_US` toward 8000 (1/125 s).

**`aeantibanding` is off for a non-obvious reason.** Antibanding quantises
exposure to multiples of the mains period (1/100 s or 1/120 s) so fluorescent
lighting does not band — which would put an 8.3 ms floor under a 4 ms cap and
silently undo it. There is no mains lighting on a road. Turn it back on
(`=1`) only if you see rolling bands in tunnel or car-park footage.

### Applying and reverting

```bash
echo 'VISION_CSI_EXPOSURE_MAX_US=2000' >> ~/.maverick-env   # 1/500 s
sudo systemctl restart vision_publisher
journalctl -u vision_publisher -f | grep -i profile
```

`~/.maverick-env` is not in git, so per-device values survive every pull-deploy.

To back the whole thing out without a code change:

```bash
echo 'VISION_CSI_TUNING=0' >> ~/.maverick-env
sudo systemctl restart vision_publisher
```

### If tuning breaks the pipeline

Argus rejects a property it cannot honour — an exposure range outside what the
chosen sensor mode supports, or a `sensor-mode` absent from this device tree —
by failing the pipeline at `PLAYING`. The ladder carries an untuned CSI rung
directly beneath the tuned one for exactly this reason, so a bad value costs
image quality rather than footage. You will see it in the log:

```
Profile csi unusable: did not reach PLAYING (...)
Frame source: recorder appsink (recording + inference)
```

A `csi-untuned` profile winning means one of your values was rejected. Check
`--list-modes` before pinning `VISION_CSI_SENSOR_MODE`.

### Sensor mode is worth pinning

Left unset, Argus picks a mode to satisfy the requested caps and may choose a
binned or cropped readout without telling you — same resolution out, different
sharpness and a different field of view. List them, try each, compare with
`focus_assist.py` on an identical static scene:

```bash
./venv/bin/python focus_assist.py --list-modes
VISION_CSI_SENSOR_MODE=0 ./venv/bin/python focus_assist.py
VISION_CSI_SENSOR_MODE=1 ./venv/bin/python focus_assist.py
```

---

## 4. Not enough pixels on the sign

This is the one that is *not* blur, is frequently mistaken for it, and is often
the real accuracy limiter. If focus and exposure are both right and signs still
read poorly, count the pixels.

Sign width on sensor, for a 24-inch (0.61 m) sign, 1920 px across the IMX477's
6.287 mm active width:

| Lens | HFOV | @30 m | @50 m | @80 m |
|---|---|---|---|---|
| 6 mm | 55° | 37 px | 22 px | 14 px |
| 12 mm | 29° | 75 px | 45 px | 28 px |
| 16 mm | 22° | 99 px | 60 px | 37 px |

Then remember the pipeline downscales: the inference branch runs at 1280×720
(×0.67), and the YOLO stage letterboxes to its own input size again. **A 6 mm
lens delivers the value classifier roughly a 7-pixel sign at 50 m.** No amount
of focusing fixes a 7-pixel sign — there is not enough there to read.

So if you are going to change hardware, **change the lens, not the camera**. A
12–16 mm C-mount on the IMX477 you already own roughly triples linear resolution
on target. The cost is field of view, which for a forward-facing sign reader is
an acceptable trade and arguably an improvement. The 6 mm CS lens bundled with
most HQ kits is also simply the weakest optic in the kit.

Cheaper still, and worth trying first: crop rather than downscale for inference.
`VISION_RECORD_INFER_WIDTH/HEIGHT` currently scale the whole 1080p frame down;
taking a centre crop at native resolution instead would preserve far more pixels
on target at identical inference cost. Not implemented — noted here as the next
obvious move.

*(FOV figures are geometric and ignore lens distortion. Verify against your
actual lens.)*

---

## 5. Things that are not the camera

Before buying anything, rule these out — all of them present as "blurry":

- **The windscreen.** Dirt, tint, an IR-reflective coating, or the camera aimed
  through a section the wipers do not reach. Wipe it and re-shoot §1.
- **Reflections.** A bright dash or a light-coloured dashmat reflecting into the
  glass washes out contrast. A polariser or a dark mat helps.
- **The protective film.** M12 and CS lenses ship with a film or a plastic cap
  over the front element that is easy to leave on.
- **Mount vibration.** A suction mount on a long arm resonates; the footage is
  sharp when parked and soft at 100 km/h regardless of shutter speed. Check by
  comparing §1 parked-engine-off against parked-engine-running.
- **Rotation.** If the camera is mounted upside down and `VISION_CSI_FLIP` is
  not set, the detector gets 180°-rotated frames — far outside its training
  distribution, which shows up as confident nonsense rather than as no output.

---

## Quick reference

```bash
# diagnose
ssh -J pi@10.0.0.18 jetson@192.168.100.2
cd ~/maverick-telemetry-hub/jetson
sudo systemctl stop vision_publisher

gst-launch-1.0 nvarguscamerasrc num-buffers=30 \
  ! 'video/x-raw(memory:NVMM),width=1920,height=1080' \
  ! nvvidconv ! video/x-raw,format=I420 ! jpegenc \
  ! multifilesink location=/tmp/still_%03d.jpg      # use still_029.jpg

# focus
./venv/bin/python focus_assist.py --grid

# tune
echo 'VISION_CSI_EXPOSURE_MAX_US=2000' >> ~/.maverick-env
sudo systemctl restart vision_publisher

# revert everything
echo 'VISION_CSI_TUNING=0' >> ~/.maverick-env
sudo systemctl restart vision_publisher

sudo systemctl start vision_publisher
```
