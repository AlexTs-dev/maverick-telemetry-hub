"""
jetson/clip_server.py
Maverick Telemetry Hub — Jetson dashcam clip server

Serves recorded footage read-only over HTTP so the Pi's Express bridge can proxy
it to the dashboard. Footage stays on the Jetson (it has the disk); the Pi holds
only metadata and a path.

WHY THIS EXISTS RATHER THAN http.server's SimpleHTTPRequestHandler: that handler
does not implement Range requests, and without Range a <video> element cannot
seek — it can only play a clip from the start. Range is the whole feature.

READ-ONLY, DELIBERATELY. Only GET and HEAD are implemented, so every other
method gets a 501 from BaseHTTPRequestHandler. Combined with ReadOnlyPaths= on
the clip root in the unit file, that mirrors the Pi's single-writer invariant:
vision_publisher/recorder.py writes footage, this process only reads it. Nothing
here can delete a clip — deletes arrive over MQTT and are executed by the
recorder, which is the process that owns the files.

This is the Jetson's only inbound listener. Every path from the wire goes
through ClipStore.resolve(), which rejects anything that escapes the clip tree
or is not a .mp4.

Config:
    MAVERICK_DASHCAM_ROOT        clip root (default /var/lib/maverick-dashcam)
    MAVERICK_CLIP_SERVER_BIND    default 0.0.0.0
    MAVERICK_CLIP_SERVER_PORT    default 8088

Managed by systemd — see jetson/deploy/clip_server.service
"""

import json
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from clipstore import ClipStore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DASHCAM_ROOT = os.environ.get("MAVERICK_DASHCAM_ROOT", "/var/lib/maverick-dashcam")
BIND_HOST    = os.environ.get("MAVERICK_CLIP_SERVER_BIND", "0.0.0.0")
BIND_PORT    = int(os.environ.get("MAVERICK_CLIP_SERVER_PORT", "8088"))

CHUNK_SIZE = 256 * 1024

# bytes=0-1023 | bytes=1024- | bytes=-1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("clip_server")

store = ClipStore(DASHCAM_ROOT)

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

class ClipRequestHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 for keep-alive: seeking a video fires a burst of Range requests,
    # and a fresh TCP connection for each one is needless latency.
    protocol_version = "HTTP/1.1"
    server_version = "MaverickClipServer/1.0"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        # Default goes to stderr unformatted and is far too chatty: scrubbing a
        # video fires a Range request per seek.
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        self._handle(body=True)

    def do_HEAD(self):
        self._handle(body=False)

    def _handle(self, body: bool) -> None:
        try:
            path = unquote(urlparse(self.path).path)

            if path in ("/health", "/health/"):
                self._send_json(200, {
                    "status": "ok",
                    "root": str(store.root),
                    **store.stats(),
                })
                return

            if not path.startswith("/clips/"):
                self._send_json(404, {"error": "Not found"})
                return

            self._serve_clip(path[len("/clips/"):], body)

        except (BrokenPipeError, ConnectionResetError):
            # Routine: the browser abandons an in-flight range the moment the
            # user seeks somewhere else. Not worth a log line.
            pass
        except Exception as e:
            log.warning(f"Request {self.path} failed: {e}")
            try:
                self._send_json(500, {"error": "Internal error"})
            except Exception:
                pass

    def _serve_clip(self, rel_path: str, body: bool) -> None:
        # resolve() is the jail: it rejects traversal and non-.mp4 paths.
        abs_path = store.resolve(rel_path)
        if abs_path is None or not abs_path.is_file():
            self._send_json(404, {"error": "Clip not found"})
            return

        size = abs_path.stat().st_size
        rng = self._parse_range(self.headers.get("Range"), size)

        if rng == "unsatisfiable":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if rng is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end = rng
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        # A clip is never rewritten once closed, so it can be cached hard.
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if not body:
            return

        with abs_path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    @staticmethod
    def _parse_range(header, size: int):
        """None (whole file) | (start, end) | "unsatisfiable".

        Multi-range requests are answered with the whole file, which is a legal
        response and something no browser video player actually needs.
        """
        if not header or size == 0:
            return None
        match = _RANGE_RE.match(header.strip())
        if not match:
            return None
        first, last = match.group(1), match.group(2)

        if first == "":
            if last == "":
                return None
            suffix = int(last)
            if suffix == 0:
                return "unsatisfiable"
            return max(0, size - suffix), size - 1

        start = int(first)
        if start >= size:
            return "unsatisfiable"
        end = int(last) if last else size - 1
        return start, min(end, size - 1)


def run() -> None:
    log.info(f"clip_server starting — root={DASHCAM_ROOT} bind={BIND_HOST}:{BIND_PORT}")
    if not store.clips_dir.exists():
        log.warning(f"Clip directory {store.clips_dir} does not exist yet — "
                    f"serving 404s until the recorder creates it")

    httpd = ThreadingHTTPServer((BIND_HOST, BIND_PORT), ClipRequestHandler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
