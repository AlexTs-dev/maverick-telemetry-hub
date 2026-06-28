// client/src/pages/TripListPage.tsx

import { useNavigate } from 'react-router-dom'
import { useTrips } from '@/contexts/TripContext'
import type { Trip } from '@/contexts/TripContext'
import { Badge }      from '@/components/ui/badge'
import { Button }     from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton }   from '@/components/ui/skeleton'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(ts: string) {
  return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

function formatTime(ts: string) {
  return new Date(ts).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}

function formatDuration(seconds: number | null) {
  if (!seconds) return null
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}m` : `${m} min`
}

function formatDistance(start: number | null, end: number | null) {
  if (start == null || end == null) return null
  return `${(end - start).toFixed(1)} mi`
}

function fmt(n: number | null, decimals = 1) {
  return n != null ? n.toFixed(decimals) : '—'
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col items-center w-14">
      <span className="text-base font-semibold tabular-nums leading-tight">{value}</span>
      <span className="text-[10px] text-muted-foreground uppercase tracking-wider">{label}</span>
    </div>
  )
}

function ChevronRight() {
  return (
    <svg className="w-4 h-4 text-muted-foreground shrink-0" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  )
}

function TripRow({ trip, onTap }: { trip: Trip; onTap: () => void }) {
  const meta = [
    trip.notes,
    formatDuration(trip.duration_seconds),
    formatDistance(trip.odometer_start, trip.odometer_end),
  ].filter(Boolean).join(' · ')

  return (
    <button
      onClick={onTap}
      className="w-full flex items-center gap-3 px-4 text-left
                 min-h-[80px] border-b last:border-b-0
                 hover:bg-muted/50 active:bg-muted transition-colors"
    >
      {/* Date + meta */}
      <div className="flex-1 min-w-0 py-3">
        <div className="flex items-baseline gap-2">
          <span className="text-base font-semibold">{formatDate(trip.started_at)}</span>
          <span className="text-sm text-muted-foreground">{formatTime(trip.started_at)}</span>
        </div>
        {meta && (
          <p className="text-sm text-muted-foreground truncate mt-0.5">{meta}</p>
        )}
      </div>

      {/* Stats */}
      <div className="flex items-center gap-1 shrink-0">
        <Stat label="mpg" value={fmt(trip.avg_fuel_economy_mpg)} />
        <Stat label="mph" value={fmt(trip.avg_speed_mph, 0)} />
      </div>

      {/* DTC badge */}
      <div className="w-16 flex justify-center shrink-0">
        {trip.dtc_count > 0 && (
          <Badge variant="destructive" className="text-xs px-2">
            {trip.dtc_count} DTC
          </Badge>
        )}
      </div>

      <ChevronRight />
    </button>
  )
}

function LoadingSkeleton() {
  return (
    <div className="divide-y">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 min-h-[80px]">
          <div className="flex-1 space-y-2 py-3">
            <Skeleton className="h-4 w-36" />
            <Skeleton className="h-3 w-56" />
          </div>
          <Skeleton className="h-8 w-28" />
        </div>
      ))}
    </div>
  )
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-48 text-muted-foreground gap-2">
      <span className="text-4xl">🛻</span>
      <p className="text-sm">No trips recorded yet</p>
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

export function TripListPage() {
  const { trips, loading, error, refresh } = useTrips()
  const navigate = useNavigate()

  return (
    <div className="flex flex-col h-full">
      {/* Header — 48px */}
      <div className="flex items-center justify-between px-4 h-12 border-b shrink-0">
        <div className="flex items-baseline gap-2">
          <h1 className="text-base font-semibold">Trips</h1>
          {!loading && !error && (
            <span className="text-xs text-muted-foreground">{trips.length} recorded</span>
          )}
        </div>
        <Button variant="ghost" size="sm" onClick={refresh} className="h-8 text-xs">
          Refresh
        </Button>
      </div>

      {/* Column headers — 28px */}
      <div className="flex items-center gap-3 px-4 h-7 border-b bg-muted/30 shrink-0">
        <span className="flex-1 text-[10px] text-muted-foreground uppercase tracking-wider">Trip</span>
        <div className="flex gap-1 shrink-0">
          {['mpg', 'mph'].map(h => (
            <span key={h} className="w-14 text-center text-[10px] text-muted-foreground uppercase tracking-wider">{h}</span>
          ))}
        </div>
        <div className="w-16" />
        <div className="w-4" />
      </div>

      {/* Scrollable list — remainder of 480px */}
      <ScrollArea className="flex-1">
        {loading  ? <LoadingSkeleton /> :
         error    ? <ErrorState message={error} onRetry={refresh} /> :
         trips.length === 0 ? <EmptyState /> : (
          <div>
            {trips.map(trip => (
              <TripRow
                key={trip.id}
                trip={trip}
                onTap={() => navigate(`/trips/${trip.id}`)}
              />
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  )
}
