# Training the speed-limit models

Everything here runs on a desktop with an NVIDIA GPU (or Colab) — **not** on
the Jetson and **not** in this repo's runtime. The outputs are two `.pt`
files that go to `jetson/models/` (see the README there for the scp +
on-device TensorRT engine build).

Two models, two datasets, both derived from the same sources:

| Model | Task | Classes |
|---|---|---|
| `speed_limit_detector` (YOLO26n) | find the sign | 1: `speed_limit_sign` |
| `speed_limit_value` (YOLO11n-cls) | read the number from the crop | `25,30,…,75` + `other` |

Why two stages: LISA has ~1000+ instances of 35 mph but single-digit counts
of 55/65 — a per-value detector fails on rare values. Pooling every speed
sign into one detector class makes detection reliable regardless of value,
and the crop classifier trains on tight, scale-normalized images where the
digits are actually legible. The `other` class lets stage 2 veto stage 1
false positives.

## 0. Environment

The training venv lives at **`C:\maverick-training\venv`** (Python 3.12) —
**outside the OneDrive-synced tree**, and so should datasets and `runs/`
(e.g. `C:\datasets\`). OneDrive churning on multi-GB torch installs and tens
of thousands of images will fight you. Run `yolo` commands from `C:\datasets\`.

Two GPU paths:

### AMD Radeon (this machine — RX 9070 XT, ROCm on Windows)

ROCm's Windows PyTorch wheels are **cp312 — Python 3.12 only** (not 3.14).
Install the ROCm runtime + torch **before** ultralytics so pip keeps the
ROCm build instead of pulling a CPU/CUDA torch over it:

```
py -3.12 -m venv C:\maverick-training\venv
set PY=C:\maverick-training\venv\Scripts\python.exe
set BASE=https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1
%PY% -m pip install --no-cache-dir ^
  %BASE%/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl ^
  %BASE%/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl ^
  %BASE%/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl ^
  %BASE%/rocm-7.2.1.tar.gz
%PY% -m pip install --no-cache-dir ^
  %BASE%/torch-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl ^
  %BASE%/torchvision-0.24.1+rocm7.2.1-cp312-cp312-win_amd64.whl
%PY% -m pip install ultralytics albumentations pyyaml
```

Requires **AMD Adrenalin driver 26.2.2+** (RDNA4/gfx1201 support). Verify the
GPU is visible before training — ROCm masquerades as CUDA, so the training
commands below run unchanged with `device=0`:

```
%PY% -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

`True  AMD Radeon RX 9070 XT` means you're set. `False` usually means the
Adrenalin driver is older than 26.2.2 — update it from AMD's site and
recheck. gfx1201 on Windows is still maturing: pin the stable ROCm release
above (a training-crash bug existed in ROCm 7.12, fixed 7.13) and **never
use nightly wheels**. Windows ROCm can also be slower than the card's
Linux/CUDA-equivalent ceiling — fine for the minutes-long classifier
retrains; if the one-time detector train drags, run just that on Colab.

### NVIDIA / Colab

```
pip install ultralytics albumentations pyyaml
```

Colab's free tier trains both models with zero local setup — a good fallback
for the one-time detector run.

`albumentations` enables ultralytics' built-in blur/CLAHE augmentation block
(dashcam motion blur, low light). `pyyaml` + `opencv-python` are needed by
the scripts here; ultralytics pulls opencv in.

## 1. Get the datasets

- **LISA (US, dashcam, per-value)** — easiest via the Roboflow mirror
  `universe.roboflow.com/dakota-smith/lisa-road-signs`: create a free
  account, export as **"YOLO"** format, download and unzip to
  `C:\datasets\lisa`. Academic license — fine for this personal project.
- **Mapillary MTSD (per-value US top-up)** — register at
  `mapillary.com/dataset/trafficsign` (CC-BY-SA). Its annotations need
  converting to YOLO format first (Roboflow can ingest and re-export, which
  also lets you filter to the `regulatory--maximum-speed-limit-*--g1`
  classes plus a sample of other signs for negatives). Unzip to
  `C:\datasets\mtsd`.
- **Do not use European/GTSRB-derived sets** — red-ring circular signs with
  km/h values (20/30/50…) that don't exist on US roads.

## 2. Build the detector dataset (single class + hard negatives)

```
python remap_to_detector.py --src C:\datasets\lisa --src C:\datasets\mtsd ^
    --out C:\datasets\speed_limit_det --keep-negatives 0.3
```

Every annotation matching `speedLimit(\d+)` (LISA) or
`regulatory--maximum-speed-limit-(\d+)--g1` (MTSD) becomes class 0; other
annotations are dropped; 30% of images with no speed sign are kept as hard
negatives (empty label files). Train/val are split **by clip group, never by
frame** — these datasets are video-derived and frame-level splits leak
near-duplicates into val. The script prints instance counts and asserts the
split is group-disjoint.

## 3. Train the detector

```
yolo detect train data=C:\datasets\speed_limit_det\dataset.yaml model=yolo26n.pt ^
    epochs=100 imgsz=960 batch=-1 patience=20 close_mosaic=15 ^
    degrees=3 translate=0.1 scale=0.5 fliplr=0.0 name=sl_det
```

- **`fliplr=0.0` is not optional** — horizontal flips mirror the digits.
- `close_mosaic=15` stops mosaic for the last 15 epochs so the model settles
  on real sign geometry; `imgsz=960` matches the deployment input size.
- Fallback: if YOLO26 misbehaves anywhere in the toolchain, swap
  `model=yolo11n.pt` — nothing else changes.

**Accept when** val `mAP50 ≥ 0.85` and `recall ≥ 0.85` at conf 0.35.

## 4. Harvest classifier crops

```
python harvest_crops.py --src C:\datasets\lisa --src C:\datasets\mtsd ^
    --out C:\datasets\speed_limit_crops --min-px 24 --margin 0.15
```

Writes `train/{25,30,…,75,other}/` and `val/…` folders of 96×96 crops from
the ground-truth boxes (same group-aware split), skipping boxes under 24 px
(illegible), plus a capped sample of non-speed-sign crops as `other`. It
prints a per-class histogram — **look at it**. Target 150–300+ per value
class; expect 45/55/65/75 to be short from LISA alone.

## 5. Top up rare values (55/65/75 especially)

Mine your own dashcam footage with the freshly-trained detector:

```
python harvest_crops.py --video C:\clips\drive1.mp4 ^
    --detector C:\datasets\runs\detect\sl_det\weights\best.pt ^
    --out C:\datasets\mined --stride 5
```

Crops land in `mined\unsorted\` — manually sort the good ones into the
matching `speed_limit_crops\train\{value}\` folders (this is minutes of work
per clip, and night/rain crops from your own camera are the most valuable
training data you can add). Retrain the classifier after — it's cheap.

## 6. Train the value classifier

```
yolo classify train data=C:\datasets\speed_limit_crops model=yolo11n-cls.pt ^
    epochs=60 imgsz=96 batch=256 fliplr=0.0 name=sl_cls
```

Open `runs\classify\sl_cls\train_batch0.jpg` and confirm no crop is mirrored
(if your ultralytics version ignores `fliplr` for classify, disable flips
per its augmentation docs).

**Accept when** val top-1 ≥ 0.97 AND per-class recall ≥ 0.90 *including the
rare values* — check the confusion matrix, especially the 25↔35 cells (the
classic US failure pair).

## 7. Hand off

```
copy runs\detect\sl_det\weights\best.pt   speed_limit_detector.pt
copy runs\classify\sl_cls\weights\best.pt speed_limit_value.pt
```

Then follow `jetson/models/README.md`: scp both to the Jetson and build the
TensorRT engines on-device. For desktop testing against recorded clips,
point `VISION_SL_DETECTOR`/`VISION_SL_CLASSIFIER` at these `.pt` files
directly — no engines needed off-Jetson.
