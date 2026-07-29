"""
jetson/clipstore.py
Maverick Telemetry Hub — Jetson dashcam clip store

Owns the on-disk layout of dashcam footage: naming, sidecars, protection, and
retention. Pure stdlib — no gi, no cv2, no paho — so recorder.py can write
through it, clip_server.py can read through it, and the pruner can be exercised
against a throwaway directory on any machine (see __main__).

Layout:
    $root/clips/YYYY/MM/DD/<clip_id>.mp4        the footage
                           <clip_id>.json       sidecar: exact end/duration/dims
                           <clip_id>.protected  marker: presence == exempt from pruning
    $root/protected.json                        protection *windows*

    clip_id = 20260729T143005Z_a1b2c3d4

THE FILESYSTEM IS THE SOURCE OF TRUTH. A clip's start time is encoded in its
filename and its end time is recoverable from mtime, so every clip stays
readable after an ungraceful power cut — which, on an ignition-switched box, is
the normal way this process dies. The sidecar only *refines* that metadata.
There is deliberately no central index of clips to fall out of sync with disk.

Protection is stored two ways because it has two jobs:
  - a per-clip `.protected` marker — no read-modify-write of a shared file, so
    a power cut can never corrupt the protection state of unrelated clips;
  - `protected.json` *windows* — needed because protecting a trip must also
    cover clips that have not been recorded yet.
"""

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("clipstore")

CLIP_SUFFIX      = ".mp4"
SIDECAR_SUFFIX   = ".json"
PROTECTED_SUFFIX = ".protected"

_CLIP_STAMP_FMT = "%Y%m%dT%H%M%SZ"
# 20260729T143005Z_a1b2c3d4 — anchored, so a hostile or corrupt name from the
# wire can never resolve to a path outside the clip tree.
_CLIP_ID_RE = re.compile(r"^(\d{8}T\d{6}Z)_([0-9a-f]{8})$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def clip_id_start(clip_id: str) -> Optional[datetime]:
    """Start time encoded in a clip_id, or None if the id is malformed."""
    m = _CLIP_ID_RE.match(clip_id or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _CLIP_STAMP_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_valid_clip_id(clip_id: str) -> bool:
    return clip_id_start(clip_id) is not None


@dataclass
class Clip:
    clip_id:     str
    rel_path:    str          # POSIX, relative to $root/clips — what the Pi stores
    started_at:  str          # ISO-8601 UTC
    ended_at:    str
    duration_s:  float
    size_bytes:  int
    width_px:    Optional[int] = None
    height_px:   Optional[int] = None
    fps:         Optional[float] = None
    protected:   bool = False

    def as_payload(self) -> dict:
        d = asdict(self)
        d["path"] = d.pop("rel_path")
        return d


class ClipStore:
    """On-disk dashcam footage. One writer (recorder.py); readers are read-only."""

    def __init__(self, root: os.PathLike | str):
        self.root = Path(root)
        self.clips_dir = self.root / "clips"
        self.windows_path = self.root / "protected.json"

    # -- layout ------------------------------------------------------------

    def ensure_dirs(self) -> None:
        self.clips_dir.mkdir(parents=True, exist_ok=True)

    def allocate(self, start: datetime) -> tuple[str, str, Path]:
        """Mint a clip_id for a segment starting at `start`. Returns
        (clip_id, rel_path, abs_path) with the parent directory created."""
        clip_id = f"{start.strftime(_CLIP_STAMP_FMT)}_{os.urandom(4).hex()}"
        rel_dir = start.strftime("%Y/%m/%d")
        abs_dir = self.clips_dir / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)
        rel_path = f"{rel_dir}/{clip_id}{CLIP_SUFFIX}"
        return clip_id, rel_path, abs_dir / f"{clip_id}{CLIP_SUFFIX}"

    def resolve(self, rel_path: str) -> Optional[Path]:
        """Map a stored relative path to an absolute one, or None if it escapes
        the clip tree. Every path arriving from MQTT or HTTP goes through here."""
        if not rel_path or "\x00" in rel_path:
            return None
        candidate = (self.clips_dir / rel_path).resolve()
        try:
            candidate.relative_to(self.clips_dir.resolve())
        except ValueError:
            return None
        if candidate.suffix != CLIP_SUFFIX:
            return None
        return candidate

    def path_for_id(self, clip_id: str) -> Optional[Path]:
        """Locate a clip by id alone. The id encodes its date, so this is a
        direct lookup rather than a walk of the whole tree."""
        start = clip_id_start(clip_id)
        if start is None:
            return None
        path = self.clips_dir / start.strftime("%Y/%m/%d") / f"{clip_id}{CLIP_SUFFIX}"
        return path if path.exists() else None

    # -- sidecars ----------------------------------------------------------

    def write_sidecar(self, abs_path: Path, data: dict) -> None:
        """Atomic (.tmp + replace) so a power cut leaves either the old sidecar
        or the new one, never a half-written file."""
        sidecar = abs_path.with_suffix(SIDECAR_SUFFIX)
        tmp = sidecar.with_suffix(SIDECAR_SUFFIX + ".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, sidecar)
        except OSError as e:
            log.warning(f"Could not write sidecar for {abs_path.name}: {e}")
            tmp.unlink(missing_ok=True)

    def _read_sidecar(self, abs_path: Path) -> dict:
        try:
            return json.loads(abs_path.with_suffix(SIDECAR_SUFFIX).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -- reading -----------------------------------------------------------

    def read_clip(self, abs_path: Path) -> Optional[Clip]:
        """Build a Clip from disk. Works with or without a sidecar — a missing
        sidecar is the expected outcome of a power cut mid-segment."""
        clip_id = abs_path.stem
        started = clip_id_start(clip_id)
        if started is None:
            return None
        try:
            st = abs_path.stat()
        except OSError:
            return None

        side = self._read_sidecar(abs_path)
        ended = parse_iso(side.get("ended_at")) or datetime.fromtimestamp(st.st_mtime, timezone.utc)
        if ended < started:
            ended = started
        duration = side.get("duration_s")
        if not isinstance(duration, (int, float)) or duration <= 0:
            duration = (ended - started).total_seconds()

        return Clip(
            clip_id=clip_id,
            rel_path=abs_path.relative_to(self.clips_dir).as_posix(),
            started_at=started.isoformat(),
            ended_at=ended.isoformat(),
            duration_s=round(float(duration), 3),
            size_bytes=st.st_size,
            width_px=side.get("width_px"),
            height_px=side.get("height_px"),
            fps=side.get("fps"),
            protected=abs_path.with_suffix(PROTECTED_SUFFIX).exists(),
        )

    def iter_clips(self) -> Iterator[Clip]:
        """All clips, oldest first. Sorted by clip_id, which sorts by start time
        because the timestamp prefix is fixed-width and zero-padded."""
        if not self.clips_dir.exists():
            return
        paths = sorted(self.clips_dir.rglob(f"*{CLIP_SUFFIX}"), key=lambda p: p.stem)
        for path in paths:
            clip = self.read_clip(path)
            if clip is not None:
                yield clip

    # -- protection --------------------------------------------------------

    def load_windows(self) -> dict:
        try:
            data = json.loads(self.windows_path.read_text(encoding="utf-8"))
            windows = data.get("windows")
            return windows if isinstance(windows, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_windows(self, windows: dict) -> None:
        tmp = self.windows_path.with_suffix(".json.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"windows": windows}, indent=2), encoding="utf-8")
            os.replace(tmp, self.windows_path)
        except OSError as e:
            log.error(f"Could not persist protection windows: {e}")
            tmp.unlink(missing_ok=True)

    def add_window(self, window_id: str, frm: str, to: Optional[str]) -> None:
        """Protect everything starting inside [frm, to]. `to=None` means
        open-ended — used while a trip is still running, then superseded by a
        bounded window on trip_close. Keyed, so a repeat protect for the same
        trip replaces rather than accumulates."""
        windows = self.load_windows()
        windows[window_id] = {"from": frm, "to": to}
        self.save_windows(windows)
        self.apply_windows()

    def remove_window(self, window_id: str) -> bool:
        windows = self.load_windows()
        if window_id not in windows:
            return False
        del windows[window_id]
        self.save_windows(windows)
        return True

    def is_within_windows(self, when: datetime, windows: Optional[dict] = None) -> bool:
        for w in (self.load_windows() if windows is None else windows).values():
            frm = parse_iso(w.get("from"))
            if frm is None or when < frm:
                continue
            to = parse_iso(w.get("to"))
            if to is None or when <= to:
                return True
        return False

    def mark_protected(self, abs_path: Path, protected: bool = True) -> None:
        marker = abs_path.with_suffix(PROTECTED_SUFFIX)
        try:
            if protected:
                marker.touch(exist_ok=True)
            else:
                marker.unlink(missing_ok=True)
        except OSError as e:
            log.warning(f"Could not update protection marker for {abs_path.name}: {e}")

    def apply_windows(self) -> int:
        """Stamp markers on every clip that falls inside a live window. Run at
        startup and whenever windows change, so protection converges even if a
        protect arrived while the clip was still recording."""
        windows = self.load_windows()
        if not windows:
            return 0
        stamped = 0
        for clip in self.iter_clips():
            if clip.protected:
                continue
            started = parse_iso(clip.started_at)
            if started and self.is_within_windows(started, windows):
                path = self.resolve(clip.rel_path)
                if path:
                    self.mark_protected(path)
                    stamped += 1
        if stamped:
            log.info(f"Protected {stamped} clip(s) from retention windows")
        return stamped

    # -- deletion ----------------------------------------------------------

    def delete(self, clip_id: str) -> bool:
        """Remove a clip and its metadata. Footage first: a crash mid-delete
        should leave a stray sidecar, never a sidecar-less orphan .mp4 that the
        pruner would then have to re-derive."""
        path = self.path_for_id(clip_id)
        if path is None:
            return False
        try:
            path.unlink(missing_ok=True)
            path.with_suffix(SIDECAR_SUFFIX).unlink(missing_ok=True)
            path.with_suffix(PROTECTED_SUFFIX).unlink(missing_ok=True)
        except OSError as e:
            log.warning(f"Could not delete {clip_id}: {e}")
            return False
        self._rmdir_if_empty(path.parent)
        return True

    def _rmdir_if_empty(self, directory: Path) -> None:
        """Walk empty date dirs back up toward clips/, never past it."""
        try:
            clips_root = self.clips_dir.resolve()
            current = directory.resolve()
            while current != clips_root and clips_root in current.parents:
                if any(current.iterdir()):
                    return
                current.rmdir()
                current = current.parent
        except OSError:
            pass

    def collect_orphans(self) -> int:
        """Drop sidecars/markers whose .mp4 is gone (an interrupted delete)."""
        if not self.clips_dir.exists():
            return 0
        removed = 0
        for suffix in (SIDECAR_SUFFIX, PROTECTED_SUFFIX):
            for path in self.clips_dir.rglob(f"*{suffix}"):
                if not path.with_suffix(CLIP_SUFFIX).exists():
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed

    # -- retention ---------------------------------------------------------

    def prune(self, retention_days: float, max_bytes: int, min_free_bytes: int) -> dict:
        """Age limit first, then byte budget / free-space floor, oldest first.

        PROTECTED CLIPS ARE NEVER DELETED. If protected footage alone blows the
        budget we surface storage_pressure and stop — silently deleting crash
        footage to reclaim space would defeat the whole point of protecting it.
        """
        self.collect_orphans()
        clips = list(self.iter_clips())          # oldest first
        deleted: list[str] = []
        reasons: set[str] = set()

        # 1. Age
        if retention_days > 0:
            cutoff = utc_now() - timedelta(days=retention_days)
            for clip in clips:
                if clip.protected:
                    continue
                started = parse_iso(clip.started_at)
                if started and started < cutoff and self.delete(clip.clip_id):
                    deleted.append(clip.clip_id)
                    reasons.add("age")

        remaining = [c for c in clips if c.clip_id not in set(deleted)]
        used = sum(c.size_bytes for c in remaining)

        # 2. Byte budget and free-space floor
        for clip in remaining:
            free = self._free_bytes()
            over_budget = max_bytes > 0 and used > max_bytes
            under_floor = min_free_bytes > 0 and free < min_free_bytes
            if not (over_budget or under_floor):
                break
            if clip.protected:
                continue
            if self.delete(clip.clip_id):
                deleted.append(clip.clip_id)
                used -= clip.size_bytes
                reasons.add("budget")

        # 3. Did we run out of things we were allowed to delete?
        free = self._free_bytes()
        pressure = bool((max_bytes > 0 and used > max_bytes)
                        or (min_free_bytes > 0 and free < min_free_bytes))
        if pressure:
            log.error(
                f"Storage pressure: {used / 2**30:.1f} GiB used, {free / 2**30:.1f} GiB free — "
                f"only protected footage remains to delete. Free space manually."
            )

        if deleted:
            log.info(f"Pruned {len(deleted)} clip(s) ({'+'.join(sorted(reasons)) or 'none'})")

        return {
            "deleted": deleted,
            "reason": "+".join(sorted(reasons)) if reasons else None,
            "storage_pressure": pressure,
        }

    def _free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.root).free
        except OSError:
            return 0

    def stats(self) -> dict:
        clips = list(self.iter_clips())
        protected = [c for c in clips if c.protected]
        try:
            usage = shutil.disk_usage(self.root)
            total, free = usage.total, usage.free
        except OSError:
            total = free = 0
        return {
            "clip_count":       len(clips),
            "bytes_used":       sum(c.size_bytes for c in clips),
            "protected_count":  len(protected),
            "protected_bytes":  sum(c.size_bytes for c in protected),
            "oldest_clip_at":   clips[0].started_at if clips else None,
            "newest_clip_at":   clips[-1].started_at if clips else None,
            "disk_total_bytes": total,
            "disk_free_bytes":  free,
        }


# ---------------------------------------------------------------------------
# Standalone exercise
#
# The pruner is the only thing here that destroys data, so it gets a way to be
# driven against a throwaway tree before it is ever pointed at real footage:
#
#   python clipstore.py --root /tmp/t --synth 500 --synth-days 45
#   python clipstore.py --root /tmp/t --stats
#   python clipstore.py --root /tmp/t --prune --retention-days 30
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Inspect or exercise a dashcam clip store")
    ap.add_argument("--root", required=True)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--retention-days", type=float, default=30.0)
    ap.add_argument("--max-bytes", type=int, default=0)
    ap.add_argument("--min-free-bytes", type=int, default=0)
    ap.add_argument("--synth", type=int, metavar="N", help="fabricate N fake clips")
    ap.add_argument("--synth-days", type=float, default=40.0)
    ap.add_argument("--synth-size", type=int, default=4096)
    ap.add_argument("--protect", metavar="WINDOW_ID")
    ap.add_argument("--protect-from")
    ap.add_argument("--protect-to")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    store = ClipStore(args.root)
    store.ensure_dirs()

    if args.synth:
        now = utc_now()
        step = timedelta(days=args.synth_days) / max(args.synth, 1)
        for i in range(args.synth):
            start = now - timedelta(days=args.synth_days) + step * i
            _, _, path = store.allocate(start)
            path.write_bytes(b"\0" * args.synth_size)
            os.utime(path, (start.timestamp() + 60, start.timestamp() + 60))
        print(f"created {args.synth} synthetic clips under {store.clips_dir}")

    if args.protect:
        store.add_window(args.protect, args.protect_from or utc_now().isoformat(), args.protect_to)
        print(f"window {args.protect} added")

    if args.list:
        for clip in store.iter_clips():
            flag = "P" if clip.protected else "-"
            print(f"{flag} {clip.clip_id}  {clip.started_at}  {clip.size_bytes:>10}B  {clip.rel_path}")

    if args.prune:
        result = store.prune(args.retention_days, args.max_bytes, args.min_free_bytes)
        print(json.dumps({**result, "deleted": len(result["deleted"])}, indent=2))

    if args.stats or not (args.list or args.prune or args.synth or args.protect):
        print(json.dumps(store.stats(), indent=2))


if __name__ == "__main__":
    _main()
