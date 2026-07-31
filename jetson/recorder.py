"""
jetson/recorder.py
Maverick Telemetry Hub — Jetson dashcam recorder

Owns the camera and the GStreamer pipeline. One source is tee'd two ways:

    camera ─┬─ (encode if needed) ─ h264parse ─ splitmuxsink   (footage on disk)
            └─ (decode/convert)   ─ appsink                    (frames for YOLO)

Driven through PyGObject rather than cv2.VideoCapture because only the native
API exposes what this needs: splitmuxsink's `format-location-full` (so every
segment gets an exact wall-clock filename) and `split-now` (so a segment can be
cut at a trip boundary or the instant a crash is detected). cv2's GStreamer
backend gives neither.

WHY THE TEE, AND NOT A SECOND CAPTURE: a second cv2.VideoCapture on the same
/dev/video* fails on essentially every V4L2 device. Exactly one process may own
the camera, so recording and inference must come off one pipeline.

NO HARDWARE ENCODER ON THE ORIN NANO: verified on the device — NVENC is absent
from this SKU (NVDEC is present). H.264 therefore comes from the camera itself
where possible, and from the CPU otherwise. See _profiles() for the measured
numbers behind the ladder.

WHY FRAGMENTED MP4: power is ignition-switched, so an ungraceful cut mid-segment
is the normal way this process dies. A plain MP4 loses its moov atom and is
unrecoverable; with ~1s fragments the truncated file still plays up to its last
complete fragment.

FAILURE IS NOT FATAL. Every entry point returns rather than raises. If gi,
GStreamer or NVENC is missing, or no profile reaches PLAYING, start() returns
False and camera.py falls back to cv2.VideoCapture with recording off. A dashcam
that cannot record is degraded; a Jetson that no longer detects speed limit
signs is broken.

Config (all env, all optional):
    VISION_RECORD_ENABLED   0/1   master switch (default 0 — opt in per device)
    MAVERICK_DASHCAM_ROOT         clip root (default /var/lib/maverick-dashcam)
    VISION_RECORD_DEVICE          V4L2 device (default /dev/video0)
    VISION_RECORD_SOURCE          auto | csi | usb (default auto)
    VISION_CSI_FLIP               nvvidconv flip-method for CSI (default 0=none,
                                  2=180deg for an inverted camera mount)
    VISION_RECORD_WIDTH/HEIGHT/FPS/BITRATE
    VISION_RECORD_SEGMENT_S       segment length, seconds (default 60)
    MAVERICK_DASHCAM_RETENTION_DAYS / _MAX_BYTES / _MIN_FREE_BYTES

CSI image tuning (Argus only — USB profiles are untouched by all of these).
See CAMERA-TUNING.md for what to change and how to tell whether it helped:
    VISION_CSI_TUNING             0/1 master switch (default 1)
    VISION_CSI_EXPOSURE_MAX_US    shutter cap, microseconds (default 4000 = 1/250s)
    VISION_CSI_EXPOSURE_MIN_US    default 13
    VISION_CSI_GAIN_MAX           analog gain ceiling (default 16)
    VISION_CSI_ISP_GAIN_MAX       digital gain ceiling (default 8)
    VISION_CSI_TNR_MODE           temporal noise reduction (default 0 = off)
    VISION_CSI_EE_MODE            edge enhancement (default 0 = off)
    VISION_CSI_AEANTIBANDING      default 0 = off; see the note by the constants
    VISION_CSI_SENSOR_MODE        -1 = let Argus choose (default)
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import numpy as np

from clipstore import Clip, ClipStore, parse_iso, utc_now

log = logging.getLogger("recorder")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


RECORD_ENABLED = _env_flag("VISION_RECORD_ENABLED", "0")
DASHCAM_ROOT   = os.environ.get("MAVERICK_DASHCAM_ROOT", "/var/lib/maverick-dashcam")

RECORD_DEVICE  = os.environ.get("VISION_RECORD_DEVICE", "/dev/video0")
RECORD_SOURCE  = os.environ.get("VISION_RECORD_SOURCE", "auto").strip().lower()
# nvvidconv flip-method, applied to the CSI source BEFORE the tee so recording
# and inference are corrected together. A camera mounted upside down otherwise
# feeds the sign detector 180deg-rotated frames — far out of its training
# distribution, which shows up as confident nonsense rather than as no output.
CSI_FLIP       = _env_int("VISION_CSI_FLIP", 0)
RECORD_WIDTH   = _env_int("VISION_RECORD_WIDTH", 1920)
RECORD_HEIGHT  = _env_int("VISION_RECORD_HEIGHT", 1080)
RECORD_FPS     = _env_int("VISION_RECORD_FPS", 30)
RECORD_BITRATE = _env_int("VISION_RECORD_BITRATE", 8_000_000)
SEGMENT_S      = _env_int("VISION_RECORD_SEGMENT_S", 60)

# --- CSI (Argus) image tuning ----------------------------------------------
#
# An unconfigured nvarguscamerasrc is wrong for a dashcam. Its defaults suit a
# camera sitting still in a room: auto-exposure will pick 1/30s in overcast
# light or a tunnel, and temporal noise reduction blends consecutive frames.
# Both look fine on a static scene and both smear a moving one — so the sign
# detector gets unreadable frames at exactly the moment a sign passes, which is
# the only moment that matters. See CAMERA-TUNING.md.
#
# Capping exposure is a REAL TRADE, not a free win: a shorter shutter costs
# noise, because AE compensates with gain. Taken deliberately — a noisy sharp
# frame can be read, a clean smeared one cannot.
#
# aeantibanding is OFF for a subtler reason. Antibanding quantises exposure to
# multiples of the mains period (1/100s or 1/120s) so fluorescent lighting does
# not band, which would put an 8.3ms floor under a 4ms cap and silently undo it.
# There is no mains lighting on a road.
CSI_TUNING          = _env_flag("VISION_CSI_TUNING", "1")
CSI_EXPOSURE_MIN_US = _env_int("VISION_CSI_EXPOSURE_MIN_US", 13)    # sensor floor
CSI_EXPOSURE_MAX_US = _env_int("VISION_CSI_EXPOSURE_MAX_US", 4000)  # 1/250 s
CSI_GAIN_MAX        = _env_int("VISION_CSI_GAIN_MAX", 16)           # analog, sensor
CSI_ISP_GAIN_MAX    = _env_int("VISION_CSI_ISP_GAIN_MAX", 8)        # digital, brightens noise
CSI_TNR_MODE        = _env_int("VISION_CSI_TNR_MODE", 0)            # 0 off, 1 fast, 2 quality
CSI_EE_MODE         = _env_int("VISION_CSI_EE_MODE", 0)             # edge enhance, same scale
CSI_AEANTIBANDING   = _env_int("VISION_CSI_AEANTIBANDING", 0)       # 0 off, 1 auto, 2 50Hz, 3 60Hz
CSI_SENSOR_MODE     = _env_int("VISION_CSI_SENSOR_MODE", -1)        # -1 = let Argus choose

# Frames handed to the classifier. Downscaled here so the YOLO input is
# unchanged by recording at 1080p — inference cost must not rise.
INFER_WIDTH  = _env_int("VISION_RECORD_INFER_WIDTH", 1280)
INFER_HEIGHT = _env_int("VISION_RECORD_INFER_HEIGHT", 720)

# Pixels on target: CROP instead of downscale.
#
# The scale above throws away half the linear resolution before YOLO ever sees
# a frame, and YOLO then letterboxes to its own imgsz (960) — so a sign loses
# 1920 -> 1280 -> 960, a factor of 2. That compounds with a hard floor:
# speed_limit_model rejects any box under VISION_SL_MIN_BOX_PX (24) measured in
# THIS frame's coordinates, so a distant sign is not merely harder to read, it
# is discarded before the value classifier runs.
#
# Cropping at native resolution instead costs nothing: a 960x960 centre crop
# fed to a detector running imgsz=960 is resampled zero times, so a sign keeps
# every sensor pixel it started with — roughly double the current linear size.
# The price is field of view, and it is a real price: half the width. Distant
# signs sit near the vanishing point, which is why this is the right trade for
# reading them early, but a sign at the frame edge is now outside the crop.
#
# OFF by default. Landing this code on a Jetson must not silently change what
# the detector sees; opt in per device via ~/.maverick-env, and verify against
# real drives rather than a parked car.
INFER_CROP     = _env_flag("VISION_INFER_CROP", "0")
INFER_CROP_W   = _env_int("VISION_INFER_CROP_W", 960)
INFER_CROP_H   = _env_int("VISION_INFER_CROP_H", 960)
# Signs sit above the road surface, so the useful crop is usually not vertically
# centred. Positive moves the window DOWN, matching image coordinates.
INFER_CROP_Y   = _env_int("VISION_INFER_CROP_Y", 0)

# Sized against the measured disk: the Orin Nano devkit here has a 937 GiB NVMe
# with ~880 GiB free. At 8 Mbps (~3.6 GB/hour) a 400 GiB budget holds roughly
# 110 hours, so for any realistic amount of driving the 30-DAY AGE LIMIT is what
# actually expires footage — which is what "keep a month" is supposed to mean.
# The byte budget is a backstop against an unusually heavy month, not the
# primary policy.
RETENTION_DAYS = float(os.environ.get("MAVERICK_DASHCAM_RETENTION_DAYS", "30"))
MAX_BYTES      = _env_int("MAVERICK_DASHCAM_MAX_BYTES", 400 * 2**30)      # 400 GiB
MIN_FREE_BYTES = _env_int("MAVERICK_DASHCAM_MIN_FREE_BYTES", 40 * 2**30)  # 40 GiB

# splitmuxsink with async-finalize=true hands the closed fragment to a muxer
# that finishes writing on its own thread, so the file is not complete the
# instant the next segment starts. Settle before stat()ing it.
FINALIZE_SETTLE_S = 3.0
PRUNE_INTERVAL_S  = 300.0
PULL_TIMEOUT_NS   = 100 * 1_000_000  # 100ms

# ---------------------------------------------------------------------------
# GStreamer import — soft, so a box without PyGObject still runs inference
# ---------------------------------------------------------------------------

Gst = None
_GST_IMPORT_ERROR: Optional[str] = None


def _load_gst() -> bool:
    global Gst, _GST_IMPORT_ERROR
    if Gst is not None:
        return True
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst as _Gst
        _Gst.init(None)
        Gst = _Gst
        return True
    except (ImportError, ValueError, AttributeError) as e:
        _GST_IMPORT_ERROR = str(e)
        return False


def _has_element(name: str) -> bool:
    if not _load_gst():
        return False
    factory = Gst.ElementFactory.find(name)
    return factory is not None

# ---------------------------------------------------------------------------
# Pipeline profiles
#
# THIS BOARD HAS NO HARDWARE VIDEO ENCODER. Verified on the device: the
# nvvideo4linux2 plugin registers exactly one element (nvv4l2decoder), and
# there is no /dev/nvhost-msenc. NVENC is absent from the Jetson Orin Nano SKU
# (p3767-0005) -- it has NVDEC for decode only. Anything that needs H.264 out
# must get it from the camera or from the CPU.
#
# Measured on this board, 15W mode, 10s of 1080p30 from videotestsrc, idle:
#     camera H.264 passthrough    no encode at all   <- costs nothing
#     openh264enc  1080p30        1.5x realtime
#     x264enc      1080p30        0.65x realtime     <- CANNOT keep up
#     x264enc       720p30        1.5x realtime
#     x264enc      1080p15        1.3x realtime
#
# So the ladder prefers a camera that encodes H.264 onboard (most UVC webcams
# do). That path re-encodes nothing, leaves the CPU free, and lets the inference
# branch use the hardware DECODER, which this board does have. Everything below
# it is a software encode competing with YOLO for a 6-core CPU in a hot cab.
#
# Tried in order; the first to reach PLAYING wins. Falling down the ladder is
# normal, not an error — a USB2 webcam cannot carry raw 1080p30 (it exceeds the
# bus), so it offers MJPG or H.264 instead.
# ---------------------------------------------------------------------------

def _encoder_chain() -> Optional[str]:
    """Raw video in, H.264 out. None if nothing can encode."""
    if _has_element("nvv4l2h264enc"):
        # Not present on the Orin Nano, but the Orin NX/AGX and future SKUs do
        # have NVENC — take it if it is ever there.
        return (f"nvvidconv ! video/x-raw(memory:NVMM),format=NV12 "
                f"! nvv4l2h264enc bitrate={RECORD_BITRATE} iframeinterval={RECORD_FPS} "
                f"idrinterval={RECORD_FPS} insert-sps-pps=true maxperf-enable=true")
    if _has_element("openh264enc"):
        # Measured ~2.3x faster than x264 ultrafast at 1080p30 on this board,
        # and the only software encoder that keeps up at full resolution.
        return (f"videoconvert ! openh264enc bitrate={RECORD_BITRATE} "
                f"gop-size={RECORD_FPS}")
    if _has_element("x264enc"):
        # bitrate is kbit/s here, unlike openh264enc's bits/s.
        return (f"videoconvert ! x264enc bitrate={RECORD_BITRATE // 1000} "
                f"speed-preset=ultrafast tune=zerolatency key-int-max={RECORD_FPS}")
    return None


def _record_tail() -> str:
    """h264 in, segmented fragmented-MP4 files out.

    splitmuxsink is configured HERE, in the pipeline description, not afterwards
    from Python. Setting muxer-factory/muxer-properties with set_property() after
    Gst.parse_launch() silently does nothing: splitmuxsink builds its muxer at
    construction (async-finalize defaults to false), so a later switch never
    reaches the muxer. The property reads back correctly, which is what makes the
    failure silent — verified on the device, the output had zero moof atoms and
    an unplayable file after a power cut. Configure it inline instead.

    fragment-duration=1000 is what makes the file survive an ignition cut: each
    ~1s of video is flushed as a complete moof+mdat, so a truncated file plays up
    to its last fragment. Without it the file is ftyp+free+mdat with no moov and
    nothing can read it.
    """
    return (
        f"h264parse config-interval=-1 "
        f"! splitmuxsink name=splitmux "
        f"async-finalize=true muxer-factory=mp4mux "
        f'muxer-properties="properties,fragment-duration=1000" '
        f"max-size-time={SEGMENT_S * 1_000_000_000} max-size-bytes=0"
    )


def infer_crop_rect() -> Optional[tuple[int, int, int, int]]:
    """(x0, y0, x1, y1) of the native-resolution inference crop, or None.

    Public so focus_assist/focus_server can show the operator the exact region
    the detector will see — aiming a camera at a window you cannot see is how
    signs end up just outside it.

    Clamped rather than validated: a crop wider than the sensor is an operator
    typo, and silently using the whole width beats failing the pipeline and
    losing both inference and footage.
    """
    if not INFER_CROP:
        return None
    # Even origin AND even size: chroma is subsampled 2x in I420/NV12, so an
    # odd value in either makes nvvidconv refuse the crop or shift the colour
    # planes against the luma.
    cw = max(2, min(INFER_CROP_W, RECORD_WIDTH)) & ~1
    ch = max(2, min(INFER_CROP_H, RECORD_HEIGHT)) & ~1
    x0 = ((RECORD_WIDTH - cw) // 2) & ~1
    y0 = (RECORD_HEIGHT - ch) // 2 + INFER_CROP_Y
    y0 = max(0, min(y0, RECORD_HEIGHT - ch)) & ~1
    return x0, y0, x0 + cw, y0 + ch


def _infer_tail(nvmm: bool, decode: str = "") -> str:
    """Native-resolution crop, or downscaled BGR, into the appsink.

    leaky=downstream plus drop=true/max-buffers=1 is what makes a slow YOLO pass
    unable to stall recording: this branch throws frames away rather than
    applying backpressure through the tee.
    """
    prefix = f"{decode} ! " if decode else ""
    rect = infer_crop_rect()

    if nvmm and _has_element("nvvidconv"):
        if rect:
            x0, y0, x1, y1 = rect
            # nvvidconv's left/right/top/bottom are a source RECTANGLE, not
            # counts of pixels to remove — passing counts fails negotiation.
            # It then scales that rectangle to the output caps, so the caps MUST
            # restate the crop size; anything else silently re-introduces the
            # resampling this exists to avoid.
            scale = (f"nvvidconv left={x0} right={x1} top={y0} bottom={y1} "
                     f"! video/x-raw,format=BGRx,"
                     f"width={x1 - x0},height={y1 - y0} ! videoconvert "
                     f"! video/x-raw,format=BGR")
        else:
            scale = (f"nvvidconv ! video/x-raw,format=BGRx,"
                     f"width={INFER_WIDTH},height={INFER_HEIGHT} ! videoconvert "
                     f"! video/x-raw,format=BGR")
    elif rect:
        x0, y0, x1, y1 = rect
        # videocrop takes COUNTS, the opposite convention to nvvidconv above.
        scale = (f"videoconvert ! videocrop left={x0} right={RECORD_WIDTH - x1} "
                 f"top={y0} bottom={RECORD_HEIGHT - y1} "
                 f"! video/x-raw,format=BGR")
    else:
        scale = (f"videoconvert ! videoscale ! video/x-raw,format=BGR,"
                 f"width={INFER_WIDTH},height={INFER_HEIGHT}")

    return (f"queue max-size-buffers=2 leaky=downstream ! {prefix}{scale} "
            f"! appsink name=infersink drop=true max-buffers=1 sync=false")


def _csi_tuning_props() -> str:
    """nvarguscamerasrc properties for a camera bolted to a moving vehicle.

    Exposure is nanoseconds at the element but microseconds in the env vars: the
    useful range here is 13µs–4ms, and nine trailing zeros is an excellent way to
    ship a 4-second shutter by typo.

    TNR and edge enhancement are both off. They are ISP post-processing that
    trades real detail for a nicer-looking picture — TNR by blending across
    frames (ghosting anything that moves), EE by drawing halos at edges the
    optics did not actually resolve. A detector wants the optical truth; a human
    watching footage back may prefer otherwise, hence both are env-tunable.
    """
    props = [
        f'exposuretimerange="{CSI_EXPOSURE_MIN_US * 1000} {CSI_EXPOSURE_MAX_US * 1000}"',
        f'gainrange="1 {CSI_GAIN_MAX}"',
        f'ispdigitalgainrange="1 {CSI_ISP_GAIN_MAX}"',
        f"tnr-mode={CSI_TNR_MODE}",
        f"ee-mode={CSI_EE_MODE}",
        f"aeantibanding={CSI_AEANTIBANDING}",
    ]
    if CSI_SENSOR_MODE >= 0:
        # Left unset, Argus picks a mode to satisfy the requested caps and may
        # choose a binned or cropped readout without saying so — same resolution
        # out, different sharpness and a different field of view. Pin it once
        # you have compared them; `focus_assist.py --list-modes` lists them.
        props.append(f"sensor-mode={CSI_SENSOR_MODE}")
    return " ".join(props)


def csi_source_description(width: int, height: int, fps: int, tuned: bool = True) -> str:
    """The CSI source chain, up to but not including the tee.

    Public on purpose: focus_assist.py measures focus through the same ISP
    settings the recorder runs with, because sharpness measured through a
    different pipeline is not the sharpness the detector will be handed.
    """
    props = f" {_csi_tuning_props()}" if tuned and CSI_TUNING else ""
    src = (f"nvarguscamerasrc{props} ! video/x-raw(memory:NVMM),width={width},"
           f"height={height},framerate={fps}/1,format=NV12")
    if CSI_FLIP:
        # Stays in NVMM, so the profile's nvmm=True contract still holds.
        src += (f" ! nvvidconv flip-method={CSI_FLIP} "
                f"! video/x-raw(memory:NVMM),format=NV12")
    return src


def _profiles() -> list[tuple[str, str]]:
    """[(name, complete pipeline description), ...] in preference order."""
    w, h, fps, dev = RECORD_WIDTH, RECORD_HEIGHT, RECORD_FPS, RECORD_DEVICE
    encoder = _encoder_chain()
    candidates: list[tuple[str, str]] = []

    def raw_profile(name: str, source: str, nvmm: bool):
        if encoder is None:
            return
        # An NVMM source cannot feed a CPU encoder directly: nvarguscamerasrc and
        # nvv4l2decoder hand out video/x-raw(memory:NVMM), and videoconvert will
        # not negotiate with NVMM caps — the pipeline fails to link at build time
        # ("could not link videoconvert to openh264enc"), taking the csi and
        # usb-mjpg-hw profiles down with it. nvvidconv is the element that copies
        # buffers out of NVMM into system memory. The NVENC chain already starts
        # with nvvidconv and stays in NVMM, so it needs no help.
        enc = encoder
        if nvmm and encoder.startswith("videoconvert"):
            enc = f"nvvidconv ! video/x-raw,format=I420 ! {encoder}"
        candidates.append((name, (
            f"{source} ! tee name=t "
            f"t. ! queue max-size-buffers=8 ! {enc} ! {_record_tail()} "
            f"t. ! {_infer_tail(nvmm)}"
        )))

    def h264_profile(name: str, source: str):
        # The camera already produced H.264: record it verbatim and use the
        # hardware decoder to get frames for inference. No encode anywhere.
        decode = "nvv4l2decoder" if _has_element("nvv4l2decoder") else "avdec_h264"
        candidates.append((name, (
            f"{source} ! h264parse ! tee name=t "
            f"t. ! queue max-size-buffers=8 ! {_record_tail()} "
            f"t. ! {_infer_tail(_has_element('nvv4l2decoder'), decode=decode)}"
        )))

    usb_h264 = f"v4l2src device={dev} io-mode=2 ! video/x-h264,width={w},height={h},framerate={fps}/1"
    usb_mjpg = f"v4l2src device={dev} io-mode=2 ! image/jpeg,width={w},height={h},framerate={fps}/1"
    usb_raw  = f"v4l2src device={dev} io-mode=2 ! video/x-raw,width={w},height={h},framerate={fps}/1"
    csi         = csi_source_description(w, h, fps)
    csi_untuned = csi_source_description(w, h, fps, tuned=False)

    def csi_profiles():
        """Two rungs, not one.

        Argus rejects a property it cannot honour — an exposure range outside
        what the chosen sensor mode supports, a sensor-mode absent from this
        device tree — by failing the pipeline at PLAYING, and with
        VISION_RECORD_SOURCE=csi that is the only candidate. Tuning improves the
        picture; recording at all is the feature. So untuned CSI sits directly
        underneath, and a bad env value costs image quality rather than footage.
        """
        raw_profile("csi", csi, True)
        if csi_untuned != csi:
            raw_profile("csi-untuned", csi_untuned, True)

    if RECORD_SOURCE == "test":
        raw_profile("test", f"videotestsrc is-live=true ! video/x-raw,width={w},height={h},"
                            f"framerate={fps}/1", False)
        return candidates
    if RECORD_SOURCE == "csi":
        csi_profiles()
        return candidates

    # Cheapest first.
    h264_profile("usb-h264", usb_h264)
    if _has_element("nvv4l2decoder"):
        raw_profile("usb-mjpg-hw", f"{usb_mjpg} ! jpegparse ! nvv4l2decoder mjpeg=1", True)
    if _has_element("jpegdec"):
        raw_profile("usb-mjpg-sw", f"{usb_mjpg} ! jpegdec", False)
    raw_profile("usb-raw", usb_raw, False)
    if RECORD_SOURCE != "usb":
        csi_profiles()
    return candidates

# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class Recorder:
    """Continuous segmented recording plus a frame tap for inference.

    Thread safety: splitmuxsink signals fire on GStreamer streaming threads,
    while tick()/read_frame() are called from the publisher's main loop, so all
    shared segment state is under _lock.
    """

    def __init__(self, on_clip_closed: Optional[Callable[[Clip], None]] = None,
                 on_pruned: Optional[Callable[[list, str], None]] = None):
        self.store = ClipStore(DASHCAM_ROOT)
        self._on_clip_closed = on_clip_closed
        self._on_pruned = on_pruned

        self._lock = threading.Lock()
        self._pipeline = None
        self._appsink = None
        self._splitmux = None
        self._bus = None

        self._profile: Optional[str] = None
        self._current: Optional[dict] = None   # segment being written
        self._pending: list[dict] = []         # closed, awaiting finalize
        self._failed = False
        self._last_prune = 0.0
        self._storage_pressure = False
        self._segments_written = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        if not RECORD_ENABLED:
            log.info("Recording disabled (VISION_RECORD_ENABLED=0)")
            return False
        if not _load_gst():
            log.warning(f"GStreamer/PyGObject unavailable — recording disabled: {_GST_IMPORT_ERROR}")
            return False

        try:
            self.store.ensure_dirs()
        except OSError as e:
            log.error(f"Cannot use clip root {DASHCAM_ROOT} — recording disabled: {e}")
            return False

        # Reconcile protection before writing anything: a protect that arrived
        # while a clip was still recording only takes effect here.
        try:
            self.store.collect_orphans()
            self.store.apply_windows()
        except OSError as e:
            log.warning(f"Clip store reconcile failed: {e}")

        profiles = _profiles()
        if not profiles:
            log.error("No usable H.264 path — the camera does not supply H.264 and no "
                      "encoder element is installed. Recording disabled.")
            return False

        for name, description in profiles:
            if self._try_profile(name, description):
                self._profile = name
                log.info(f"Recording started — profile={name} {RECORD_WIDTH}x{RECORD_HEIGHT}@"
                         f"{RECORD_FPS} {RECORD_BITRATE // 1000}kbps segments={SEGMENT_S}s")
                # The inference window decides what the detector can possibly
                # see. Log it either way: "the sign was outside the crop" and
                # "the sign was too small" look identical from the outside.
                rect = infer_crop_rect()
                if rect:
                    x0, y0, x1, y1 = rect
                    log.info(f"Inference window: native crop {x1 - x0}x{y1 - y0} "
                             f"at ({x0},{y0}) — no rescale, "
                             f"{100 * (x1 - x0) // RECORD_WIDTH}% of frame width")
                else:
                    log.info(f"Inference window: full frame scaled to "
                             f"{INFER_WIDTH}x{INFER_HEIGHT}")
                if name != "usb-h264" and not _has_element("nvv4l2h264enc"):
                    # Worth saying out loud: this board has no NVENC, so every
                    # frame is being encoded on the same CPU that runs
                    # everything else. A camera with onboard H.264 removes it.
                    log.warning("Encoding in SOFTWARE — this Jetson has no hardware encoder. "
                                "A camera with onboard H.264 would remove this cost entirely.")
                return True

        log.warning("No recording profile could start — recording disabled")
        return False

    def _try_profile(self, name: str, description: str) -> bool:
        pipeline = None
        try:
            log.debug(f"Trying profile {name}: {description}")
            pipeline = Gst.parse_launch(description)
            splitmux = pipeline.get_by_name("splitmux")
            appsink = pipeline.get_by_name("infersink")
            if splitmux is None or appsink is None:
                raise RuntimeError("pipeline missing splitmux/infersink")

            self._configure_splitmux(splitmux)
            splitmux.connect("format-location-full", self._on_format_location)

            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("set_state(PLAYING) failed")

            # PLAYING is async; wait for it to actually settle so a profile that
            # cannot negotiate caps is rejected here rather than looking healthy.
            change, state, _ = pipeline.get_state(5 * Gst.SECOND)
            if change != Gst.StateChangeReturn.SUCCESS or state != Gst.State.PLAYING:
                raise RuntimeError(f"did not reach PLAYING (got {state.value_nick})")

            self._pipeline = pipeline
            self._splitmux = splitmux
            self._appsink = appsink
            self._bus = pipeline.get_bus()
            self._failed = False
            return True
        except Exception as e:
            log.info(f"Profile {name} unusable: {e}")
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            return False

    def _configure_splitmux(self, splitmux) -> None:
        """Only what is safe to set after construction.

        Everything that affects the MUXER (async-finalize, muxer-factory,
        muxer-properties) is set inline in _record_tail() — see the note there.
        Setting it here would be silently ignored.
        """
        # Fallback only; format-location-full supplies the real names.
        splitmux.set_property("location", str(self.store.clips_dir / "segment%05d.mp4"))

        # Assert the inline config actually landed. A power cut costing the whole
        # in-flight segment is the exact failure this guards, and it is otherwise
        # invisible until someone tries to play a clip after a crash.
        props = splitmux.get_property("muxer-properties")
        fragmented = props is not None and "fragment-duration" in props.to_string()
        if not fragmented:
            log.error("Fragmented MP4 is NOT active — an ungraceful power cut will "
                      "lose the entire in-flight segment. Check the splitmuxsink "
                      "configuration in _record_tail().")
        elif not splitmux.get_property("async-finalize"):
            log.warning("async-finalize is off — muxer-properties are ignored in this mode")

    def stop(self) -> None:
        """EOS, wait for the muxer to finalize, then tear down. Must complete
        inside the unit's TimeoutStopSec (10s) or systemd SIGKILLs us and the
        final segment is truncated."""
        pipeline = self._pipeline
        if pipeline is None:
            return
        try:
            pipeline.send_event(Gst.Event.new_eos())
            bus = pipeline.get_bus()
            if bus is not None:
                bus.timed_pop_filtered(
                    5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        except Exception as e:
            log.warning(f"Error during EOS: {e}")
        finally:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            self._pipeline = None
            self._appsink = None
            self._splitmux = None

        self._close_current()
        self._finalize_pending(force=True)
        log.info("Recording stopped")

    # -- segment bookkeeping ----------------------------------------------

    def _on_format_location(self, _splitmux, _fragment_id, _first_sample) -> str:
        """splitmuxsink asking where the NEXT segment goes. Fires exactly at the
        boundary, so wall-clock now is the new segment's start — and unlike
        deriving it from pipeline running-time, it stays correct across a chrony
        step (the Orin has no RTC and gets stepped hard after boot)."""
        now = utc_now()
        try:
            clip_id, rel_path, abs_path = self.store.allocate(now)
        except OSError as e:
            log.error(f"Cannot allocate segment path: {e}")
            return str(self.store.clips_dir / f"fallback{int(time.time())}.mp4")

        with self._lock:
            if self._current is not None:
                self._current["closed_at"] = now
                self._current["settle_at"] = time.monotonic() + FINALIZE_SETTLE_S
                self._pending.append(self._current)
            self._current = {
                "clip_id":    clip_id,
                "rel_path":   rel_path,
                "abs_path":   abs_path,
                "started_at": now,
            }
        return str(abs_path)

    def _close_current(self) -> None:
        with self._lock:
            if self._current is None:
                return
            self._current["closed_at"] = utc_now()
            self._current["settle_at"] = 0.0
            self._pending.append(self._current)
            self._current = None

    def _finalize_pending(self, force: bool = False) -> None:
        """Write sidecars and announce clips whose files have settled."""
        now = time.monotonic()
        with self._lock:
            ready = [p for p in self._pending if force or now >= p.get("settle_at", 0.0)]
            self._pending = [p for p in self._pending if p not in ready]

        for seg in ready:
            abs_path = seg["abs_path"]
            if not abs_path.exists():
                log.warning(f"Segment {seg['clip_id']} never materialised — skipping")
                continue

            started, closed = seg["started_at"], seg.get("closed_at") or utc_now()
            self.store.write_sidecar(abs_path, {
                "ended_at":   closed.isoformat(),
                "duration_s": round((closed - started).total_seconds(), 3),
                "width_px":   RECORD_WIDTH,
                "height_px":  RECORD_HEIGHT,
                "fps":        RECORD_FPS,
            })

            # Stamp protection at creation so a clip recorded during an already
            # protected trip is exempt from the very first prune it sees.
            if self.store.is_within_windows(started):
                self.store.mark_protected(abs_path)

            clip = self.store.read_clip(abs_path)
            if clip is None:
                continue
            self._segments_written += 1
            if self._on_clip_closed:
                try:
                    self._on_clip_closed(clip)
                except Exception as e:
                    log.warning(f"on_clip_closed failed for {clip.clip_id}: {e}")

    # -- main-loop hooks ---------------------------------------------------

    def tick(self) -> None:
        """Pump the bus, finalize settled segments, prune on a slow cadence.
        Called from the publisher's main loop; never raises."""
        if self._pipeline is None:
            return
        try:
            self._drain_bus()
            self._finalize_pending()
            if time.monotonic() - self._last_prune >= PRUNE_INTERVAL_S:
                self._last_prune = time.monotonic()
                self.prune()
        except Exception as e:
            log.warning(f"recorder tick failed — continuing: {e}")

    def _drain_bus(self) -> None:
        if self._bus is None:
            return
        while True:
            msg = self._bus.timed_pop_filtered(
                0, Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS)
            if msg is None:
                return
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                log.error(f"Pipeline error: {err} ({debug})")
                self._failed = True
            elif msg.type == Gst.MessageType.WARNING:
                err, _ = msg.parse_warning()
                log.warning(f"Pipeline warning: {err}")
            elif msg.type == Gst.MessageType.EOS:
                log.warning("Pipeline reached EOS unexpectedly")
                self._failed = True

    def read_frame(self) -> Optional[np.ndarray]:
        """Latest frame from the inference branch as BGR, or None."""
        appsink = self._appsink
        if appsink is None:
            return None
        try:
            sample = appsink.emit("try-pull-sample", PULL_TIMEOUT_NS)
            if sample is None:
                return None
            return self._sample_to_bgr(sample)
        except Exception as e:
            log.debug(f"Frame pull failed: {e}")
            return None

    @staticmethod
    def _sample_to_bgr(sample) -> Optional[np.ndarray]:
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if buf is None or caps is None:
            return None
        structure = caps.get_structure(0)
        ok_w, width = structure.get_int("width")
        ok_h, height = structure.get_int("height")
        if not (ok_w and ok_h):
            return None

        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            # GStreamer pads rows up to a 4-byte boundary. width*3 is already
            # aligned at 1280, but derive the stride rather than assume it.
            stride = mapinfo.size // height if height else width * 3
            flat = np.frombuffer(mapinfo.data, dtype=np.uint8, count=stride * height)
            return flat.reshape((height, stride))[:, : width * 3].reshape((height, width, 3)).copy()
        except (ValueError, TypeError):
            return None
        finally:
            buf.unmap(mapinfo)

    # -- control -----------------------------------------------------------

    def split_now(self) -> bool:
        """Force a segment boundary — used to isolate a crash in its own clip
        and to align segments with trip start/stop."""
        if self._splitmux is None:
            return False
        try:
            self._splitmux.emit("split-now")
            return True
        except Exception as e:
            log.warning(f"split-now failed: {e}")
            return False

    def prune(self) -> dict:
        result = self.store.prune(RETENTION_DAYS, MAX_BYTES, MIN_FREE_BYTES)
        self._storage_pressure = result["storage_pressure"]
        if result["deleted"] and self._on_pruned:
            try:
                self._on_pruned(result["deleted"], result["reason"] or "budget")
            except Exception as e:
                log.warning(f"on_pruned failed: {e}")
        return result

    def protect(self, window_id: str, frm: str, to: Optional[str]) -> None:
        self.store.add_window(window_id, frm, to)
        log.info(f"Protection window {window_id} set ({frm} .. {to or 'open'})")

    def unprotect(self, window_id: str) -> bool:
        removed = self.store.remove_window(window_id)
        if removed:
            log.info(f"Protection window {window_id} removed")
        return removed

    def delete_clips(self, clip_ids: list) -> list:
        deleted = [cid for cid in clip_ids if self.store.delete(cid)]
        if deleted:
            log.info(f"Deleted {len(deleted)} clip(s) on request")
        return deleted

    # -- reporting ---------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._pipeline is not None and not self._failed

    def stats(self) -> dict:
        stats = self.store.stats()
        with self._lock:
            current = self._current["clip_id"] if self._current else None
        stats.update({
            "recording":        self.recording,
            "profile":          self._profile,
            "current_clip":     current,
            "segments_written": self._segments_written,
            "storage_pressure": self._storage_pressure,
        })
        return stats


# ---------------------------------------------------------------------------
# Standalone run — record without touching vision_publisher.
#   VISION_RECORD_ENABLED=1 MAVERICK_DASHCAM_ROOT=/tmp/dash python recorder.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import signal
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    rec = Recorder(on_clip_closed=lambda c: log.info(f"clip closed: {json.dumps(c.as_payload())}"))
    if not rec.start():
        log.error("Recorder failed to start")
        sys.exit(1)

    stopping = False

    def _stop(*_):
        global stopping
        stopping = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        last_report = 0.0
        while not stopping:
            rec.tick()
            frame = rec.read_frame()
            if time.monotonic() - last_report > 10:
                last_report = time.monotonic()
                shape = frame.shape if frame is not None else None
                log.info(f"frame={shape} stats={json.dumps(rec.stats())}")
            time.sleep(0.05)
    finally:
        rec.stop()
