"""
jetson/speed_limit_model.py
Maverick Telemetry Hub — two-stage speed-limit inference

Stage 1: YOLO26n detector, single class ("speed_limit_sign").
Stage 2: YOLO11n-cls value classifier on the best detected crop
         (classes: 25,30,...,75 mph plus "other" as a false-positive veto).

Consumed by classifier.py's speed_limit track:
    init()  -> bool                          # load + warmup, once
    infer(frame) -> (label, conf) | None     # ("speed_limit_55", 0.78)

Degrades gracefully by design: init() returns False with ONE warning if
ultralytics or the model files are absent (dev machines, pre-training
Jetsons) — the speed_limit track is simply not registered and the scene
track is unaffected. The ultralytics import lives inside init() so merely
importing this module is always safe.

Latency budget (Orin Nano Super, FP16 engines): detector ~15-25ms @960 +
classifier ~1-3ms @96 per sample — far under classifier.py's 5s-per-call
contract. Engine deserialization + CUDA warmup (~1-3s) happens in init(),
never on the tick path.

YOLO() loads .pt and .engine files transparently — desktop testing points
VISION_SL_DETECTOR/VISION_SL_CLASSIFIER at .pt weights; the Jetson uses
.engine files built on-device (see jetson/models/README.md).
"""

import logging
import os

log = logging.getLogger("speed_limit_model")  # inherits vision_publisher's basicConfig

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MODELS_DIR     = os.path.join(os.path.dirname(__file__), "models")
DETECTOR_PATH   = os.environ.get("VISION_SL_DETECTOR",
                                 os.path.join(_MODELS_DIR, "speed_limit_detector.engine"))
CLASSIFIER_PATH = os.environ.get("VISION_SL_CLASSIFIER",
                                 os.path.join(_MODELS_DIR, "speed_limit_value.engine"))

# Detector gate is deliberately loose — stage 2's "other" class + strict
# CLS_CONF do the vetoing. A false LIMIT is worse than a missed sign.
DET_CONF   = float(os.environ.get("VISION_SL_DET_CONF", "0.35"))
CLS_CONF   = float(os.environ.get("VISION_SL_CLS_CONF", "0.70"))

IMGSZ      = int(os.environ.get("VISION_SL_IMGSZ", "960"))  # must match engine export
CLS_IMGSZ  = 96

# Below this box size the digits aren't legible and 25<->35 confusion spikes;
# skip and let a closer sighting confirm (the K-of-M window absorbs the wait).
MIN_BOX_PX  = int(os.environ.get("VISION_SL_MIN_BOX_PX", "24"))
CROP_MARGIN = 0.15

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_det = None
_cls = None
_init_done = False
_available = False

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def init() -> bool:
    """Load both models and warm them up. Idempotent; never raises."""
    global _det, _cls, _init_done, _available
    if _init_done:
        return _available
    _init_done = True
    try:
        import numpy as np
        from ultralytics import YOLO

        for path in (DETECTOR_PATH, CLASSIFIER_PATH):
            if not os.path.exists(path):
                raise FileNotFoundError(path)

        _det = YOLO(DETECTOR_PATH)
        _cls = YOLO(CLASSIFIER_PATH)

        # Warmup: engine deserialization + first-call CUDA setup (~1-3s)
        # must happen here, never on the classifier tick path.
        dummy = np.zeros((720, 1280, 3), dtype=np.uint8)
        _det.predict(dummy, imgsz=IMGSZ, conf=DET_CONF, verbose=False)
        _cls.predict(dummy[:CLS_IMGSZ, :CLS_IMGSZ], imgsz=CLS_IMGSZ, verbose=False)

        _available = True
        log.info(f"speed-limit models loaded — det={DETECTOR_PATH} cls={CLASSIFIER_PATH}")
    except Exception as e:
        log.warning(f"speed_limit track unavailable ({e.__class__.__name__}: {e}) — "
                    "scene track only; see jetson/models/README.md")
        _available = False
    return _available


def infer(frame):
    """Two-stage read: detect the sign, classify the value on the crop.
    Returns ("speed_limit_NN", confidence) or None. May raise — the caller
    (classifier.step) wraps every track infer in a rate-limited guard."""
    boxes = _det.predict(frame, imgsz=IMGSZ, conf=DET_CONF, verbose=False)[0].boxes
    if boxes is None or len(boxes) == 0:
        return None

    i = int(boxes.conf.argmax())
    det_conf = float(boxes.conf[i])
    x1, y1, x2, y2 = (float(v) for v in boxes.xyxy[i])
    w, h = x2 - x1, y2 - y1
    if min(w, h) < MIN_BOX_PX:
        return None

    frame_h, frame_w = frame.shape[:2]
    mx, my = w * CROP_MARGIN, h * CROP_MARGIN
    cx1 = max(0, int(x1 - mx))
    cy1 = max(0, int(y1 - my))
    cx2 = min(frame_w, int(x2 + mx))
    cy2 = min(frame_h, int(y2 + my))
    crop = frame[cy1:cy2, cx1:cx2]

    probs = _cls.predict(crop, imgsz=CLS_IMGSZ, verbose=False)[0].probs
    name = _cls.names[int(probs.top1)]
    cls_conf = float(probs.top1conf)
    if name == "other" or cls_conf < CLS_CONF:
        return None                      # stage-2 veto

    # The event asserts "sign present AND reads NN" — min() is the honest
    # weakest-link confidence for that conjunction.
    return (f"speed_limit_{name}", round(min(det_conf, cls_conf), 3))
