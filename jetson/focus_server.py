"""
jetson/focus_server.py
Maverick Telemetry Hub — browser-based live focus meter

focus_assist.py prints the focus metric to a terminal, which is the right tool
when you are already on an SSH session. It is the wrong tool when you are in the
driver's seat with one hand on the lens: you cannot read a scrolling terminal on
a phone, and the thing you actually need — "is the number going up or down as I
turn?" — is exactly what a wall of text hides.

This serves the same measurement as a web page instead. Open it on a phone,
prop it on the dash, and turn the ring until the bar peaks.

    ./venv/bin/python focus_server.py            # http://<jetson>:8090/

WHAT IT SHARES WITH focus_assist.py. Everything that decides a number: the
metric functions, the capture setup, and the exposure verdict are imported, not
reimplemented. A focus meter that disagreed with the other focus meter would be
worse than having only one.

MEASURED AT NATIVE RESOLUTION, PREVIEWED SMALL. The sharpness figure is computed
on the full-resolution frame before any resampling — downscaling first would
low-pass away the high frequencies the metric exists to measure, and a badly set
lens would score respectably. The preview JPEG is downscaled *afterwards*, and
only to keep the stream light over WiFi. The picture you see is a proxy; the
number is the measurement.

ONE CAMERA, MANY VIEWERS. Exactly one process may own the camera, and by the
same logic one process may open it once. A single capture thread reads frames
and every connected browser is served from the latest one, so opening the page
on a phone and a laptop at the same time is free rather than a second open that
fails.

EXACTLY ONE PROCESS MAY OWN THE CAMERA — vision_publisher has to be stopped
first, same as focus_assist.py.

    sudo systemctl stop vision_publisher
    ./venv/bin/python focus_server.py
    sudo systemctl restart vision_publisher     # passwordless; start is not

See CAMERA-TUNING.md §2 for the procedure this is the instrument for.
"""

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import cv2

# focus_assist.py owns the measurement. Import it rather than restating it, so
# the browser and the terminal can never report different numbers for the same
# lens. Same directory, so this works when run from jetson/.
try:
    from focus_assist import (WARMUP_FRAMES, camera_owner, centre_crop,
                              exposure_health, grid_sharpness, open_capture,
                              sharpness, verdict)
except ImportError as e:  # pragma: no cover - operator error, not a code path
    sys.exit(f"Cannot import focus_assist.py ({e}).\n"
             f"Run this from the jetson/ directory: "
             f"cd ~/maverick-telemetry-hub/jetson")

# Redraw rate for the meter pushed to the browser. Matches focus_assist's
# REFRESH_S rationale: faster is unreadable with your hand on the lens, slower
# lags the ring.
PUSH_S = 0.12

# The grid is 9 Laplacians over the full frame — far more expensive than the
# centre crop, and it diagnoses mount tilt, which does not change while you turn
# a focus ring. Once a second is plenty.
GRID_S = 1.0

# Preview only. Bandwidth, not measurement — see the module docstring.
PREVIEW_WIDTH = 960
PREVIEW_QUALITY = 70
PREVIEW_FPS = 15


class FrameHub:
    """The single capture thread's latest frame and metrics.

    Readers never block the camera: the thread publishes a finished JPEG and a
    finished metrics dict, and viewers take whatever is current. A slow phone on
    WiFi therefore drops frames instead of throttling the measurement.
    """

    def __init__(self, roi: float, halflife: float):
        self._roi = roi
        self._cond = threading.Condition()
        self._jpeg: Optional[bytes] = None
        self._metrics: dict = {}
        self._seq = 0
        self._stop = threading.Event()
        self.error: Optional[str] = None

        # Peak-hold with the same decay law as focus_assist: a peak set by one
        # lucky frame, or by overshooting the true maximum, fades instead of
        # pinning the bar low forever.
        self._halflife = halflife
        self._peak = 0.0
        self._best = 0.0
        self._reset_requested = False

    # -- capture side -----------------------------------------------------

    def run(self, cap) -> None:
        decay = 0.5 ** (PUSH_S / max(self._halflife, 0.1))
        frames = 0
        started = time.monotonic()
        last_push = 0.0
        last_grid = 0.0
        last_preview = 0.0
        cells = None

        while not self._stop.is_set():
            ok, frame = cap.read()
            if not ok:
                self.error = ("Camera read failed. Check the ribbon seating "
                              "and that nothing else grabbed the device.")
                with self._cond:
                    self._cond.notify_all()
                return
            frames += 1

            # Native resolution, before any resize — see module docstring.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            roi = centre_crop(gray, self._roi)
            value = sharpness(roi)
            luma, clipped, crushed = exposure_health(roi)

            now = time.monotonic()

            if self._reset_requested:
                # Deliberately clears `best` too. The button means "I have moved
                # to a new target"; carrying the old scene's maximum across
                # would make every later reading look like a regression.
                self._reset_requested = False
                self._peak = 0.0
                self._best = 0.0

            self._best = max(self._best, value)
            self._peak = max(self._peak * decay, value)

            if now - last_grid >= GRID_S:
                last_grid = now
                cells = grid_sharpness(gray)

            if now - last_push < PUSH_S:
                continue
            last_push = now

            ratio = value / self._peak if self._peak > 0 else 0.0
            metrics = {
                "sharp": round(value, 1),
                "ratio": round(ratio, 4),
                "peak": round(self._peak, 1),
                "best": round(self._best, 1),
                "luma": round(luma, 1),
                "clip": round(clipped, 1),
                "crushed": round(crushed, 1),
                "fps": round(frames / max(now - started, 1e-6), 1),
                "hint": verdict(luma, clipped, crushed),
                "frames": frames,
            }
            if cells:
                peak_cell = max(max(row) for row in cells) or 1.0
                metrics["grid"] = [[round(c / peak_cell, 3) for c in row]
                                   for row in cells]

            jpeg = None
            if now - last_preview >= 1.0 / PREVIEW_FPS:
                last_preview = now
                jpeg = self._encode_preview(frame)

            with self._cond:
                self._metrics = metrics
                if jpeg is not None:
                    self._jpeg = jpeg
                self._seq += 1
                self._cond.notify_all()

    def _encode_preview(self, frame):
        """Downscale, mark the measured region, encode.

        The ROI rectangle is drawn because the number only describes what is
        inside it. Without the box, the natural thing to do is aim the *frame*
        at the target and wonder why the reading does not respond.
        """
        h, w = frame.shape[:2]
        if w > PREVIEW_WIDTH:
            scale = PREVIEW_WIDTH / w
            frame = cv2.resize(frame, (PREVIEW_WIDTH, int(h * scale)),
                               interpolation=cv2.INTER_AREA)
            h, w = frame.shape[:2]

        rh, rw = int(h * self._roi), int(w * self._roi)
        y, x = (h - rh) // 2, (w - rw) // 2
        cv2.rectangle(frame, (x, y), (x + rw, y + rh), (0, 255, 255), 2)

        ok, buf = cv2.imencode(".jpg", frame,
                               [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY])
        return buf.tobytes() if ok else None

    # -- viewer side ------------------------------------------------------

    def wait(self, last_seq: int, timeout: float = 5.0):
        """Block until a frame newer than `last_seq`. Returns (seq, jpeg, metrics)."""
        with self._cond:
            if self._seq <= last_seq:
                self._cond.wait(timeout)
            return self._seq, self._jpeg, self._metrics

    def reset_peak(self) -> None:
        self._reset_requested = True

    def stop(self) -> None:
        self._stop.set()


HUB: Optional[FrameHub] = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # noqa: D102 - quiet; journald has enough
        pass

    def do_POST(self):
        if self.path.rstrip("/") == "/reset":
            HUB.reset_peak()
            self._send_json({"ok": True})
        else:
            self.send_error(404)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/":
            self._send_html()
        elif path == "/stream.mjpg":
            self._send_stream()
        elif path == "/metrics":
            self._send_events()
        elif path == "/healthz":
            self._send_json({"ok": HUB.error is None, "error": HUB.error})
        else:
            self.send_error(404)

    def _send_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self):
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = 0
        try:
            while True:
                seq, jpeg, _ = HUB.wait(seq)
                if HUB.error:
                    return
                if not jpeg:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " +
                                 str(len(jpeg)).encode() + b"\r\n\r\n")
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # A phone locking its screen is normal, not an error worth logging.
            pass

    def _send_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = 0
        try:
            while True:
                seq, _, metrics = HUB.wait(seq)
                payload = dict(metrics)
                if HUB.error:
                    payload = {"error": HUB.error}
                self.wfile.write(b"data: " + json.dumps(payload).encode() +
                                 b"\n\n")
                if HUB.error:
                    return
        except (BrokenPipeError, ConnectionResetError):
            pass


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Maverick — focus</title>
<style>
  :root{
    --bg:#0b0f14; --panel:#141b24; --line:#243040;
    --text:#e8eef6; --dim:#8fa3b8; --good:#39d98a; --warn:#ffb020; --bad:#ff5c5c;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:16px/1.4 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       -webkit-text-size-adjust:100%}
  header{display:flex;align-items:baseline;gap:.6rem;padding:.7rem 1rem;
         border-bottom:1px solid var(--line)}
  header h1{font-size:1rem;margin:0;font-weight:600;letter-spacing:.02em}
  header .sub{color:var(--dim);font-size:.8rem}
  main{padding:1rem;display:grid;gap:1rem;max-width:900px;margin:0 auto}

  /* The number you actually tune against. Big enough to read at arm's length
     on a dashboard in daylight. */
  .ratio{font-size:clamp(3.5rem,18vw,6rem);font-weight:700;line-height:1;
         font-variant-numeric:tabular-nums;letter-spacing:-.02em}
  .ratio small{font-size:.28em;color:var(--dim);font-weight:600;margin-left:.2em}
  .barwrap{height:26px;background:#0a0e13;border:1px solid var(--line);
           border-radius:6px;overflow:hidden;margin:.6rem 0}
  .bar{height:100%;width:0;background:linear-gradient(90deg,#2b7fff,var(--good));
       transition:width .1s linear}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));
         gap:.5rem}
  .stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;
        padding:.5rem .6rem}
  .stat .k{color:var(--dim);font-size:.7rem;text-transform:uppercase;
           letter-spacing:.06em}
  .stat .v{font-size:1.15rem;font-weight:600;font-variant-numeric:tabular-nums}

  .hint{padding:.6rem .8rem;border-radius:8px;font-size:.9rem;display:none}
  .hint.on{display:block;background:#3a2a10;border:1px solid #6b4a12;
           color:var(--warn)}

  img{width:100%;display:block;border-radius:10px;border:1px solid var(--line);
      background:#000}

  .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;max-width:260px}
  .cell{aspect-ratio:1;border-radius:5px;display:flex;align-items:center;
        justify-content:center;font-size:.78rem;font-weight:600;
        font-variant-numeric:tabular-nums;color:#04121a;background:#1b2430}

  button{appearance:none;border:1px solid var(--line);background:var(--panel);
         color:var(--text);font:inherit;font-weight:600;padding:.85rem 1rem;
         border-radius:10px;width:100%;cursor:pointer}
  button:active{background:#1d2836}
  .row{display:grid;grid-template-columns:1fr;gap:.6rem}
  .dim{color:var(--dim);font-size:.82rem}
  .offline{color:var(--bad)}
  h2{font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;
     color:var(--dim);margin:0 0 .5rem}
</style>
</head>
<body>
<header>
  <h1>Maverick focus</h1>
  <span class="sub" id="src">connecting…</span>
</header>
<main>
  <section>
    <div class="ratio"><span id="ratio">--</span><small>% of peak</small></div>
    <div class="barwrap"><div class="bar" id="bar"></div></div>
    <div class="dim">Turn the ring slowly. Chase the highest number.</div>
  </section>

  <div class="hint" id="hint"></div>

  <section class="stats">
    <div class="stat"><div class="k">sharp</div><div class="v" id="sharp">--</div></div>
    <div class="stat"><div class="k">best</div><div class="v" id="best">--</div></div>
    <div class="stat"><div class="k">luma</div><div class="v" id="luma">--</div></div>
    <div class="stat"><div class="k">clip</div><div class="v" id="clip">--</div></div>
    <div class="stat"><div class="k">fps</div><div class="v" id="fps">--</div></div>
  </section>

  <div class="row">
    <button id="reset">Reset peak — new target</button>
  </div>

  <section>
    <h2>Preview <span class="dim">— yellow box is what is measured</span></h2>
    <img id="cam" alt="camera preview">
  </section>

  <section>
    <h2>3×3 sharpness — % of best cell</h2>
    <div class="grid" id="grid"></div>
    <p class="dim" style="margin:.6rem 0 0">
      Uniformly soft corners are the lens and are fine — signs are read near the
      centre. One soft <em>edge</em> with the opposite edge sharp is mount tilt.
      A soft centre with sharper edges means focus has gone past infinity.
      Only trust this on an evenly lit, evenly detailed target.
    </p>
  </section>
</main>

<script>
const $ = id => document.getElementById(id);

// Cache-bust so a reconnect never resurrects a dead stream from bfcache.
$('cam').src = '/stream.mjpg?t=' + Date.now();

const gridEl = $('grid');
const cells = [];
for (let i = 0; i < 9; i++) {
  const d = document.createElement('div');
  d.className = 'cell';
  d.textContent = '--';
  gridEl.appendChild(d);
  cells.push(d);
}

// Blue (cold/soft) through green (sharp). Same direction as the bar, so "more
// colour" always means "better" wherever you look on the page.
function cellColour(v){
  const h = 210 - 90 * Math.max(0, Math.min(1, v));
  const l = 22 + 48 * Math.max(0, Math.min(1, v));
  return `hsl(${h} 70% ${l}%)`;
}

let es;
function connect(){
  es = new EventSource('/metrics');
  es.onopen = () => { $('src').textContent = 'live'; $('src').classList.remove('offline'); };
  es.onerror = () => { $('src').textContent = 'reconnecting…'; $('src').classList.add('offline'); };
  es.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.error){ $('src').textContent = m.error; $('src').classList.add('offline'); return; }

    const pct = Math.round((m.ratio || 0) * 100);
    $('ratio').textContent = pct;
    $('bar').style.width = pct + '%';
    $('sharp').textContent = m.sharp;
    $('best').textContent = m.best;
    $('luma').textContent = m.luma;
    $('clip').textContent = m.clip + '%';
    $('fps').textContent = m.fps;

    // clip is the reason a good lens reads soft, so make it shout rather than
    // sit quietly in a row of grey numbers.
    $('clip').style.color = m.clip > 15 ? 'var(--bad)'
                          : m.clip > 5  ? 'var(--warn)' : 'var(--text)';

    if (m.hint){ $('hint').textContent = m.hint; $('hint').classList.add('on'); }
    else { $('hint').classList.remove('on'); }

    if (m.grid){
      const flat = m.grid.flat();
      flat.forEach((v,i) => {
        cells[i].textContent = Math.round(v*100) + '%';
        cells[i].style.background = cellColour(v);
      });
    }
  };
}
connect();

$('reset').addEventListener('click', () => {
  fetch('/reset', {method:'POST'});
  $('ratio').textContent = '--';
  $('bar').style.width = '0%';
});
</script>
</body>
</html>
"""


def lan_addresses(port: int) -> list[str]:
    """Best-effort list of URLs this page is reachable on.

    Printed because the whole point is to open it from a phone, and hunting for
    the Jetson's address is a silly way to start a focus session.
    """
    urls = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                urls.append(f"http://{ip}:{port}/")
    except socket.gaierror:
        pass
    return sorted(set(urls))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Live focus meter served as a web page.",
        epilog="Stop vision_publisher first — only one process may own the camera.")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--roi", type=float, default=0.5,
                   help="centre fraction of each axis to measure (default 0.5)")
    p.add_argument("--peak-halflife", type=float, default=20.0,
                   help="seconds for the held peak to halve (default 20)")
    p.add_argument("--force", action="store_true",
                   help="skip the vision_publisher ownership check")
    # Mirrors focus_assist.py so the same flags select the same camera.
    p.add_argument("--usb", action="store_true")
    p.add_argument("--device", default=None)
    p.add_argument("--source")
    p.add_argument("--untuned", action="store_true")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--fps", type=int, default=30)
    args = p.parse_args(argv)

    if not 0.05 <= args.roi <= 1.0:
        p.error("--roi must be between 0.05 and 1.0")
    if args.device is None:
        from focus_assist import RECORD_DEVICE
        args.device = RECORD_DEVICE

    owner = camera_owner()
    if owner and not args.force:
        sys.exit(f"{owner} is running and owns the camera — exactly one process "
                 f"may hold it.\n\n    sudo systemctl stop {owner}\n\n"
                 f"...then re-run this. (--force skips this check; the open "
                 f"will simply fail.)")

    cap, described = open_capture(args)
    if not cap.isOpened():
        sys.exit(f"Could not open the camera ({described}).\n"
                 f"Check the ribbon seating and `ls /dev/video*`; "
                 f"`focus_assist.py --list-modes` will show whether Argus sees "
                 f"the sensor.")

    print(f"Source : {described}")
    print(f"ROI    : centre {args.roi:.0%} of frame")
    print(f"Warmup : {WARMUP_FRAMES} frames for AE/AWB to settle...", flush=True)
    for _ in range(WARMUP_FRAMES):
        if not cap.read()[0]:
            cap.release()
            sys.exit("Camera opened but delivered no frames.")

    global HUB
    HUB = FrameHub(args.roi, args.peak_halflife)
    worker = threading.Thread(target=HUB.run, args=(cap,), daemon=True)
    worker.start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    print("\nOpen on your phone:")
    for url in lan_addresses(args.port) or [f"http://<jetson-ip>:{args.port}/"]:
        print(f"    {url}")
    print("\nCtrl-C to stop, then:  sudo systemctl restart vision_publisher\n",
          flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HUB.stop()
        server.server_close()
        cap.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
