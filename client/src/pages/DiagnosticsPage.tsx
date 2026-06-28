import { useEffect, useState } from 'react'
import { useDiagnose, type DTC } from '@/contexts/TripContext'
import { Badge }      from '@/components/ui/badge'
import { Button }     from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton }   from '@/components/ui/skeleton'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(ts: string) {
  return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
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
  const diagnose = useDiagnose()

  function load() {
    setLoading(true)
    setError(null)
    fetch('/api/dtcs')
      .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(setDtcs)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

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
          <h1 className="text-base font-semibold">Fault Codes</h1>
          {!loading && !error && dtcs.length > 0 && (
            <span className="text-xs text-muted-foreground">{dtcs.length} total</span>
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
      ) : dtcs.length === 0 ? (
        <EmptyState />
      ) : (
        <ScrollArea className="flex-1">
          <div className="p-3 space-y-4">
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
