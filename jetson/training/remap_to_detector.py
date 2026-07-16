"""
jetson/training/remap_to_detector.py
Maverick Telemetry Hub — build the single-class detector dataset

Merges one or more YOLO-format traffic-sign datasets (LISA Roboflow export,
Mapillary MTSD conversion, ...) into one detector dataset whose only class
is speed_limit_sign (class 0). Annotations matching a speed-limit class
pattern are remapped; all other annotations are dropped; a fraction of
images with no speed sign is kept as hard negatives (empty label files —
other white rectangular signs are exactly what the detector must learn to
ignore).

Train/val are split BY CLIP GROUP, never by frame: these datasets are
video-derived, and frame-level splits leak near-duplicates into val. The
split is hash-based and deterministic, so re-runs (and added sources) never
move an existing group between splits.

Usage:
    python remap_to_detector.py --src C:\\datasets\\lisa --src C:\\datasets\\mtsd \
        --out C:\\datasets\\speed_limit_det --keep-negatives 0.3
"""

import argparse
import hashlib
import random
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

SPEED_CLASS_PATTERNS = [
    re.compile(r"^speedLimit(\d+)$"),                            # LISA
    re.compile(r"^regulatory--maximum-speed-limit-(\d+)--g1$"),  # Mapillary MTSD
]

VAL_PERCENT = 15                       # of clip groups, not frames
SPLIT_DIRS  = ("train", "valid", "val", "test")   # Roboflow uses "valid"
IMG_EXTS    = {".jpg", ".jpeg", ".png", ".bmp"}


def load_names(root: Path) -> dict:
    """Class index -> name from data.yaml (list or dict form)."""
    data = yaml.safe_load((root / "data.yaml").read_text(encoding="utf-8"))
    names = data["names"]
    if isinstance(names, dict):
        return {int(k): str(v) for k, v in names.items()}
    return {i: str(n) for i, n in enumerate(names)}


def speed_class_ids(names: dict) -> set:
    return {i for i, n in names.items()
            if any(p.match(n) for p in SPEED_CLASS_PATTERNS)}


def group_key(stem: str, group_regex) -> str:
    """Clip-group identity for a frame filename."""
    if group_regex is not None:
        m = group_regex.match(stem)
        if m:
            return m.group(1)
        return stem
    # Default heuristic: strip Roboflow's "_jpg.rf.<hash>" mangling, then a
    # trailing frame counter ("clipA_0017" -> "clipA").
    stem = re.split(r"_(?:jpg|jpeg|png|bmp)\.rf\.", stem, maxsplit=1)[0]
    return re.sub(r"[_\-]?\d+$", "", stem) or stem


def assign_split(group: str) -> str:
    """Deterministic group -> split (stable across runs and added sources)."""
    h = int(hashlib.md5(group.encode("utf-8")).hexdigest(), 16)
    return "val" if (h % 100) < VAL_PERCENT else "train"


def iter_images(root: Path):
    for split in SPLIT_DIRS:
        img_dir = root / split / "images"
        if not img_dir.is_dir():
            continue
        for img in sorted(img_dir.iterdir()):
            if img.suffix.lower() in IMG_EXTS:
                yield img, root / split / "labels" / (img.stem + ".txt")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", action="append", required=True, type=Path,
                    help="YOLO-format dataset root (repeatable); must contain data.yaml")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--keep-negatives", type=float, default=0.3,
                    help="fraction of no-speed-sign images kept as hard negatives")
    ap.add_argument("--group-regex", type=str, default=None,
                    help="regex whose group(1) is the clip-group key (overrides the heuristic)")
    args = ap.parse_args(argv)

    group_regex = re.compile(args.group_regex) if args.group_regex else None
    rng = random.Random(0)             # reproducible negative sampling

    positives, negatives = [], []      # (src_idx, img_path, remapped_lines, group)
    for si, src in enumerate(args.src):
        names = load_names(src)
        speed_ids = speed_class_ids(names)
        if not speed_ids:
            print(f"WARNING: {src} has no speed-limit classes — contributes negatives only")
        print(f"[{src}] speed classes: {sorted(names[i] for i in speed_ids)}")

        for img, label in iter_images(src):
            kept = []
            if label.exists():
                for line in label.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and int(parts[0]) in speed_ids:
                        kept.append("0 " + " ".join(parts[1:5]))
            # Per-source group prefix: generic clip names must not merge
            # across datasets.
            group = f"s{si}:{group_key(img.stem, group_regex)}"
            (positives if kept else negatives).append((si, img, kept, group))

    kept_negatives = [n for n in negatives if rng.random() < args.keep_negatives]
    entries = positives + kept_negatives

    if not positives:
        print("ERROR: no speed-limit annotations found in any source")
        return 1

    split_groups = {"train": set(), "val": set()}
    counts = Counter()
    for split in ("train", "val"):
        (args.out / split / "images").mkdir(parents=True, exist_ok=True)
        (args.out / split / "labels").mkdir(parents=True, exist_ok=True)

    for si, img, kept, group in entries:
        split = assign_split(group)
        split_groups[split].add(group)
        out_name = f"s{si}_{img.name}"
        shutil.copy2(img, args.out / split / "images" / out_name)
        (args.out / split / "labels" / (Path(out_name).stem + ".txt")).write_text(
            "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        counts[f"{split}_images"] += 1
        counts[f"{split}_instances"] += len(kept)
        if not kept:
            counts[f"{split}_negatives"] += 1

    # The whole point of group splitting — fail loudly if it ever breaks.
    overlap = split_groups["train"] & split_groups["val"]
    assert not overlap, f"train/val share clip groups: {sorted(overlap)[:5]}"

    (args.out / "dataset.yaml").write_text(
        f"path: {args.out.resolve().as_posix()}\n"
        "train: train/images\n"
        "val: val/images\n"
        "nc: 1\n"
        "names:\n"
        "  - speed_limit_sign\n",
        encoding="utf-8")

    print(f"\nWrote {args.out}")
    for split in ("train", "val"):
        print(f"  {split}: {counts[f'{split}_images']} images "
              f"({counts[f'{split}_instances']} sign instances, "
              f"{counts[f'{split}_negatives']} negatives), "
              f"{len(split_groups[split])} clip groups")
    print(f"  negatives kept: {len(kept_negatives)}/{len(negatives)} "
          f"(--keep-negatives {args.keep_negatives})")
    print("  train/val clip groups verified disjoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
