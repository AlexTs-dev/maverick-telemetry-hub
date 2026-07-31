// client/src/components/DashcamSection.tsx
//
// Dashcam footage for one trip: a continuous player, the segment list, and
// deletion.
//
// THE FOOTAGE IS ONE VIDEO, THE FILES ARE NOT. recorder.py cuts a segment every
// VISION_RECORD_SEGMENT_S (60s by default) and at every trip boundary, because
// short self-contained files are what make an ignition cut survivable and what
// lets the retention purge free space a minute at a time. None of that is a
// reason to make someone reviewing a drive click through forty clips, so this
// component stitches them back together at PLAYBACK time: one timeline across
// the whole trip, one scrubber, automatic advance at each boundary. Nothing is
// concatenated on disk — the Jetson's clip root is read-only to everything but
// the recorder, and a merged copy would double a month of 1080p.
//
// The video bytes live on the Jetson. video_url points at the Express proxy on
// the Pi, which range-forwards to the Jetson's clip server — so seeking works,
// and the browser never learns the Jetson exists.
//
// KIOSK CSP: playback here depends on `media-src http://localhost:3000` in
// client/src-tauri/tauri.conf.json. connect-src covers fetch/XHR/WebSocket but
// NOT <video>, so without media-src the element falls back to default-src and
// is blocked with no visible error — it just never plays. (That config is
// schema-validated and rejects unknown keys, so the reason lives here rather
// than as a comment beside it.)
//
// Deletion is asynchronous end to end (202 -> MQTT -> db_writer -> Jetson
// unlinks -> row goes), and irreversible, so every path through it is behind an
// AlertDialog. Protected clips are the footage a crash left behind: they need a
// second, explicit confirmation on top of that.

import { useEffect, useMemo, useRef, useState } from 'react'
import {
  IconChevronRight, IconColorSwatch, IconMaximize, IconMinimize,
  IconPlayerPauseFilled, IconPlayerPlayFilled,
} from '@tabler/icons-react'
import type { DashcamClip } from '@/contexts/TripContext'
import { useDashcam } from '@/contexts/TripContext'
import { Badge }  from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { WindscreenFilter } from '@/components/WindscreenFilter'
import { WINDSCREEN_FILTER_ID } from '@/lib/windscreen'
import { Slider } from '@/components/ui/slider'
import { cn }     from '@/lib/utils'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// recorder.py's default VISION_RECORD_SEGMENT_S. Only reached when a clip has
// neither a sidecar duration nor a usable started/ended span; a guess beats a
// zero-length entry, which would leave that segment unreachable on the timeline.
const FALLBACK_CLIP_S = 60

function formatClipTime(ts: string | number) {
  return new Date(ts).toLocaleTimeString('en-US', {
    hour: 'numeric', minute: '2-digit', second: '2-digit',
  })
}

function formatSize(bytes: number | null) {
  if (bytes == null) return '—'
  const mb = bytes / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`
}

/** Elapsed seconds as m:ss, or h:mm:ss once the trip runs past an hour. */
function formatTimecode(seconds: number) {
  const whole = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0
  const h = Math.floor(whole / 3600)
  const m = Math.floor((whole % 3600) / 60)
  const s = whole % 60
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m)
  return h > 0
    ? `${h}:${mm}:${String(s).padStart(2, '0')}`
    : `${mm}:${String(s).padStart(2, '0')}`
}

/**
 * How long a segment runs on the timeline. duration_s comes from the sidecar
 * the recorder writes at finalize and is accurate to the millisecond; the
 * wall-clock span is the fallback for a clip whose sidecar never landed.
 */
function clipSeconds(clip: DashcamClip): number {
  if (clip.duration_s != null && clip.duration_s > 0) return clip.duration_s
  const span = (Date.parse(clip.ended_at) - Date.parse(clip.started_at)) / 1000
  return Number.isFinite(span) && span > 0 ? span : FALLBACK_CLIP_S
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function ConfirmDelete({ open, onOpenChange, title, description, confirmLabel, onConfirm }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel: string
  onConfirm: () => void
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel asChild>
            <Button variant="outline" size="sm">Cancel</Button>
          </AlertDialogCancel>
          <AlertDialogAction asChild>
            <Button variant="destructive" size="sm" onClick={onConfirm}>{confirmLabel}</Button>
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function ClipRow({ clip, position, active, busy, onSelect, onDelete }: {
  clip: DashcamClip
  position: string
  active: boolean
  busy: boolean
  onSelect: () => void
  onDelete: () => void
}) {
  const pending = clip.state === 'pending_delete'
  return (
    <div className={`flex items-center gap-2 rounded-md px-2 py-1.5 ${active ? 'bg-accent' : ''}`}>
      <button
        type="button"
        onClick={onSelect}
        disabled={pending}
        className="flex-1 min-w-0 flex items-center gap-2 text-left disabled:opacity-50"
      >
        {/* Where this segment starts on the trip timeline, not its own length —
            the timeline is the thing the player scrubs. */}
        <span className="text-[10px] text-muted-foreground tabular-nums w-10 shrink-0">
          {position}
        </span>
        <span className="text-xs tabular-nums">{formatClipTime(clip.started_at)}</span>
        <span className="text-[10px] text-muted-foreground tabular-nums">
          {formatSize(clip.size_bytes)}
        </span>
        {clip.protected === 1 && (
          <Badge variant="secondary" className="text-[9px] px-1 py-0 shrink-0">saved</Badge>
        )}
        {pending && (
          <span className="text-[9px] text-muted-foreground shrink-0">deleting…</span>
        )}
      </button>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 shrink-0 text-muted-foreground hover:text-destructive"
        onClick={onDelete}
        disabled={busy || pending}
        aria-label={`Delete clip from ${formatClipTime(clip.started_at)}`}
      >
        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round"
                d="M6 7h12M9 7V5h6v2m-8 0 1 12h8l1-12" />
        </svg>
      </Button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Section
// ---------------------------------------------------------------------------

export function DashcamSection({ tripId, clips }: { tripId: string; clips: DashcamClip[] }) {
  const { deleteClip, deleteTripVideos, setFootageProtected } = useDashcam()

  // Which clip the element has loaded. Tracked by id rather than index so a
  // delete landing mid-playback cannot silently switch the footage underneath.
  const [activeId, setActiveId]   = useState<string | null>(null)
  const [elapsed, setElapsed]     = useState(0)               // seconds into the active clip
  const [scrub, setScrub]         = useState<number | null>(null) // trip seconds, while dragging
  // Whether playback is MEANT to continue, not whether the element is playing.
  // Swapping the source pauses the element (the load algorithm fires `pause`),
  // so tracking the element would flicker the control at every boundary — which
  // is exactly the segmentation this component exists to hide.
  const [playing, setPlaying]     = useState(false)
  const [failed, setFailed]       = useState<Set<string>>(new Set())
  const [showSegments, setShowSegments] = useState(false)
  // On by default: the raw footage is genuinely cyan, so corrected is the
  // truthful-looking view. Off stays one click away because the stored file is
  // the uncorrected original and it should remain possible to see it.
  const [wbCorrect, setWbCorrect] = useState(true)
  const [fullscreen, setFullscreen]     = useState(false)

  const [busy, setBusy]               = useState<Set<string>>(new Set())
  const [pendingClip, setPendingClip] = useState<DashcamClip | null>(null)
  const [confirmAll, setConfirmAll]   = useState(false)
  const [error, setError]             = useState<string | null>(null)

  const videoRef  = useRef<HTMLVideoElement>(null)
  const shellRef  = useRef<HTMLDivElement>(null)
  const loadedRef = useRef<string | null>(null)   // clip_id currently attached to the element
  const seekToRef = useRef<number | null>(null)   // seconds within the next clip, applied on load
  // Mirrors `playing` for the load handler, which runs from a media event and
  // would otherwise read the value captured when the source was attached.
  const wantPlayRef = useRef(false)

  // A clip on its way out cannot be part of a continuous timeline — the file is
  // about to be unlinked on the Jetson. It stays in the segment list, greyed.
  const playable = useMemo(
    () => clips.filter(c => c.state !== 'pending_delete' && c.state !== 'deleted'),
    [clips],
  )

  // offsets[i] is where segment i starts on the trip timeline.
  const { offsets, total } = useMemo(() => {
    const acc: number[] = []
    let running = 0
    for (const clip of playable) {
      acc.push(running)
      running += clipSeconds(clip)
    }
    return { offsets: acc, total: running }
  }, [playable])

  const found  = playable.findIndex(c => c.clip_id === activeId)
  const index  = found >= 0 ? found : 0
  const active = playable.at(index)
  const globalTime = scrub ?? (offsets[index] ?? 0) + elapsed
  const sliderMax  = Math.max(1, Math.round(total))

  // Attach the source imperatively rather than through a `key`-remount: reusing
  // the same element preserves the user-gesture unlock WebKit grants it on the
  // first play, which is what lets the next segment start on its own at a
  // boundary. A remounted element is a fresh one, and its play() is refused.
  useEffect(() => {
    const video = videoRef.current
    if (!video || !active || loadedRef.current === active.clip_id) return
    loadedRef.current = active.clip_id
    video.src = active.video_url
    video.load()
  }, [active])

  useEffect(() => {
    const sync = () => setFullscreen(document.fullscreenElement === shellRef.current)
    document.addEventListener('fullscreenchange', sync)
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [])

  if (clips.length === 0) return null

  const anyProtected = clips.some(c => c.protected === 1)
  const unavailable  = active == null || failed.has(active.clip_id)
  const positionAt   = active ? Date.parse(active.started_at) + elapsed * 1000 : null

  // -- playback ------------------------------------------------------------

  /** Move to segment `i`, resuming at `within` seconds once it has loaded. */
  function openSegment(i: number, within: number) {
    const clip = playable.at(i)
    if (!clip) return
    seekToRef.current = within
    setElapsed(within)
    setActiveId(clip.clip_id)
  }

  /** Seek the trip timeline, crossing into another segment if it has to. */
  function seek(target: number) {
    if (playable.length === 0) return
    const clamped = Math.max(0, Math.min(target, Math.max(0, total - 0.25)))
    let i = 0
    while (i + 1 < offsets.length && offsets[i + 1] <= clamped) i += 1
    const within = clamped - offsets[i]

    if (playable[i].clip_id === active?.clip_id && videoRef.current) {
      videoRef.current.currentTime = within
      setElapsed(within)
    } else {
      openSegment(i, within)
    }
  }

  /** Next segment that is not known-broken. False when the trip is over. */
  function advance(skip: Set<string>) {
    for (let i = index + 1; i < playable.length; i += 1) {
      if (!skip.has(playable[i].clip_id)) {
        openSegment(i, 0)
        return true
      }
    }
    return false
  }

  function setPlayback(next: boolean) {
    wantPlayRef.current = next
    setPlaying(next)
    // Buffer ahead only once playback is actually wanted. A trip page that is
    // merely open should not pull a segment's ~60 MB over the Pi's link — but
    // once the drive is playing, buffering ahead is what keeps the handover at
    // each boundary from being visible. The element is never remounted, so this
    // sticks for every segment after it.
    if (next && videoRef.current) videoRef.current.preload = 'auto'
  }

  function togglePlay() {
    const video = videoRef.current
    if (!video || !active) return
    if (playing) {
      setPlayback(false)
      video.pause()
      return
    }
    setPlayback(true)

    // Parked on the last frame of the trip — start over from the top rather
    // than doing nothing.
    if (total > 0 && globalTime >= total - 0.5) {
      const first = playable.at(0)
      if (first && first.clip_id !== active.clip_id) {
        openSegment(0, 0)          // the load handler starts it
        return
      }
      video.currentTime = 0
      setElapsed(0)
    }
    video.play().catch(() => setPlayback(false))
  }

  function handleLoadedMetadata() {
    const video = videoRef.current
    if (!video) return
    const target = seekToRef.current
    seekToRef.current = null
    if (target != null) {
      const limit = Number.isFinite(video.duration) ? video.duration - 0.05 : target
      video.currentTime = Math.max(0, Math.min(target, limit))
    }
    if (wantPlayRef.current) video.play().catch(() => setPlayback(false))
  }

  function handleEnded() {
    if (!advance(failed)) setPlayback(false)
  }

  /**
   * A segment the Jetson can no longer serve — purged out from under us, or the
   * dashcam is offline. Skip it and keep the drive playing rather than ending
   * the whole trip on one bad file; only give up when nothing is left.
   */
  function handleError() {
    if (!active) return
    const next = new Set(failed).add(active.clip_id)
    setFailed(next)
    if (!wantPlayRef.current || !advance(next)) setPlayback(false)
  }

  function retry() {
    const video = videoRef.current
    if (!active || !video) return
    setFailed(prev => {
      const remaining = new Set(prev)
      remaining.delete(active.clip_id)
      return remaining
    })
    video.load()
  }

  async function toggleFullscreen() {
    const shell = shellRef.current
    if (!shell) return
    try {
      if (document.fullscreenElement) await document.exitFullscreen()
      else await shell.requestFullscreen()
    } catch {
      // Webview without the Fullscreen API — the button is simply inert.
    }
  }

  // -- mutations -----------------------------------------------------------

  async function run(key: string, fn: () => Promise<void>) {
    setBusy(prev => new Set(prev).add(key))
    setError(null)
    try {
      await fn()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Request failed')
    } finally {
      setBusy(prev => { const s = new Set(prev); s.delete(key); return s })
    }
  }

  async function handleDeleteClip(clip: DashcamClip) {
    setPendingClip(null)
    await run(clip.clip_id, async () => {
      // force=1 only when the clip is protected — the dialog already made that
      // explicit, so the server's 409 guard has been deliberately answered.
      await deleteClip(tripId, clip.clip_id, clip.protected === 1)
      if (active?.clip_id === clip.clip_id) {
        const next = playable.findIndex(c => c.clip_id !== clip.clip_id)
        if (next >= 0) openSegment(next, 0)
        else { setPlayback(false); setActiveId(null) }
      }
    })
  }

  async function handleDeleteAll() {
    setConfirmAll(false)
    await run('__all__', () => deleteTripVideos(tripId, anyProtected))
  }

  // -- render --------------------------------------------------------------

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between px-0.5">
        <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
          Dashcam · {formatTimecode(total)}
        </p>
        <div className="flex items-center gap-1">
          {anyProtected && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 text-[10px] px-2"
              disabled={busy.has('__protect__')}
              onClick={() => run('__protect__', () => setFootageProtected(tripId, false))}
            >
              Release
            </Button>
          )}
          {!anyProtected && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 text-[10px] px-2"
              disabled={busy.has('__protect__')}
              onClick={() => run('__protect__', () => setFootageProtected(tripId, true))}
            >
              Keep
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            className="h-6 text-[10px] px-2 text-muted-foreground hover:text-destructive"
            disabled={busy.has('__all__')}
            onClick={() => setConfirmAll(true)}
          >
            Delete all
          </Button>
        </div>
      </div>

      <div className="rounded-lg border bg-card p-3 space-y-2">
        {anyProtected && (
          <p className="text-[10px] text-muted-foreground leading-relaxed">
            This footage is kept indefinitely because a possible collision was detected.
            It is exempt from the 30-day purge until you delete or release it.
          </p>
        )}

        {/* The player. In fullscreen this element IS the viewport, so it takes
            the black background and centres the frame itself. */}
        <div
          ref={shellRef}
          className="space-y-1 [&:fullscreen]:flex [&:fullscreen]:flex-col
                     [&:fullscreen]:justify-center [&:fullscreen]:bg-black [&:fullscreen]:p-3
                     [&:fullscreen_video]:max-h-full [&:fullscreen_video]:rounded-none"
        >
          <div className="relative">
            <WindscreenFilter />
            <video
              ref={videoRef}
              style={wbCorrect ? { filter: `url(#${WINDSCREEN_FILTER_ID})` } : undefined}
              // No audio branch exists in recorder.py's pipeline, so muting
              // costs nothing — and muted media is exempt from the autoplay
              // gating that would otherwise refuse the play() at each segment
              // boundary in the kiosk's WebKit webview.
              muted
              playsInline
              preload="metadata"
              className="w-full rounded-md bg-black aspect-video object-contain"
              onLoadedMetadata={handleLoadedMetadata}
              onTimeUpdate={e => { if (scrub == null) setElapsed(e.currentTarget.currentTime) }}
              onSeeked={e => setElapsed(e.currentTarget.currentTime)}
              onEnded={handleEnded}
              onError={handleError}
            />
            {unavailable && (
              <div className="absolute inset-0 rounded-md bg-background/95 border border-dashed
                              flex flex-col items-center justify-center gap-1">
                <p className="text-xs text-muted-foreground">Footage unavailable</p>
                <p className="text-[10px] text-muted-foreground">
                  The dashcam may be offline or this clip has been purged
                </p>
                {active && (
                  <Button variant="outline" size="sm" className="h-6 text-[10px] px-2 mt-1"
                          onClick={retry}>
                    Retry
                  </Button>
                )}
              </div>
            )}
          </div>

          {/* One scrubber for the whole trip. The ticks are the segment
              boundaries — visible, but not something you have to click. */}
          <div className="flex items-center gap-2 pt-0.5">
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 shrink-0"
              onClick={togglePlay}
              disabled={playable.length === 0}
              aria-label={playing ? 'Pause' : 'Play'}
            >
              {playing
                ? <IconPlayerPauseFilled className="size-3.5" />
                : <IconPlayerPlayFilled  className="size-3.5" />}
            </Button>

            <Slider
              className="flex-1"
              min={0}
              max={sliderMax}
              step={1}
              value={[Math.max(0, Math.min(Math.round(globalTime), sliderMax))]}
              disabled={playable.length === 0}
              onValueChange={([value]) => setScrub(value)}
              onValueCommit={([value]) => { setScrub(null); seek(value) }}
              aria-label="Seek through this trip's footage"
            >
              {offsets.slice(1).map((offset, i) => (
                <span
                  key={playable[i + 1].clip_id}
                  className="absolute inset-y-0 w-px bg-background/70"
                  style={{ left: `${(offset / total) * 100}%` }}
                />
              ))}
            </Slider>

            <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
              {formatTimecode(globalTime)} / {formatTimecode(total)}
            </span>

            {/* Colour correction is a view setting, not an edit — the stored
                clip is unchanged either way, so this toggles what you see
                rather than what is kept. */}
            <Button
              variant="ghost"
              size="icon"
              className={cn('h-7 w-7 shrink-0', !wbCorrect && 'text-muted-foreground')}
              onClick={() => setWbCorrect(v => !v)}
              aria-pressed={wbCorrect}
              title={wbCorrect
                ? 'Windscreen colour correction on — showing corrected footage'
                : 'Windscreen colour correction off — showing the raw sensor output'}
              aria-label={wbCorrect
                ? 'Turn off windscreen colour correction'
                : 'Turn on windscreen colour correction'}
            >
              <IconColorSwatch className="size-3.5" />
            </Button>

            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 shrink-0"
              onClick={toggleFullscreen}
              aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'}
            >
              {fullscreen
                ? <IconMinimize className="size-3.5" />
                : <IconMaximize className="size-3.5" />}
            </Button>
          </div>

          <p className="text-[10px] text-muted-foreground tabular-nums px-0.5">
            {positionAt != null ? formatClipTime(positionAt) : '—'}
            {active?.width_px ? ` · ${active.width_px}×${active.height_px}` : ''}
            {active?.fps ? ` · ${active.fps}fps` : ''}
            {playable.length > 1 ? ` · segment ${index + 1} of ${playable.length}` : ''}
          </p>
        </div>

        {/* The segments are an implementation detail of how the footage is
            stored, so they are folded away — but deleting is still per-file, so
            they have to stay reachable. */}
        {clips.length > 1 && (
          <div className="border-t pt-2">
            <button
              type="button"
              onClick={() => setShowSegments(v => !v)}
              className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
              aria-expanded={showSegments}
            >
              <IconChevronRight
                className={cn('size-3 transition-transform', showSegments && 'rotate-90')}
              />
              {clips.length} segments
            </button>

            {showSegments && (
              <div className="space-y-0.5 mt-1">
                {clips.map(clip => {
                  const i = playable.findIndex(c => c.clip_id === clip.clip_id)
                  return (
                    <ClipRow
                      key={clip.clip_id}
                      clip={clip}
                      position={i >= 0 ? formatTimecode(offsets[i]) : '—'}
                      active={clip.clip_id === active?.clip_id}
                      busy={busy.has(clip.clip_id)}
                      onSelect={() => { if (i >= 0) seek(offsets[i]) }}
                      onDelete={() => setPendingClip(clip)}
                    />
                  )
                })}
              </div>
            )}
          </div>
        )}

        {error && <p className="text-[10px] text-destructive">{error}</p>}
      </div>

      <ConfirmDelete
        open={pendingClip !== null}
        onOpenChange={(open) => !open && setPendingClip(null)}
        title="Delete this segment?"
        description={pendingClip?.protected === 1
          ? 'This clip was saved because a possible collision was detected during this trip. '
            + 'Deleting it removes the file from the dashcam permanently.'
          : 'This removes the video file from the dashcam permanently.'}
        confirmLabel="Delete"
        onConfirm={() => pendingClip && handleDeleteClip(pendingClip)}
      />

      <ConfirmDelete
        open={confirmAll}
        onOpenChange={setConfirmAll}
        title={`Delete all ${clips.length} segments?`}
        description={anyProtected
          ? 'This trip\'s footage was saved because a possible collision was detected. '
            + 'Deleting it removes every clip from the dashcam permanently.'
          : 'This removes every clip for this trip from the dashcam permanently.'}
        confirmLabel="Delete all"
        onConfirm={handleDeleteAll}
      />
    </div>
  )
}
