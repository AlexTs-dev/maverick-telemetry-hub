"""
jetson/focus_assist.py
Maverick Telemetry Hub — lens focus and image-quality assistant

A live sharpness meter for setting a MANUAL lens, over SSH, with no display.

The IMX477 has no autofocus — there is no actuator in the sensor or the module,
so focus lives entirely in the lens barrel and the back-focus ring. Setting it
by eye on a phone-sized preview does not work at the precision that matters:
the difference between a sign the value classifier can read at 60m and one it
cannot is a fraction of a turn. So this prints a number, and you turn the ring
until the number peaks.

    sharp   1842.3  |################----|   97% of peak    luma 118  clip 0.4%

WHAT IT MEASURES. Variance of the Laplacian over a centre crop: the standard
focus metric, and the right one here because focus is the only thing changing
while you turn a ring. It is scale-sensitive (a high-contrast scene scores
higher than a flat one), which is why the display is dominated by "% of peak"
rather than the absolute figure — you are hunting a maximum, not hitting a
target value. The peak decays on a half-life so overshooting is recoverable
without restarting.

MEASURED AT NATIVE RESOLUTION, deliberately. The inference branch downscales to
1280x720, but measuring focus there would low-pass away exactly the high
frequencies being measured, and a badly focused lens would score respectably.
Focus is an optical property; measure it before any resampling.

MEASURED THROUGH THE RECORDER'S OWN ISP SETTINGS, also deliberately — it imports
csi_source_description() from recorder.py rather than rolling its own
nvarguscamerasrc. Edge enhancement alone can move this metric by a large factor,
so sharpness measured through a different pipeline is not the sharpness the
detector is handed.

EXACTLY ONE PROCESS MAY OWN THE CAMERA. This opens it directly, so
vision_publisher has to be stopped first; running anyway just fails to open the
device. The check is done up front so that failure is legible rather than a bare
"camera unavailable".

Usage (on the Jetson, from the jetson/ directory):

    sudo systemctl stop vision_publisher
    ./venv/bin/python focus_assist.py                  # live meter, centre crop
    ./venv/bin/python focus_assist.py --grid           # 3x3 map: tilt / decentring
    ./venv/bin/python focus_assist.py --save /tmp/foc  # also write JPEGs
    ./venv/bin/python focus_assist.py --list-modes     # Argus sensor modes
    sudo systemctl start vision_publisher

See CAMERA-TUNING.md for the procedure this tool is the instrument for.
"""

import argparse
import os
import subprocess
import sys
import time
from typing import Optional

import cv2
import numpy as np

# recorder.py owns the CSI source description; import it so this tool and the
# dashcam can never disagree about what the camera is doing. Same directory, so
# this works when run from jetson/ — which is also where the venv lives.
try:
    from recorder import RECORD_DEVICE, csi_source_description
except ImportError as e:  # pragma: no cover - operator error, not a code path
    sys.exit(f"Cannot import recorder.py ({e}).\n"
             f"Run this from the jetson/ directory: cd ~/maverick-telemetry-hub/jetson")

# Argus auto-exposure and auto-white-balance need roughly a second to converge.
# Frames before that are dark, magenta, and meaningless to a sharpness metric —
# reading them would show a "peak" that is really just AE settling.
WARMUP_FRAMES = 30

# Redraw rate for the live meter. Faster than this and the number is unreadable
# while your hand is on the lens; slower and it lags the ring.
REFRESH_S = 0.12

BAR_CELLS = 20


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian — higher is sharper.

    CV_64F, not the input dtype: on uint8 the Laplacian's negative lobes clip to
    zero, which throws away half the edge response and flattens the very peak
    this tool exists to find.
    """
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure_health(gray: np.ndarray) -> tuple[float, float, float]:
    """(mean luma, % blown highlights, % crushed shadows).

    Focus and exposure are separable faults that look identical in a thumbnail —
    an over-exposed frame has no contrast left to be sharp with, and would read
    as soft no matter how well the lens is set. Show both so they can be told
    apart without pulling a file off the device.
    """
    return (float(gray.mean()),
            float((gray >= 250).mean() * 100.0),
            float((gray <= 5).mean() * 100.0))


def centre_crop(gray: np.ndarray, fraction: float) -> np.ndarray:
    """The middle `fraction` of each axis.

    Focus is set for the road ahead, which is in the middle of the frame. The
    corners of a cheap wide M12 lens are soft no matter what the ring is doing,
    and including them just adds a constant that dilutes the peak.
    """
    h, w = gray.shape[:2]
    ch, cw = int(h * fraction), int(w * fraction)
    y, x = (h - ch) // 2, (w - cw) // 2
    return gray[y:y + ch, x:x + cw]


def grid_sharpness(gray: np.ndarray, n: int = 3) -> list[list[float]]:
    """Sharpness per cell of an n x n grid.

    Diagnostic for problems the centre crop cannot see: uniformly soft corners
    are the lens, one soft EDGE is a tilted sensor or a lens not seated square
    in its mount, and a soft centre with sharp edges means focus is set past
    infinity.
    """
    h, w = gray.shape[:2]
    return [[sharpness(gray[r * h // n:(r + 1) * h // n,
                            c * w // n:(c + 1) * w // n])
             for c in range(n)] for r in range(n)]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def _has_gstreamer() -> bool:
    """Parse the build info line rather than matching its exact column padding,
    which differs between the JetPack and pip builds."""
    for line in cv2.getBuildInformation().splitlines():
        if "GStreamer" in line:
            return "YES" in line
    return False


def csi_appsink(width: int, height: int, fps: int, tuned: bool) -> str:
    """The recorder's CSI source, terminated in an appsink cv2 can read.

    No width/height on the nvvidconv output caps: this must stay at capture
    resolution. Scaling here would be measuring the scaler, not the lens.
    """
    return (f"{csi_source_description(width, height, fps, tuned=tuned)} "
            f"! nvvidconv ! video/x-raw,format=BGRx "
            f"! videoconvert ! video/x-raw,format=BGR "
            f"! appsink drop=true max-buffers=1 sync=false")


def open_capture(args) -> tuple[cv2.VideoCapture, str]:
    """Returns (capture, human-readable description of what was opened)."""
    if args.source:
        if args.source.isdigit():
            # cv2.VideoCapture("0") opens a FILE named "0"; only the int form is
            # a camera index.
            return cv2.VideoCapture(int(args.source)), f"--source index {args.source}"
        backend = cv2.CAP_GSTREAMER if "!" in args.source else cv2.CAP_ANY
        return cv2.VideoCapture(args.source, backend), f"--source: {args.source}"

    if args.usb:
        cap = cv2.VideoCapture(args.device)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        return cap, f"USB/V4L2 {args.device} at {args.width}x{args.height}"

    if not _has_gstreamer():
        sys.exit("This OpenCV has no GStreamer support, so the CSI camera "
                 "cannot be opened.\nSee the OpenCV note in jetson/README.md "
                 "(Ubuntu's python3-opencv shadows NVIDIA's build), or pass "
                 "--usb for a V4L2 camera.")

    pipeline = csi_appsink(args.width, args.height, args.fps, not args.untuned)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    tuning = "untuned" if args.untuned else "recorder tuning"
    return cap, f"CSI at {args.width}x{args.height}@{args.fps} ({tuning})"


def camera_owner() -> Optional[str]:
    """The systemd unit currently holding the camera, if we can tell.

    Advisory only — `systemctl` may be absent on a dev box, and the operator may
    have started the publisher by hand. A clear message beats a mystery failure
    to open /dev/video0.
    """
    for unit in ("vision_publisher",):
        try:
            out = subprocess.run(["systemctl", "is-active", unit],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.stdout.strip() == "active":
            return unit
    return None


def list_sensor_modes() -> int:
    """Dump the modes Argus reports for the attached sensor.

    There is no API for this — nvarguscamerasrc prints the table to stderr as it
    starts. So start it, take one buffer, and echo what it said. Worth doing
    before pinning VISION_CSI_SENSOR_MODE: the modes differ between device trees
    and between IMX477 carrier boards, and a mode that does not exist fails the
    pipeline rather than falling back to a sensible one.
    """
    cmd = ["gst-launch-1.0", "nvarguscamerasrc", "num-buffers=1", "!", "fakesink"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        sys.exit("gst-launch-1.0 not found — this only works on the Jetson.")
    except subprocess.SubprocessError as e:
        sys.exit(f"Could not query sensor modes: {e}")

    text = (out.stderr or "") + (out.stdout or "")
    lines = [ln for ln in text.splitlines() if "GST_ARGUS" in ln]
    print("\n".join(lines) if lines else text.strip() or "(no output)")
    print("\nPin one with:  VISION_CSI_SENSOR_MODE=<n>  in ~/.maverick-env")
    return 0


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def bar(fraction: float, cells: int, glyphs: tuple[str, str]) -> str:
    filled = max(0, min(cells, int(round(fraction * cells))))
    return glyphs[0] * filled + glyphs[1] * (cells - filled)


def pick_glyphs() -> tuple[str, str]:
    """Block characters where the terminal can encode them, ASCII where not.

    A UnicodeEncodeError from a progress bar would be an absurd way to lose a
    focus session in a car park.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "█░".encode(encoding)
        return "█", "░"
    except (UnicodeEncodeError, LookupError):
        return "#", "-"


def verdict(luma: float, clipped: float, crushed: float) -> str:
    """One-line reading of the exposure, so a bad AE result is not mistaken for
    a focus problem."""
    # 15%, not 5%: this is measured on the centre crop, and a road scene with
    # bright sky in the top of that crop clips several percent perfectly
    # legitimately. A warning that fires on every sunny day gets ignored.
    if clipped > 15.0:
        return "highlights blown - AE over-exposing, sharpness reads low"
    if luma < 40.0:
        return "underexposed - raise VISION_CSI_EXPOSURE_MAX_US or _GAIN_MAX"
    if crushed > 40.0:
        return "very dark scene - metric unreliable, aim at something lit"
    return ""


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args) -> int:
    owner = camera_owner()
    if owner and not args.force:
        sys.exit(f"{owner} is running and owns the camera — exactly one process "
                 f"may hold it.\n\n    sudo systemctl stop {owner}\n\n"
                 f"...then re-run this, and start it again when you are done. "
                 f"(--force skips this check; the open will simply fail.)")

    cap, described = open_capture(args)
    if not cap.isOpened():
        sys.exit(f"Could not open the camera ({described}).\n"
                 f"Check the ribbon seating and `ls /dev/video*`; "
                 f"`--list-modes` will show whether Argus sees the sensor.")

    print(f"Source : {described}")
    print(f"ROI    : centre {args.roi:.0%} of frame"
          f"{'  + 3x3 grid' if args.grid else ''}")
    print(f"Warmup : {WARMUP_FRAMES} frames for AE/AWB to settle...")

    for _ in range(WARMUP_FRAMES):
        if not cap.read()[0]:
            cap.release()
            sys.exit("Camera opened but delivered no frames.")

    glyphs = pick_glyphs()
    interactive = sys.stdout.isatty()
    print("\nTurn the focus ring slowly. Chase the highest '% of peak'.")
    print("Ctrl-C when it stops improving.\n")

    peak = 0.0
    best = 0.0
    frames = 0
    saved = 0
    last_draw = 0.0
    last_grid = 0.0
    started = time.monotonic()
    # Per-frame decay factor giving the requested half-life, so a peak set by a
    # lucky frame (or by overshooting past the true maximum) fades instead of
    # pinning the display at 40% forever.
    decay = 0.5 ** (REFRESH_S / max(args.peak_halflife, 0.1))

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("\nCamera read failed — stopping.")
                break
            frames += 1

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            roi = centre_crop(gray, args.roi)
            value = sharpness(roi)
            luma, clipped, crushed = exposure_health(roi)

            best = max(best, value)
            peak = max(peak * decay, value)

            now = time.monotonic()
            if now - last_draw < REFRESH_S:
                continue
            last_draw = now

            fps = frames / max(now - started, 1e-6)
            ratio = value / peak if peak > 0 else 0.0
            line = (f"sharp {value:9.1f}  |{bar(ratio, BAR_CELLS, glyphs)}| "
                    f"{ratio:4.0%} of peak   luma {luma:5.1f}  "
                    f"clip {clipped:4.1f}%  {fps:4.1f} fps")
            hint = verdict(luma, clipped, crushed)
            if hint:
                line += f"   [{hint}]"

            if interactive and not args.grid:
                # Pad to clear the tail of a previously longer line.
                sys.stdout.write("\r" + line.ljust(120)[:120])
                sys.stdout.flush()
            else:
                print(line, flush=True)

            if args.grid and now - last_grid >= 1.0:
                last_grid = now
                cells = grid_sharpness(gray)
                peak_cell = max(max(row) for row in cells) or 1.0
                print("      grid % of best cell:")
                for row in cells:
                    print("        " + "  ".join(f"{c / peak_cell:5.0%}"
                                                 for c in row))

            if args.save and frames % max(args.save_every, 1) == 0:
                path = os.path.join(args.save, f"focus_{saved:04d}_{value:.0f}.jpg")
                cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                saved += 1

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()

    print("\n")
    print(f"Best sharpness seen : {best:.1f}")
    print(f"Frames              : {frames}")
    if args.save:
        print(f"Saved               : {saved} JPEG(s) in {args.save}")
    print("\nLock the back-focus grub screw WITHOUT letting the barrel turn, "
          "then re-run\nand confirm the reading held. Restart the publisher:"
          "\n\n    sudo systemctl start vision_publisher")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Live lens-focus and exposure meter for the Jetson camera.",
        epilog="Stop vision_publisher first — only one process may own the camera.")
    p.add_argument("--list-modes", action="store_true",
                   help="print the Argus sensor modes and exit")
    p.add_argument("--usb", action="store_true",
                   help="use a V4L2/USB camera instead of the CSI camera")
    p.add_argument("--device", default=RECORD_DEVICE,
                   help=f"V4L2 device for --usb (default {RECORD_DEVICE})")
    p.add_argument("--source",
                   help="explicit GStreamer pipeline or device index, verbatim")
    p.add_argument("--untuned", action="store_true",
                   help="CSI without the recorder's ISP tuning, for A/B comparison")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--roi", type=float, default=0.5,
                   help="centre fraction of each axis to measure (default 0.5)")
    p.add_argument("--grid", action="store_true",
                   help="also print a 3x3 sharpness map (tilt / decentring)")
    p.add_argument("--save", metavar="DIR",
                   help="write sample JPEGs here for later inspection")
    p.add_argument("--save-every", type=int, default=30,
                   help="save one frame in N when --save is set (default 30)")
    p.add_argument("--peak-halflife", type=float, default=20.0,
                   help="seconds for the held peak to halve (default 20)")
    p.add_argument("--force", action="store_true",
                   help="skip the vision_publisher ownership check")
    args = p.parse_args(argv)

    if args.list_modes:
        return list_sensor_modes()
    if not 0.05 <= args.roi <= 1.0:
        p.error("--roi must be between 0.05 and 1.0")
    if args.save:
        os.makedirs(args.save, exist_ok=True)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
