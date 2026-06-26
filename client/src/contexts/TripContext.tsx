// client/src/contexts/TripContext.tsx
//
// Provides trip list and per-trip data to any component in the tree.
// Fetches from the Express REST API. Does not handle live WebSocket
// data — that lives in WebSocketContext.
//
// Usage:
//   const { trips, loading, error } = useTrips()
//   const { trip, readings, dtcs, loading } = useTrip(id)

import {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  type ReactNode,
} from 'react'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface TripSummary {
  avg_speed_mph:        number | null
  max_speed_mph:        number | null
  avg_rpm:              number | null
  max_coolant_temp_f:   number | null
  ev_time_pct:          number | null
  total_regen_kwh:      number | null
  avg_fuel_economy_mpg: number | null
  min_battery_soc_pct:  number | null
}

export interface Trip extends TripSummary {
  id:               number
  started_at:       string
  ended_at:         string | null
  duration_seconds: number | null
  odometer_start:   number | null
  odometer_end:     number | null
  dtc_count:        number
  notes:            string | null
}

export interface Reading {
  id:              number
  ts:              string
  rpm:             number | null
  speed_mph:       number | null
  coolant_temp_f:  number | null
  throttle_pct:    number | null
  fuel_rate_gph:   number | null
  battery_soc_pct: number | null  // HV traction battery SOC (Ford BECM Mode 22)
  hvb_temp_f:      number | null  // HV pack avg temperature
  pack_voltage_v:  number | null  // HV pack terminal voltage
}

export interface DTC {
  id:               number
  trip_id:          number
  code:             string
  first_seen_at:    string
  claude_diagnosis: string | null
  diagnosed_at:     string | null
  trip_started_at?: string
}

// ---------------------------------------------------------------------------
// Context shape
// ---------------------------------------------------------------------------

interface TripContextValue {
  // Trip list
  trips:        Trip[]
  tripsLoading: boolean
  tripsError:   string | null
  refreshTrips: () => void

  // Per-trip detail — keyed by trip id string
  getTripDetail: (id: string) => {
    trip:     Trip | null
    readings: Reading[]
    dtcs:     DTC[]
    loading:  boolean
    error:    string | null
  }
  fetchTripDetail: (id: string) => void

  // DTC diagnosis
  diagnose: (dtcId: number) => Promise<DTC>
}

// ---------------------------------------------------------------------------
// Internal state shape for per-trip detail cache
// ---------------------------------------------------------------------------

interface TripDetailState {
  trip:     Trip | null
  readings: Reading[]
  dtcs:     DTC[]
  loading:  boolean
  error:    string | null
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

// In a Tauri window the page isn't served from localhost:3000, so relative
// URLs won't reach the Express server. Detect Tauri at runtime and use an
// absolute base; in a regular browser relative URLs work fine.
const TAURI = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const API   = (TAURI ? 'http://localhost:3000' : '') + '/api'

async function apiFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API}${path}`)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.error ?? `HTTP ${res.status}`)
  }
  return res.json()
}

// ---------------------------------------------------------------------------
// Context + provider
// ---------------------------------------------------------------------------

const TripContext = createContext<TripContextValue | null>(null)

export function TripProvider({ children }: { children: ReactNode }) {
  const [trips, setTrips]               = useState<Trip[]>([])
  const [tripsLoading, setTripsLoading] = useState(false)
  const [tripsError, setTripsError]     = useState<string | null>(null)

  // Per-trip detail cache — avoids re-fetching when navigating back
  const [detailCache, setDetailCache] = useState<Record<string, TripDetailState>>({})

  // -------------------------------------------------------------------------
  // Trip list
  // -------------------------------------------------------------------------

  const refreshTrips = useCallback(async () => {
    setTripsLoading(true)
    setTripsError(null)
    try {
      const data = await apiFetch<Trip[]>('/trips')
      setTrips(data)
    } catch (err) {
      setTripsError(err instanceof Error ? err.message : 'Failed to load trips')
    } finally {
      setTripsLoading(false)
    }
  }, [])

  useEffect(() => { refreshTrips() }, [refreshTrips])

  // -------------------------------------------------------------------------
  // Per-trip detail
  // -------------------------------------------------------------------------

  const fetchTripDetail = useCallback(async (id: string) => {
    // Skip if already loading or loaded
    if (detailCache[id]?.loading || detailCache[id]?.trip) return

    setDetailCache(prev => ({
      ...prev,
      [id]: { trip: null, readings: [], dtcs: [], loading: true, error: null },
    }))

    try {
      const [trip, readings, dtcs] = await Promise.all([
        apiFetch<Trip>(`/trips/${id}`),
        apiFetch<Reading[]>(`/trips/${id}/readings`),
        apiFetch<DTC[]>(`/trips/${id}/dtcs`),
      ])

      setDetailCache(prev => ({
        ...prev,
        [id]: { trip, readings, dtcs, loading: false, error: null },
      }))
    } catch (err) {
      setDetailCache(prev => ({
        ...prev,
        [id]: {
          trip: null, readings: [], dtcs: [],
          loading: false,
          error: err instanceof Error ? err.message : 'Failed to load trip',
        },
      }))
    }
  }, [detailCache])

  const getTripDetail = useCallback((id: string): TripDetailState => {
    return detailCache[id] ?? {
      trip: null, readings: [], dtcs: [], loading: false, error: null,
    }
  }, [detailCache])

  // -------------------------------------------------------------------------
  // DTC diagnosis
  // -------------------------------------------------------------------------

  const diagnose = useCallback(async (dtcId: number): Promise<DTC> => {
    const res = await fetch(`${API}/dtcs/${dtcId}/diagnose`, { method: 'POST' })
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new Error(body.error ?? `HTTP ${res.status}`)
    }
    const result = await res.json()

    // Update the cached DTC in any loaded trip detail
    setDetailCache(prev => {
      const next = { ...prev }
      for (const id in next) {
        const detail = next[id]
        if (detail.dtcs.some(d => d.id === dtcId)) {
          next[id] = {
            ...detail,
            dtcs: detail.dtcs.map(d =>
              d.id === dtcId
                ? { ...d, claude_diagnosis: result.diagnosis, diagnosed_at: result.diagnosed_at }
                : d
            ),
          }
        }
      }
      return next
    })

    return result
  }, [])

  // -------------------------------------------------------------------------
  // Value
  // -------------------------------------------------------------------------

  return (
    <TripContext.Provider value={{
      trips,
      tripsLoading,
      tripsError,
      refreshTrips,
      getTripDetail,
      fetchTripDetail,
      diagnose,
    }}>
      {children}
    </TripContext.Provider>
  )
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

export function useTrips() {
  const ctx = useContext(TripContext)
  if (!ctx) throw new Error('useTrips must be used within TripProvider')
  return {
    trips:   ctx.trips,
    loading: ctx.tripsLoading,
    error:   ctx.tripsError,
    refresh: ctx.refreshTrips,
  }
}

export function useTrip(id: string) {
  const ctx = useContext(TripContext)
  if (!ctx) throw new Error('useTrip must be used within TripProvider')

  useEffect(() => {
    ctx.fetchTripDetail(id)
  }, [id])

  return ctx.getTripDetail(id)
}

export function useDiagnose() {
  const ctx = useContext(TripContext)
  if (!ctx) throw new Error('useDiagnose must be used within TripProvider')
  return ctx.diagnose
}
