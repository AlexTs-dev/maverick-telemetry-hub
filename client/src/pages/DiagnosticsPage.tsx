import { useEffect, useState } from 'react'
import { useDiagnose, type DTC } from '@/contexts/TripContext'
import { Badge }      from '@/components/ui/badge'
import { Button }     from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton }   from '@/components/ui/skeleton'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'

// In a Tauri window the page isn't served from localhost:3000, so relative
// URLs won't reach the Express server. Mirrors TripContext's helper — this
// page previously used a bare fetch('/api/dtcs'), which is broken in the kiosk.
const TAURI = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const API   = (TAURI ? 'http://localhost:3000' : '') + '/api'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(ts: string) {
  return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function formatBytes(bytes: number | null | undefined) {
  if (bytes == null) return '—'
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  return `${(bytes / 1024 ** 2).toFixed(0)} MB`
}

interface DashcamStatus {
  clip_count:       number
  bytes_used:       number
  protected_count:  number
  protected_bytes:  number
  unassigned_count: number
  unassigned_bytes: number
  oldest_clip_at:   string | null
  newest_clip_at:   string | null
  jetson: null | {
    status?:           string
    recording?:        boolean
    storage_pressure?: boolean
    stale?:            boolean
    disk_total_bytes?: number
    disk_free_bytes?:  number
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function DtcCard({ dtc, onDiagnose, busy }: { dtc: DTC; onDiagnose: () => void; busy: boolean }) {
  return (
    <div className="rounded-lg border bg-card p-4 space-y-3">
      {/* Top row */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <Badge variant="destructive" className="shrink-0 text-sm px-2.5 py-0.5">
            {dtc.code}
          </Badge>
          <div className="min-w-0">
            <p className="text-xs text-muted-foreground truncate">
              First seen {formatDate(dtc.first_seen_at)}
              {dtc.trip_started_at && ` · Trip ${formatDate(dtc.trip_started_at)}`}
            </p>
          </div>
        </div>

        {dtc.claude_diagnosis ? (
          <span className="text-[10px] text-muted-foreground shrink-0">
            Diagnosed {formatDate(dtc.diagnosed_at!)}
          </span>
        ) : (
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-xs shrink-0"
            onClick={onDiagnose}
            disabled={busy}
          >
            {busy ? (
              <span className="flex items-center gap-1.5">
                <Spinner /> Diagnosing…
              </span>
            ) : (
              'Ask Claude'
            )}
          </Button>
        )}
      </div>

      {/* Diagnosis text */}
      {dtc.claude_diagnosis && (
        <p className="text-sm text-foreground/80 leading-relaxed border-t pt-3">
          {dtc.claude_diagnosis}
        </p>
      )}
    </div>
  )
}

function Spinner() {
  return (
    <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Dashcam storage
//
// The only place the dashcam's disk usage is visible, and the only way to reach
// footage that matched no trip — the trip detail page is organised by trip, so
// unassigned clips would otherwise be undeletable from the UI entirely.
// ---------------------------------------------------------------------------

function StorageRow({ label, value, dim }: { label: string; value: string; dim?: boolean }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="text-[10px] text-muted-foreground uppercase tracking-wider">{label}</span>
      <span className={`text-xs tabular-nums ${dim ? 'text-muted-foreground' : ''}`}>{value}</span>
    </div>
  )
}

function DashcamCard({ status, onDeleteUnassigned, busy }: {
  status: DashcamStatus | null
  onDeleteUnassigned: () => void
  busy: boolean
}) {
  const [confirm, setConfirm] = useState(false)
  if (!status) return null

  const jetson = status.jetson
  const offline = !jetson || jetson.stale
  const freePct = jetson?.disk_total_bytes
    ? Math.round(((jetson.disk_free_bytes ?? 0) / jetson.disk_total_bytes) * 100)
    : null

  return (
    <section className="space-y-2">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider px-0.5">
        Dashcam storage
      </p>

      <div className="rounded-lg border bg-card p-3 space-y-1.5">
        <div className="flex items-center justify-between pb-1">
          <span className="text-sm font-semibold">
            {status.clip_count} clip{status.clip_count === 1 ? '' : 's'}
          </span>
          <Badge variant={offline ? 'destructive' : 'secondary'} className="text-[10px]">
            {offline ? 'Dashcam offline' : jetson?.recording ? 'Recording' : 'Idle'}
          </Badge>
        </div>

        {jetson?.storage_pressure && (
          <p className="text-[10px] text-destructive leading-relaxed pb-1">
            Disk is full and only saved (crash) footage remains. Nothing more can be
            purged automatically — delete some saved footage to free space.
          </p>
        )}

        <StorageRow label="Footage" value={formatBytes(status.bytes_used)} />
        <StorageRow
          label="Saved (crash)"
          value={`${status.protected_count} · ${formatBytes(status.protected_bytes)}`}
          dim={status.protected_count === 0}
        />
        {freePct != null && (
          <StorageRow
            label="Disk free"
            value={`${formatBytes(jetson?.disk_free_bytes)} (${freePct}%)`}
          />
        )}
        {status.oldest_clip_at && (
          <StorageRow label="Oldest" value={formatDate(status.oldest_clip_at)} dim />
        )}

        {status.unassigned_count > 0 && (
          <div className="flex items-center justify-between gap-2 border-t pt-2 mt-1">
            <div className="min-w-0">
              <p className="text-xs">
                {status.unassigned_count} unassigned · {formatBytes(status.unassigned_bytes)}
              </p>
              <p className="text-[10px] text-muted-foreground">
                Recorded outside any trip
              </p>
            </div>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-[10px] shrink-0 text-muted-foreground hover:text-destructive"
              disabled={busy}
              onClick={() => setConfirm(true)}
            >
              {busy ? 'Deleting…' : 'Delete'}
            </Button>
          </div>
        )}
      </div>

      <AlertDialog open={confirm} onOpenChange={setConfirm}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete unassigned footage?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes {status.unassigned_count} clip
              {status.unassigned_count === 1 ? '' : 's'} ({formatBytes(status.unassigned_bytes)})
              that matched no trip. Saved crash footage is never included.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel asChild>
              <Button variant="outline" size="sm">Cancel</Button>
            </AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button
                variant="destructive"
                size="sm"
                onClick={() => { setConfirm(false); onDeleteUnassigned() }}
              >
                Delete
              </Button>
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}

function LoadingSkeleton() {
  return (
    <div className="p-3 space-y-3">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="rounded-lg border bg-card p-4 space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Skeleton className="h-6 w-16 rounded-full" />
              <Skeleton className="h-3 w-40" />
            </div>
            <Skeleton className="h-8 w-24 rounded-md" />
          </div>
        </div>
      ))}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-48 gap-3 text-muted-foreground">
      <svg className="w-10 h-10 opacity-40" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
      </svg>
      <p className="text-sm">No fault codes recorded</p>
    </div>
  )
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center h-48 gap-3">
      <p className="text-sm text-destructive">{message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>Retry</Button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function DiagnosticsPage() {
  const [dtcs,      setDtcs]      = useState<DTC[]>([])
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState<string | null>(null)
  const [diagnosing, setDiagnosing] = useState<Set<number>>(new Set())
  const [dashcam,   setDashcam]   = useState<DashcamStatus | null>(null)
  const [purging,   setPurging]   = useState(false)
  const diagnose = useDiagnose()

  function load() {
    setLoading(true)
    setError(null)
    fetch(`${API}/dtcs`)
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(setDtcs)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))

    // Independent of the DTC list: a dashcam that cannot be reached must not
    // blank out the fault codes, so this failure is swallowed to null.
    fetch(`${API}/dashcam/status`)
      .then(r => (r.ok ? r.json() : null))
      .then(setDashcam)
      .catch(() => setDashcam(null))
  }

  useEffect(load, [])

  async function handleDeleteUnassigned() {
    setPurging(true)
    try {
      const res = await fetch(`${API}/videos/unassigned`, { method: 'DELETE' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      // The delete is a round trip through MQTT and the Jetson, so the counts
      // are only true once db_writer has acted — clear them optimistically and
      // let the next load reconcile.
      setDashcam(prev => (prev ? { ...prev, unassigned_count: 0, unassigned_bytes: 0 } : prev))
    } catch {
      /* surfaced on the next load — a failed purge leaves the counts intact */
    } finally {
      setPurging(false)
    }
  }

  async function handleDiagnose(dtcId: number) {
    setDiagnosing(prev => new Set(prev).add(dtcId))
    try {
      const result = await diagnose(dtcId) as any
      setDtcs(prev => prev.map(d =>
        d.id === dtcId
          ? { ...d, claude_diagnosis: result.diagnosis ?? result.claude_diagnosis, diagnosed_at: result.diagnosed_at }
          : d
      ))
    } finally {
      setDiagnosing(prev => { const s = new Set(prev); s.delete(dtcId); return s })
    }
  }

  const undiagnosed = dtcs.filter(d => !d.claude_diagnosis)
  const diagnosed   = dtcs.filter(d =>  d.claude_diagnosis)

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 h-12 border-b shrink-0">
        <div className="flex items-baseline gap-2">
          <h1 className="text-base font-semibold">Diagnostics</h1>
          {!loading && !error && dtcs.length > 0 && (
            <span className="text-xs text-muted-foreground">{dtcs.length} fault codes</span>
          )}
        </div>
        {undiagnosed.length > 0 && (
          <Badge variant="destructive">{undiagnosed.length} undiagnosed</Badge>
        )}
      </div>

      {/* Body */}
      {loading ? (
        <LoadingSkeleton />
      ) : error ? (
        <ErrorState message={error} onRetry={load} />
      ) : (
        <ScrollArea className="flex-1">
          <div className="p-3 space-y-4">
            <DashcamCard
              status={dashcam}
              busy={purging}
              onDeleteUnassigned={handleDeleteUnassigned}
            />

            {dtcs.length === 0 && <EmptyState />}

            {/* Undiagnosed — action items first */}
            {undiagnosed.length > 0 && (
              <section className="space-y-2">
                <p className="text-[10px] text-muted-foreground uppercase tracking-wider px-0.5">
                  Needs diagnosis
                </p>
                {undiagnosed.map(dtc => (
                  <DtcCard
                    key={dtc.id}
                    dtc={dtc}
                    onDiagnose={() => handleDiagnose(dtc.id)}
                    busy={diagnosing.has(dtc.id)}
                  />
                ))}
              </section>
            )}

            {/* Diagnosed */}
            {diagnosed.length > 0 && (
              <section className="space-y-2">
                <p className="text-[10px] text-muted-foreground uppercase tracking-wider px-0.5">
                  Diagnosed
                </p>
                {diagnosed.map(dtc => (
                  <DtcCard
                    key={dtc.id}
                    dtc={dtc}
                    onDiagnose={() => handleDiagnose(dtc.id)}
                    busy={diagnosing.has(dtc.id)}
                  />
                ))}
              </section>
            )}
          </div>
        </ScrollArea>
      )}
    </div>
  )
}
