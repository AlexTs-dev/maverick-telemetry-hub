"""
jetson/camera.py
Maverick Telemetry Hub — Jetson frame source arbiter

Exactly one process may own the camera: a second cv2.VideoCapture on the same
/dev/video* fails on essentially every V4L2 device. This module is where that
invariant lives. It resolves a frame source once, in order of preference:

    1. recorder.py's GStreamer appsink  — recording AND inference off one tee
    2. cv2.VideoCapture                 — inference only, no footage
    3. generated test pattern           — no camera at all (dev machines)

and hands classifier.step() the same frames either way, so vision_publisher
does not care which rung it landed on.

THE FALLBACK IS THE POINT. Recording is a new, comparatively complex path; the
speed-limit inference it sits next to is shipped, working code that must not
regress. Anything that goes wrong with recording — missing PyGObject, no NVENC,
caps that will not negotiate, a pipeline that errors out mid-drive — demotes the
source one rung and leaves inference running.
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger("camera")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# auto — try the default camera, fall back to a test pattern until it works
# test — always publish a generated test pattern (dev machines, no camera)
# anything else — passed to cv2.VideoCapture verbatim (e.g. a GStreamer
#                 pipeline string for the CSI camera via nvarguscamerasrc)
VISION_SOURCE = os.environ.get("VISION_SOURCE", "auto")

FRAME_WIDTH  = 1280
FRAME_HEIGHT = 720

# cv2.VideoCapture buffers ~4 frames. A read after a long gap (sparse sampling)
# would return stale pixels stamped with a fresh ts — silently breaking OBD
# alignment — so reads after a gap drain the buffer first. The recorder path
# does not need this: its appsink is drop=true max-buffers=1.
CAMERA_DRAIN_AFTER_S = 0.5
CAMERA_DRAIN_GRABS   = 4

# Camera reopen backoff — mirrors obd_poller's serial backoff
INITIAL_BACKOFF = 2
MAX_BACKOFF     = 60

MODE_RECORDER = "recorder"
MODE_CAMERA   = "camera"
MODE_TEST     = "test"

# ---------------------------------------------------------------------------
# Test pattern
# ---------------------------------------------------------------------------

def test_pattern_frame(n: int) -> np.ndarray:
    """Generated frame for camera-less dev machines: moving gradient + stamp."""
    frame = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
    gradient = np.linspace(0, 255, FRAME_WIDTH, dtype=np.uint8)
    frame[:, :, 0] = gradient                               # static blue ramp
    frame[:, :, 1] = (gradient.astype(int) + n * 8) % 256   # slides each frame
    cv2.putText(frame, f"TEST PATTERN frame={n}", (40, 80),
                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    cv2.putText(frame, datetime.now(timezone.utc).isoformat(), (40, 140),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    return frame

# ---------------------------------------------------------------------------
# cv2 capture
# ---------------------------------------------------------------------------

def open_camera():
    """cv2.VideoCapture per VISION_SOURCE, or None (caller uses test pattern)."""
    if VISION_SOURCE == "test":
        return None
    source = 0 if VISION_SOURCE == "auto" else VISION_SOURCE
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        return None
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    # Best-effort stale-frame defense; honored on most V4L2 backends, ignored
    # elsewhere. GStreamer sources need drop=true max-buffers=1 on the appsink
    # instead — see jetson/README.md.
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap

# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------

class FrameSource:
    """Resolves and maintains the active frame source.

    Usage:
        source = FrameSource(recorder)   # recorder may be None
        source.start()
        ...
        source.tick()                    # recovery + recorder housekeeping
        frame = source.read()            # always returns a frame
    """

    def __init__(self, recorder=None):
        self._recorder = recorder
        self._recorder_active = False
        self._cap = None
        self._frame_count = 0
        self._last_capture_t = 0.0
        self._backoff = INITIAL_BACKOFF
        self._next_retry = 0.0
        self._recorder_miss_logged = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Resolve the best available source. Returns the resulting mode."""
        if self._recorder is not None and VISION_SOURCE != "test":
            if self._recorder.start():
                self._recorder_active = True
                log.info("Frame source: recorder appsink (recording + inference)")
                return MODE_RECORDER
            log.info("Frame source: recorder unavailable — falling back to cv2.VideoCapture")

        self._cap = open_camera()
        if self._cap is None and VISION_SOURCE != "test":
            log.warning("Camera unavailable — publishing test pattern until it returns")
        return self.mode

    def release(self) -> None:
        if self._recorder_active and self._recorder is not None:
            self._recorder.stop()
            self._recorder_active = False
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    # -- state -------------------------------------------------------------

    @property
    def mode(self) -> str:
        if self._recorder_active:
            return MODE_RECORDER
        return MODE_CAMERA if self._cap is not None else MODE_TEST

    @property
    def recording(self) -> bool:
        return self._recorder_active and self._recorder is not None and self._recorder.recording

    @property
    def frame_count(self) -> int:
        return self._frame_count

    # -- per-tick maintenance ---------------------------------------------

    def tick(self) -> None:
        """Recorder housekeeping plus camera recovery. Never raises."""
        if self._recorder_active and self._recorder is not None:
            self._recorder.tick()
            # A pipeline that errored out mid-drive still holds the V4L2 device.
            # Tear it down so the cv2 fallback can actually open the camera —
            # otherwise inference would be stuck on the test pattern for the
            # rest of the drive.
            if not self._recorder.recording:
                log.error("Recording pipeline failed — releasing camera and "
                          "falling back to cv2.VideoCapture")
                try:
                    self._recorder.stop()
                except Exception as e:
                    log.warning(f"Error stopping failed recorder: {e}")
                self._recorder_active = False
                self._next_retry = 0.0
            return

        now = time.monotonic()
        if self._cap is None and VISION_SOURCE != "test" and now >= self._next_retry:
            self._cap = open_camera()
            if self._cap is not None:
                log.info("Camera reopened")
                self._backoff = INITIAL_BACKOFF
            else:
                self._next_retry = now + self._backoff
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    # -- capture -----------------------------------------------------------

    def read(self) -> np.ndarray:
        """One frame, always. Falls back a rung rather than returning None —
        callers are on the inference path and must never have to handle a
        missing frame."""
        frame = None

        if self._recorder_active and self._recorder is not None:
            frame = self._recorder.read_frame()
            if frame is None:
                # A momentary miss is normal: the appsink is drop=true and the
                # classifier samples faster than a stalled branch refills.
                if not self._recorder_miss_logged:
                    log.debug("Recorder appsink returned no frame — using test pattern")
                    self._recorder_miss_logged = True
            else:
                self._recorder_miss_logged = False

        elif self._cap is not None:
            drain = (time.monotonic() - self._last_capture_t) > CAMERA_DRAIN_AFTER_S
            if drain:
                # grab() dequeues without decoding — discarding buffered stale
                # frames is nearly free, and the read() below returns fresh pixels.
                for _ in range(CAMERA_DRAIN_GRABS):
                    self._cap.grab()
            ok, captured = self._cap.read()
            if ok:
                frame = captured
            else:
                log.warning("Camera read failed — releasing, falling back to test pattern")
                self._cap.release()
                self._cap = None

        if frame is None:
            frame = test_pattern_frame(self._frame_count)

        self._last_capture_t = time.monotonic()
        self._frame_count += 1
        return frame
