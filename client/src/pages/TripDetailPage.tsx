import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useTrip, useDiagnose } from '@/contexts/TripContext'
import type { Reading, DTC } from '@/contexts/TripContext'
import { Badge }      from '@/components/ui/badge'
import { Button }     from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton }   from '@/components/ui/skeleton'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(ts: string) {
  return new Date(ts).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' })
}
function formatTime(ts: string) {
  return new Date(ts).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}
function formatDuration(sec: number | null) {
  if (!sec) return '—'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m} min`
}
function formatDistance(start: number | null, end: number | null) {
  if (start == null || end == null) return '—'
  return `${(end - start).toFixed(1)} mi`
}
function fmt(n: number | null, decimals = 1, suffix = '') {
  return n != null ? `${n.toFixed(decimals)}${suffix}` : '—'
}

// ---------------------------------------------------------------------------
// Sparkline
// ---------------------------------------------------------------------------

function Sparkline({ values, stroke = 'var(--primary)' }: { values: number[]; stroke?: string }) {
  if (values.length < 2) {
    return (
      <div className="h-12 flex items-center justify-center text-[10px] text-muted-foreground">
        no data
      </div>
    )
  }
  const W = 300, H = 48, pad = 2
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * W
      const y = H - pad - ((v - min) / range) * (H - pad * 2)
      return `${x},${y}`
    })
    .join(' ')
  return (
    <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatCell({ label, value, dim }: { label: string; value: string; dim?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-2 px-1 gap-0.5 bg-background">
      <span className="text-sm font-semibold tabular-nums leading-tight">{value}</span>
      {dim && <span className="text-[9px] text-muted-foreground tabular-nums">{dim}</span>}
      <span className="text-[9px] text-muted-foreground uppercase tracking-wider">{label}</span>
    </div>
  )
}

function ChartCard({ title, min, max, children }: {
  title: string; min?: string; max?: string; children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border bg-card p-3 flex flex-col gap-1">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-muted-foreground uppercase tracking-wider">{title}</span>
        {(min != null || max != null) && (
          <span className="text-[10px] text-muted-foreground tabular-nums">
            {min} – {max}
          </span>
        )}
      </div>
      {children}
    </div>
  )
}

function DtcCard({ dtc, onDiagnose, busy }: { dtc: DTC; onDiagnose: () => void; busy: boolean }) {
  return (
    <div className="rounded-lg border bg-card p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Badge variant="destructive">{dtc.code}</Badge>
          <span className="text-xs text-muted-foreground">
            {new Date(dtc.first_seen_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
          </span>
        </div>
        {!dtc.claude_diagnosis && (
          <Button variant="outline" size="sm" className="h-7 text-xs shrink-0" onClick={onDiagnose} disabled={busy}>
            {busy ? 'Diagnosing…' : 'Diagnose'}
          </Button>
        )}
        {dtc.claude_diagnosis && (
          <span className="text-[10px] text-muted-foreground shrink-0">
            {new Date(dtc.diagnosed_at!).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
          </span>
        )}
      </div>
      {dtc.claude_diagnosis && (
        <p className="text-xs text-foreground/80 leading-relaxed">{dtc.claude_diagnosis}</p>
      )}
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <>
      <div className="grid grid-cols-4 gap-px bg-border border-b shrink-0">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="bg-background flex flex-col items-center py-2 gap-1">
            <Skeleton className="h-4 w-12" />
            <Skeleton className="h-2.5 w-8" />
          </div>
        ))}
      </div>
      <div className="flex-1 p-3 grid grid-cols-2 gap-3 content-start">
        {Array.from({ length: 2 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-lg" />
        ))}
      </div>
    </>
  )
}

function ErrorState({ message, onBack }: { message: string; onBack: () => void }) {
  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-3">
      <p className="text-sm text-destructive">{message}</p>
      <Button variant="outline" size="sm" onClick={onBack}>Go back</Button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Derived stats from readings
// ---------------------------------------------------------------------------

function pluck(readings: Reading[], key: keyof Reading): number[] {
  return readings.map(r => r[key] as number | null).filter((v): v is number => v != null)
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function TripDetailPage() {
  const { id }                           = useParams<{ id: string }>()
  const { trip, readings, dtcs, loading, error } = useTrip(id!)
  const diagnose                         = useDiagnose()
  const navigate                         = useNavigate()

  const [diagnosing, setDiagnosing] = useState<Set<number>>(new Set())

  async function handleDiagnose(dtcId: number) {
    setDiagnosing(prev => new Set(prev).add(dtcId))
    try {
      await diagnose(dtcId)
    } finally {
      setDiagnosing(prev => { const s = new Set(prev); s.delete(dtcId); return s })
    }
  }

  const speedVals = pluck(readings, 'speed_mph')
  const rpmVals   = pluck(readings, 'rpm')
  const socVals   = pluck(readings, 'battery_soc_pct')
  const tempVals  = pluck(readings, 'hvb_temp_f')
  const maxTemp   = tempVals.length ? Math.max(...tempVals) : null

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 h-12 border-b shrink-0">
        <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={() => navigate(-1)}>
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="m15 18-6-6 6-6" />
          </svg>
        </Button>
        {trip ? (
          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-2">
              <h1 className="text-base font-semibold">{formatDate(trip.started_at)}</h1>
              <span className="text-sm text-muted-foreground">{formatTime(trip.started_at)}</span>
            </div>
            <p className="text-xs text-muted-foreground">
              {formatDuration(trip.duration_seconds)} · {formatDistance(trip.odometer_start, trip.odometer_end)}
            </p>
          </div>
        ) : (
          <div className="flex-1 space-y-1">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-24" />
          </div>
        )}
        {dtcs.length > 0 && (
          <Badge variant="destructive" className="shrink-0">{dtcs.length} DTC</Badge>
        )}
      </div>

      {/* Loading / error */}
      {loading && <LoadingSkeleton />}
      {!loading && error && <ErrorState message={error} onBack={() => navigate(-1)} />}

      {/* Content */}
      {!loading && !error && trip && (
        <>
          {/* Stats grid — 2 rows × 4 cols */}
          <div className="grid grid-cols-4 gap-px bg-border border-b shrink-0">
            <StatCell label="mpg"       value={fmt(trip.avg_fuel_economy_mpg)} />
            <StatCell label="avg spd"   value={fmt(trip.avg_speed_mph, 0, ' mph')} />
            <StatCell label="max spd"   value={fmt(trip.max_speed_mph, 0, ' mph')} />
            <StatCell label="avg rpm"   value={fmt(trip.avg_rpm, 0)} />
            <StatCell label="coolant"   value={fmt(trip.max_coolant_temp_f, 0, '°F')} />
            <StatCell label="batt temp" value={fmt(maxTemp, 0, '°F')} />
            <StatCell label="min soc"   value={fmt(trip.min_battery_soc_pct, 0, '%')} />
            <StatCell label="dist"      value={formatDistance(trip.odometer_start, trip.odometer_end)} />
          </div>

          {/* Scrollable body */}
          <ScrollArea className="flex-1">
            <div className="p-3 space-y-3">

              {/* Charts */}
              {readings.length > 0 && (
                <div className="grid grid-cols-2 gap-3">
                  <ChartCard
                    title="Speed (mph)"
                    min={speedVals.length ? `${Math.min(...speedVals).toFixed(0)}` : undefined}
                    max={speedVals.length ? `${Math.max(...speedVals).toFixed(0)}` : undefined}
                  >
                    <Sparkline values={speedVals} stroke="var(--chart-1)" />
                  </ChartCard>

                  <ChartCard
                    title="Engine RPM"
                    min={rpmVals.length ? `${Math.min(...rpmVals).toFixed(0)}` : undefined}
                    max={rpmVals.length ? `${Math.max(...rpmVals).toFixed(0)}` : undefined}
                  >
                    <Sparkline values={rpmVals} stroke="var(--chart-3)" />
                  </ChartCard>

                  <ChartCard
                    title="Battery SOC (%)"
                    min={socVals.length ? `${Math.min(...socVals).toFixed(0)}` : undefined}
                    max={socVals.length ? `${Math.max(...socVals).toFixed(0)}` : undefined}
                  >
                    <Sparkline values={socVals} stroke="var(--chart-2)" />
                  </ChartCard>

                  <ChartCard
                    title="Battery temp (°F)"
                    min={tempVals.length ? `${Math.min(...tempVals).toFixed(0)}` : undefined}
                    max={tempVals.length ? `${Math.max(...tempVals).toFixed(0)}` : undefined}
                  >
                    <Sparkline values={tempVals} stroke="var(--chart-4)" />
                  </ChartCard>
                </div>
              )}

              {/* DTCs */}
              {dtcs.length > 0 && (
                <div className="space-y-2">
                  <p className="text-[10px] text-muted-foreground uppercase tracking-wider px-0.5">
                    Fault Codes
                  </p>
                  {dtcs.map(dtc => (
                    <DtcCard
                      key={dtc.id}
                      dtc={dtc}
                      onDiagnose={() => handleDiagnose(dtc.id)}
                      busy={diagnosing.has(dtc.id)}
                    />
                  ))}
                </div>
              )}

              {/* Notes */}
              {trip.notes && (
                <div className="rounded-lg border bg-card p-3">
                  <p className="text-[10px] text-muted-foreground uppercase tracking-wider mb-1">Notes</p>
                  <p className="text-sm">{trip.notes}</p>
                </div>
              )}
            </div>
          </ScrollArea>
        </>
      )}
    </div>
  )
}
