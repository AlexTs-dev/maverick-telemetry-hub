"""
jetson/training/harvest_crops.py
Maverick Telemetry Hub — build (and top up) the value-classifier crop dataset

GT mode — harvest 96x96 crops from ground-truth per-value boxes into the
ultralytics-classify folder layout (train/{25,...,75,other}/, val/...),
using the SAME clip-group split as remap_to_detector.py so no clip leaks
across train/val anywhere in the pipeline:

    python harvest_crops.py --src C:\\datasets\\lisa --src C:\\datasets\\mtsd \
        --out C:\\datasets\\speed_limit_crops --min-px 24 --margin 0.15

Mining mode — run a trained detector over dashcam video and save unsorted
crops for manual sorting into the class folders (the cheap way to top up
rare values and add night/rain examples):

    python harvest_crops.py --video C:\\clips\\drive1.mp4 \
        --detector best.pt --out C:\\datasets\\mined --stride 5
"""

import argparse
import hashlib
import random
import re
import sys
from collections import Counter
from pathlib import Path

import cv2
import yaml

SPEED_CLASS_PATTERNS = [
    re.compile(r"^speedLimit(\d+)$"),                            # LISA
    re.compile(r"^regulatory--maximum-speed-limit-(\d+)--g1$"),  # Mapillary MTSD
]

VALUES      = {str(v) for v in range(25, 80, 5)}   # real US postings: 25..75 by 5s
CROP_SIZE   = 96
VAL_PERCENT = 15                     # must match remap_to_detector.py
SPLIT_DIRS  = ("train", "valid", "val", "test")
IMG_EXTS    = {".jpg", ".jpeg", ".png", ".bmp"}


# --- duplicated from remap_to_detector.py on purpose: standalone scripts ---

def load_names(root: Path) -> dict:
    data = yaml.safe_load((root / "data.yaml").read_text(encoding="utf-8"))
    names = data["names"]
    if isinstance(names, dict):
        return {int(k): str(v) for k, v in names.items()}
    return {i: str(n) for i, n in enumerate(names)}


def group_key(stem: str) -> str:
    stem = re.split(r"_(?:jpg|jpeg|png|bmp)\.rf\.", stem, maxsplit=1)[0]
    return re.sub(r"[_\-]?\d+$", "", stem) or stem


def assign_split(group: str) -> str:
    h = int(hashlib.md5(group.encode("utf-8")).hexdigest(), 16)
    return "val" if (h % 100) < VAL_PERCENT else "train"

# ---------------------------------------------------------------------------


def value_map(names: dict) -> dict:
    """Class index -> value string ('25'..'75') for per-value speed classes."""
    out = {}
    for i, n in names.items():
        for p in SPEED_CLASS_PATTERNS:
            m = p.match(n)
            if m:
                out[i] = m.group(1)
    return out


def crop_norm_box(img, cx, cy, w, h, margin):
    """Crop a normalized YOLO box with margin. Returns (crop, min_side_px)."""
    ih, iw = img.shape[:2]
    bw, bh = w * iw, h * ih
    x1, y1 = (cx - w / 2) * iw, (cy - h / 2) * ih
    mx, my = bw * margin, bh * margin
    ax1 = max(0, int(x1 - mx))
    ay1 = max(0, int(y1 - my))
    ax2 = min(iw, int(x1 + bw + mx))
    ay2 = min(ih, int(y1 + bh + my))
    if ax2 <= ax1 or ay2 <= ay1:
        return None, 0.0
    return img[ay1:ay2, ax1:ax2], min(bw, bh)


def iter_images(root: Path):
    for split in SPLIT_DIRS:
        img_dir = root / split / "images"
        if not img_dir.is_dir():
            continue
        for img in sorted(img_dir.iterdir()):
            if img.suffix.lower() in IMG_EXTS:
                yield img, root / split / "labels" / (img.stem + ".txt")


def save_crop(crop, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dest), cv2.resize(crop, (CROP_SIZE, CROP_SIZE)))


def gt_mode(args) -> int:
    rng = random.Random(0)
    hist = Counter()
    skipped = Counter()
    split_groups = {"train": set(), "val": set()}
    other_candidates = {"train": [], "val": []}   # (si, img_path, parts)

    for si, src in enumerate(args.src):
        names = load_names(src)
        vmap = value_map(names)
        print(f"[{src}] per-value classes: {sorted(set(vmap.values()))}")

        for img_path, label in iter_images(src):
            if not label.exists():
                continue
            lines = [ln.split() for ln in label.read_text(encoding="utf-8").splitlines()
                     if len(ln.split()) >= 5]
            if not lines:
                continue
            group = f"s{si}:{group_key(img_path.stem)}"
            split = assign_split(group)
            split_groups[split].add(group)

            speed_lines = [p for p in lines if int(p[0]) in vmap]
            other_lines = [p for p in lines if int(p[0]) not in vmap]
            for p in other_lines:
                other_candidates[split].append((si, img_path, p))
            if not speed_lines:
                continue

            img = cv2.imread(str(img_path))
            if img is None:
                skipped["unreadable_image"] += 1
                continue
            for k, p in enumerate(speed_lines):
                value = vmap[int(p[0])]
                if value not in VALUES:
                    skipped[f"non_us_value_{value}"] += 1
                    continue
                crop, min_side = crop_norm_box(
                    img, *(float(v) for v in p[1:5]), args.margin)
                if crop is None or min_side < args.min_px:
                    skipped["below_min_px"] += 1
                    continue
                save_crop(crop, args.out / split / value
                          / f"s{si}_{img_path.stem}_{k}.jpg")
                hist[(split, value)] += 1

    # "other" crops — capped so stop signs don't drown the value classes
    val_cap = max(1, int(args.other_cap * VAL_PERCENT / (100 - VAL_PERCENT)))
    for split, cap in (("train", args.other_cap), ("val", val_cap)):
        picks = other_candidates[split]
        if len(picks) > cap:
            picks = rng.sample(picks, cap)
        for k, (si, img_path, p) in enumerate(picks):
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            crop, min_side = crop_norm_box(
                img, *(float(v) for v in p[1:5]), args.margin)
            if crop is None or min_side < args.min_px:
                continue
            save_crop(crop, args.out / split / "other"
                      / f"s{si}_{img_path.stem}_o{k}.jpg")
            hist[(split, "other")] += 1

    overlap = split_groups["train"] & split_groups["val"]
    assert not overlap, f"train/val share clip groups: {sorted(overlap)[:5]}"

    classes = sorted({c for _, c in hist})
    print(f"\nWrote {args.out}  (crops are {CROP_SIZE}x{CROP_SIZE})")
    print(f"{'class':>8}  {'train':>6}  {'val':>5}")
    for c in classes:
        print(f"{c:>8}  {hist[('train', c)]:>6}  {hist[('val', c)]:>5}")
    if skipped:
        print("skipped:", dict(skipped))
    print("train/val clip groups verified disjoint")
    print("\nTarget 150-300+ per value class — top up short classes via MTSD "
          "and mining mode (see training/README.md step 5).")
    return 0


def mining_mode(args) -> int:
    from ultralytics import YOLO   # lazy: GT mode must not require it
    model = YOLO(str(args.detector))
    out_dir = args.out / "unsorted"
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for video in args.video:
        cap = cv2.VideoCapture(str(video))
        if not cap.isOpened():
            print(f"WARNING: cannot open {video} — skipping")
            continue
        frame_idx, saved = 0, 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_idx % args.stride == 0:
                boxes = model.predict(frame, conf=0.35, verbose=False)[0].boxes
                for k in range(len(boxes) if boxes is not None else 0):
                    x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[k])
                    if min(x2 - x1, y2 - y1) < args.min_px:
                        continue
                    ih, iw = frame.shape[:2]
                    mx, my = (x2 - x1) * args.margin, (y2 - y1) * args.margin
                    crop = frame[max(0, int(y1 - my)):min(ih, int(y2 + my)),
                                 max(0, int(x1 - mx)):min(iw, int(x2 + mx))]
                    save_crop(crop, out_dir
                              / f"{Path(video).stem}_f{frame_idx:06d}_{k}.jpg")
                    saved += 1
            frame_idx += 1
        cap.release()
        print(f"[{video}] {saved} crops from {frame_idx} frames (stride {args.stride})")
        total += saved

    print(f"\n{total} crops in {out_dir} — sort the good ones into "
          "speed_limit_crops\\train\\{value}\\ by hand, then retrain the classifier.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", action="append", type=Path,
                    help="GT mode: YOLO-format dataset root (repeatable)")
    ap.add_argument("--video", action="append", type=Path,
                    help="mining mode: dashcam clip (repeatable)")
    ap.add_argument("--detector", type=Path,
                    help="mining mode: trained detector .pt")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--min-px", type=float, default=24,
                    help="skip boxes whose min side (pre-margin) is smaller")
    ap.add_argument("--margin", type=float, default=0.15)
    ap.add_argument("--stride", type=int, default=5,
                    help="mining mode: sample every Nth frame")
    ap.add_argument("--other-cap", type=int, default=500,
                    help="GT mode: max 'other' crops in train (val proportional)")
    args = ap.parse_args(argv)

    if args.video:
        if not args.detector:
            ap.error("--video (mining mode) requires --detector")
        return mining_mode(args)
    if args.src:
        return gt_mode(args)
    ap.error("give --src (GT mode) or --video --detector (mining mode)")


if __name__ == "__main__":
    sys.exit(main())
