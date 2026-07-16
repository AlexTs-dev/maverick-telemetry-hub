"""
jetson/classifier.py
Maverick Telemetry Hub — multi-track temporal-gating classifier

Owns WHEN frames are captured and WHAT gets published, across concurrent
label TRACKS, each with its own gating profile:

- scene       — stable-state semantics: a scene persists, so debounce a
                change via a consecutive-sample streak and drop to sparse
                re-checks once stable. Model: stub (synth in test mode).
- speed_limit — transient-object semantics: a sign is legible ~0.8-2.5s at
                road speed, so this track samples at SL_FPS ALWAYS (sparse
                sampling would be blind exactly when a sign passes),
                confirms via a K-of-M sliding window (motion blur breaks
                consecutive streaks), and NEVER clears its confirmed label
                on absence — no sign in view does not mean the limit
                changed; only a different confirmed value replaces it.
                Model: synthetic sign passes now; the real two-stage
                detector+classifier arrives in speed_limit_model.py.

Public contract (called by vision_publisher):
    step(capture) -> list[dict]   # ticked every ~0.05s
    get_status()  -> {"tracks": {name: {"state": str, "label": str | None}}}

capture() returns {"frame": BGR ndarray, "ts": iso-utc, "frame_id": hex}
minted at capture time. Events from all tracks are shape-identical —
{frame, ts, frame_id, source: "event", scene_label, confidence} — and the
"speed_limit_" label prefix is the only track discriminator downstream.

NOTE: VISION_SOURCE is also read by vision_publisher (camera choice) —
rename in both files or neither.
"""

import collections
import logging
import os
import time

log = logging.getLogger("classifier")  # inherits vision_publisher's basicConfig


_SYNTH_LABELS = ["highway", "city_street", "parking_lot", "residential"]


# --- scene track ---
ACTIVE_FPS               = float(os.environ.get("VISION_ACTIVE_FPS", "10"))
CONFIRM_CONSECUTIVE      = int(os.environ.get("VISION_CONFIRM_CONSECUTIVE", "10"))   # 1.0s at 10fps
STABLE_SAMPLE_INTERVAL_S = float(os.environ.get("VISION_STABLE_SAMPLE_INTERVAL", "3"))
MIN_CONFIDENCE           = float(os.environ.get("VISION_MIN_CONFIDENCE", "0.6"))
SYNTH_PERIOD_S           = float(os.environ.get("VISION_SYNTH_PERIOD", "30"))

# --- speed_limit track ---
SL_ENABLED        = os.environ.get("VISION_SL_ENABLED", "1") == "1"
SL_FPS            = float(os.environ.get("VISION_SL_FPS", "10"))        # never sparse
SL_CONFIRM_HITS   = int(os.environ.get("VISION_SL_CONFIRM_HITS", "3"))  # K ...
SL_WINDOW         = int(os.environ.get("VISION_SL_WINDOW", "10"))       # ... of M samples
SL_MIN_CONFIDENCE = float(os.environ.get("VISION_SL_MIN_CONFIDENCE", "0.5"))
SL_SYNTH_PERIOD_S = float(os.environ.get("VISION_SL_SYNTH_PERIOD", "45"))
_SL_SYNTH_BURST_S = 1.5
# The repeated 55 exercises the no-republish rule end-to-end.
_SL_SYNTH_VALUES  = ["55", "55", "35", "65", "45"]

_ERR_LOG_INTERVAL_S = 10.0

# Synthetic labels: auto-on in test-pattern mode; VISION_SYNTH_LABELS=1/0 forces.
_synth_env = os.environ.get("VISION_SYNTH_LABELS", "")
SYNTH_MODE = _synth_env == "1" or (_synth_env != "0" and os.environ.get("VISION_SOURCE") == "test")


def _stable_state_name(track) -> str:
    s = track["s"]
    if s["candidate"] is not None:
        return "confirming"
    if s["last_label"] is not None:
        return "stable"
    return "searching"


def _transient_state_name(track) -> str:
    s = track["s"]
    if any(v is not None for v in s["window"]):
        return "sighting"
    if s["last_label"] is not None:
        return "holding"    # a limit is held, but sampling never slows
    return "searching"


def _track_interval(track) -> float:
    # Only stable-semantics tracks may slow down; transient tracks have no
    # stable_interval_s and therefore always run at their active rate.
    if "stable_interval_s" in track and track["state_fn"](track) == "stable":
        return track["stable_interval_s"]
    return track["active_interval_s"]


def _build_tracks() -> list:
    """Track registry, built once at import (call sits at end of module)."""
    tracks = [{
        "name":                "scene",
        "infer":               _scene_infer,
        "apply":               _apply_stable,
        "state_fn":            _stable_state_name,
        "min_confidence":      MIN_CONFIDENCE,
        "active_interval_s":   1.0 / ACTIVE_FPS,
        "stable_interval_s":   STABLE_SAMPLE_INTERVAL_S,
        "confirm_consecutive": CONFIRM_CONSECUTIVE,
        "s": {"last_label": None, "candidate": None, "streak": 0,
              "next_sample_t": 0.0},
    }]
    sl_infer = _sl_infer_source()
    if sl_infer is not None:
        tracks.append({
            "name":              "speed_limit",
            "infer":             sl_infer,
            "apply":             _apply_transient,
            "state_fn":          _transient_state_name,
            "min_confidence":    SL_MIN_CONFIDENCE,
            "active_interval_s": 1.0 / SL_FPS,
            "confirm_hits":      SL_CONFIRM_HITS,
            "s": {"last_label": None,
                  "window": collections.deque(maxlen=SL_WINDOW),
                  "next_sample_t": 0.0, "synth_n": 0,
                  "last_resight_log_t": -_ERR_LOG_INTERVAL_S},
        })
    return tracks


def _sl_infer_source():
    """Pick what powers the speed_limit track: synth, the real two-stage
    model, or None (track not registered). The model import is lazy and
    wrapped — a broken ultralytics install must never take down the scene
    track."""
    if not SL_ENABLED:
        return None
    if SYNTH_MODE:
        return _sl_synth_infer
    try:
        import speed_limit_model
    except Exception as e:
        log.warning(f"speed_limit track unavailable (import failed: {e}) — "
                    "scene track only")
        return None
    if speed_limit_model.init():
        # Adapt infer(frame) to the track contract infer(track, frame)
        return lambda track, frame: speed_limit_model.infer(frame)
    return None   # init() already logged the reason


def step(capture) -> list:
    now = time.monotonic()
    due = [t for t in _TRACKS if now >= t["s"]["next_sample_t"]]
    if not due:
        return []                       # not time yet — vast majority of ticks

    captured = capture()                # one capture feeds every due track
    events = []
    for track in due:
        obs = None
        try:
            obs = track["infer"](track, captured["frame"])
            if obs is not None and (not obs[0] or obs[1] is None
                                    or obs[1] < track["min_confidence"]):
                obs = None
        except Exception as e:
            _warn_rate_limited(f"[{track['name']}] infer failed: {e}")
        events.extend(track["apply"](track, obs, captured))
        track["s"]["next_sample_t"] = now + _track_interval(track)
    return events


def get_status() -> dict:
    """{"tracks": {name: {"state": str, "label": str | None}}}"""
    return {"tracks": {t["name"]: {"state": t["state_fn"](t),
                                   "label": t["s"]["last_label"]}
                       for t in _TRACKS}}

def _scene_infer(track, frame):
    """Scene-model stub — returns (label, confidence 0-1) or None. A real
    scene model replaces THIS FUNCTION ONLY. Load models at import time,
    never per call. Keep per-call time well under 5s (step runs on the loop
    thread; the Pi marks status stale after 15s of heartbeat silence)."""
    if SYNTH_MODE:
        return _scene_synth_infer()
    return None


def _scene_synth_infer():
    phase = time.monotonic() / SYNTH_PERIOD_S
    label = _SYNTH_LABELS[int(phase) % len(_SYNTH_LABELS)]
    conf  = round(0.70 + 0.25 * (phase % 1.0), 3)
    return (label, conf)


def _sl_synth_infer(track, frame):
    """Simulated sign passes: a ~1.5s burst of speed_limit_NN detections at
    the start of every SL_SYNTH_PERIOD_S, with every 3rd burst sample
    dropped — proves K-of-M confirms through non-consecutive hits (a strict
    streak never would). Values cycle 55,55,35,65,45: the repeated 55
    proves the no-republish rule."""
    now = time.monotonic()
    if (now % SL_SYNTH_PERIOD_S) > _SL_SYNTH_BURST_S:
        return None                     # no sign in view
    s = track["s"]
    s["synth_n"] += 1
    if s["synth_n"] % 3 == 2:
        return None                     # motion-blur miss mid-pass
    pass_idx = int(now // SL_SYNTH_PERIOD_S)
    value = _SL_SYNTH_VALUES[pass_idx % len(_SL_SYNTH_VALUES)]
    return (f"speed_limit_{value}", 0.82)

def _make_event(label, confidence, captured) -> dict:
    """Shared event constructor — ts/frame_id ride through from the
    confirming capture, never re-stamped."""
    return {
        "frame":       captured["frame"],
        "ts":          captured["ts"],
        "frame_id":    captured["frame_id"],
        "source":      "event",
        "scene_label": label,
        "confidence":  confidence,
    }


def _apply_stable(track, obs, captured) -> list:
    """Stable-state gating: consecutive-streak debounce, sparse-when-stable.
    Exactly the original single-track behavior, on per-track state."""
    s = track["s"]
    before = track["state_fn"](track)
    events = []

    if obs is None:
        # No usable classification — streak broken, confirmed label retained
        s["candidate"], s["streak"] = None, 0
    else:
        label, confidence = obs
        if label == s["last_label"]:
            # Reconfirmed / change aborted — never re-publish the same label
            s["candidate"], s["streak"] = None, 0
            if before == "stable":
                log.info(f"[{track['name']}] sparse check: {label} still stable (conf={confidence})")
        else:
            if label == s["candidate"]:
                s["streak"] += 1
            else:
                s["candidate"], s["streak"] = label, 1
            if s["streak"] >= track["confirm_consecutive"]:
                log.info(f"[{track['name']}] confirmed: {label} (confidence={confidence}) — publishing event")
                events.append(_make_event(label, confidence, captured))
                s["last_label"] = label
                s["candidate"], s["streak"] = None, 0

    after = track["state_fn"](track)
    if after != before:
        detail = f" (candidate={s['candidate']} streak={s['streak']})" if after == "confirming" else ""
        log.info(f"[{track['name']}] state {before} -> {after}{detail}")
    return events


def _apply_transient(track, obs, captured) -> list:
    """Transient-object gating: K-of-M sliding window. Absence NEVER clears
    the held label — only a different confirmed value replaces it."""
    s = track["s"]
    before = track["state_fn"](track)
    events = []

    if obs is None:
        s["window"].append(None)        # a miss dilutes the window, nothing more
    else:
        label, confidence = obs
        if label == s["last_label"]:
            # Re-sighting the held value — change-driven pipeline, no republish.
            # Append None, not the label: re-sights must not pile up as
            # candidate hits for a value that is already held.
            now = time.monotonic()
            if now - s["last_resight_log_t"] >= _ERR_LOG_INTERVAL_S:
                log.info(f"[{track['name']}] re-sighted {label} — no republish")
                s["last_resight_log_t"] = now
            s["window"].append(None)
        else:
            s["window"].append(label)
            hits = sum(1 for v in s["window"] if v == label)
            if hits >= track["confirm_hits"]:
                log.info(f"[{track['name']}] confirmed: {label} (confidence={confidence}, "
                         f"{hits}/{len(s['window'])} window hits) — publishing event")
                events.append(_make_event(label, confidence, captured))
                s["last_label"] = label
                s["window"].clear()     # leftover hits must not double-fire

    after = track["state_fn"](track)
    if after != before:
        log.info(f"[{track['name']}] state {before} -> {after}")
    return events

_last_warn_t = -_ERR_LOG_INTERVAL_S
_suppressed  = 0

def _warn_rate_limited(msg: str) -> None:
    global _last_warn_t, _suppressed
    now = time.monotonic()
    if now - _last_warn_t >= _ERR_LOG_INTERVAL_S:
        if _suppressed:
            msg += f" ({_suppressed} similar suppressed)"
        log.warning(msg)
        _last_warn_t, _suppressed = now, 0
    else:
        _suppressed += 1


# Built at import, once every function above exists. step()/get_status()
# iterate this list; Phase 2 wires the real speed-limit model in via
# _build_tracks().
_TRACKS = _build_tracks()