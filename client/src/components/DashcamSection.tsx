// client/src/components/DashcamSection.tsx
//
// Dashcam footage for one trip: a player, the clip list, and deletion.
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

import { useState } from 'react'
import type { DashcamClip } from '@/contexts/TripContext'
import { useDashcam } from '@/contexts/TripContext'
import { Badge }  from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatClipTime(ts: string) {
  return new Date(ts).toLocaleTimeString('en-US', {
    hour: 'numeric', minute: '2-digit', second: '2-digit',
  })
}

function formatSize(bytes: number | null) {
  if (bytes == null) return '—'
  const mb = bytes / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(0)} MB`
}

function formatClipDuration(seconds: number | null) {
  if (seconds == null) return '—'
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${s}s`
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

function ClipRow({ clip, active, busy, onSelect, onDelete }: {
  clip: DashcamClip
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
        <span className="text-xs tabular-nums">{formatClipTime(clip.started_at)}</span>
        <span className="text-[10px] text-muted-foreground tabular-nums">
          {formatClipDuration(clip.duration_s)} · {formatSize(clip.size_bytes)}
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

  const [selected, setSelected] = useState<string | null>(clips[0]?.clip_id ?? null)
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [pendingClip, setPendingClip] = useState<DashcamClip | null>(null)
  const [confirmAll, setConfirmAll] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [playbackFailed, setPlaybackFailed] = useState(false)

  if (clips.length === 0) return null

  const active = clips.find(c => c.clip_id === selected) ?? clips[0]
  const anyProtected = clips.some(c => c.protected === 1)

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
      if (selected === clip.clip_id) {
        setSelected(clips.find(c => c.clip_id !== clip.clip_id)?.clip_id ?? null)
      }
    })
  }

  async function handleDeleteAll() {
    setConfirmAll(false)
    await run('__all__', () => deleteTripVideos(tripId, anyProtected))
  }

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between px-0.5">
        <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
          Dashcam · {clips.length} clip{clips.length === 1 ? '' : 's'}
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

        {active && (
          <div className="space-y-1">
            {playbackFailed ? (
              <div className="aspect-video w-full rounded-md bg-background border border-dashed
                              flex flex-col items-center justify-center gap-1">
                <p className="text-xs text-muted-foreground">Footage unavailable</p>
                <p className="text-[10px] text-muted-foreground">
                  The dashcam may be offline or this clip has been purged
                </p>
              </div>
            ) : (
              <video
                key={active.clip_id}
                src={active.video_url}
                controls
                playsInline
                preload="metadata"
                className="w-full rounded-md bg-black aspect-video"
                onError={() => setPlaybackFailed(true)}
              />
            )}
            <p className="text-[10px] text-muted-foreground tabular-nums px-0.5">
              {formatClipTime(active.started_at)}
              {active.width_px ? ` · ${active.width_px}×${active.height_px}` : ''}
              {active.fps ? ` · ${active.fps}fps` : ''}
            </p>
          </div>
        )}

        {clips.length > 1 && (
          <div className="space-y-0.5 border-t pt-2">
            {clips.map(clip => (
              <ClipRow
                key={clip.clip_id}
                clip={clip}
                active={clip.clip_id === active?.clip_id}
                busy={busy.has(clip.clip_id)}
                onSelect={() => { setSelected(clip.clip_id); setPlaybackFailed(false) }}
                onDelete={() => setPendingClip(clip)}
              />
            ))}
          </div>
        )}

        {error && <p className="text-[10px] text-destructive">{error}</p>}
      </div>

      <ConfirmDelete
        open={pendingClip !== null}
        onOpenChange={(open) => !open && setPendingClip(null)}
        title="Delete this clip?"
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
        title={`Delete all ${clips.length} clips?`}
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
