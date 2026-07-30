// client/src/contexts/TripContext.tsx
//
// Provides trip list and per-trip data to any component in the tree.
// Fetches from the Express REST API. Does not handle live WebSocket
// data — that lives in WebSocketContext.
//
// Usage:
//   const { trips, loading, error } = useTrips()
//   const { trip, readings, dtcs, videos, crashEvents, loading } = useTrip(id)
//   const { deleteClip, deleteTripVideos, setFootageProtected } = useDashcam()

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
  id:                number
  started_at:        string
  ended_at:          string | null
  duration_seconds:  number | null
  odometer_start:    number | null
  odometer_end:      number | null
  dtc_count:         number
  crash_count:       number
  footage_protected: number   // 0 | 1 — dashcam footage exempt from the purge
  clip_count:        number   // dashcam clips still on the Jetson
  notes:             string | null
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
  battery_current_a: number | null  // HV pack current; negative = charging/regen
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

// A dashcam segment. The file lives on the Jetson — video_url points at the
// Express proxy, which range-forwards to it, so the browser only ever talks to
// the Pi. `protected` clips are exempt from the rolling retention purge.
export interface DashcamClip {
  id:         number
  clip_id:    string
  trip_id?:   number | null
  started_at: string
  ended_at:   string
  duration_s: number | null
  size_bytes: number | null
  width_px:   number | null
  height_px:  number | null
  fps:        number | null
  protected:  number          // 0 | 1 — SQLite has no boolean
  state:      'available' | 'pending_delete' | 'deleted' | 'missing'
  video_url:  string
}

// A confirmed detection from the Jetson's vision pipeline, with the JPEG the
// Pi persisted for it. scene_label prefixed "speed_limit_" came from the
// speed-limit sign track; anything else is a whole-frame scene label.
export interface VisionFrame {
  id:           number
  ts:           string
  frame_id:     string | null
  source:       string
  width_px:     number | null
  height_px:    number | null
  scene_label:  string | null
  confidence:   number | null
  snapshot_url: string | null
}

// A hard deceleration detected from the OBD speed stream.
//   hard_brake      — logged only, protects nothing
//   potential_crash — also protects this trip's footage from the purge
export interface CrashEvent {
  id:               number
  ts:               string
  severity:         'hard_brake' | 'potential_crash'
  source:           string
  peak_decel_g:     number | null
  speed_before_mph: number | null
  speed_after_mph:  number | null
  detail:           string | null
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
  getTripDetail: (id: string) => TripDetailState
  fetchTripDetail: (id: string) => void

  // DTC diagnosis
  diagnose: (dtcId: number) => Promise<DTC>

  // Dashcam
  deleteClip:      (tripId: string, clipId: string, force?: boolean) => Promise<void>
  deleteTripVideos: (tripId: string, force?: boolean) => Promise<void>
  setFootageProtected: (tripId: string, isProtected: boolean) => Promise<void>
}

// ---------------------------------------------------------------------------
// Internal state shape for per-trip detail cache
// ---------------------------------------------------------------------------

interface TripDetailState {
  trip:        Trip | null
  readings:    Reading[]
  dtcs:        DTC[]
  videos:      DashcamClip[]
  crashEvents: CrashEvent[]
  vision:      VisionFrame[]
  loading:     boolean
  error:       string | null
}

const EMPTY_DETAIL: TripDetailState = {
  trip: null, readings: [], dtcs: [], videos: [], crashEvents: [], vision: [],
  loading: false, error: null,
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
      [id]: { ...EMPTY_DETAIL, loading: true },
    }))

    try {
      const [trip, readings, dtcs, videos, crashEvents, vision] = await Promise.all([
        apiFetch<Trip>(`/trips/${id}`),
        apiFetch<Reading[]>(`/trips/${id}/readings`),
        apiFetch<DTC[]>(`/trips/${id}/dtcs`),
        apiFetch<DashcamClip[]>(`/trips/${id}/videos`),
        apiFetch<CrashEvent[]>(`/trips/${id}/crash-events`),
        apiFetch<VisionFrame[]>(`/trips/${id}/vision`),
      ])

      setDetailCache(prev => ({
        ...prev,
        [id]: { trip, readings, dtcs, videos, crashEvents, vision, loading: false, error: null },
      }))
    } catch (err) {
      setDetailCache(prev => ({
        ...prev,
        [id]: {
          ...EMPTY_DETAIL,
          error: err instanceof Error ? err.message : 'Failed to load trip',
        },
      }))
    }
  }, [detailCache])

  const getTripDetail = useCallback((id: string): TripDetailState => {
    return detailCache[id] ?? EMPTY_DETAIL
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
  // Dashcam
  //
  // Deletes are asynchronous end to end: the bridge answers 202 and publishes
  // an MQTT command, db_writer marks the row pending_delete, and the Jetson
  // removes the file before the row finally disappears. The detail cache never
  // refetches and has no invalidation API, so these patch it in place — the
  // same approach diagnose() takes above.
  // -------------------------------------------------------------------------

  const patchDetail = useCallback((
    tripId: string,
    fn: (detail: TripDetailState) => TripDetailState,
  ) => {
    setDetailCache(prev => (prev[tripId] ? { ...prev, [tripId]: fn(prev[tripId]) } : prev))
  }, [])

  const mutate = useCallback(async (path: string, init: RequestInit) => {
    const res = await fetch(`${API}${path}`, init)
    if (!res.ok) {
      const body = await res.json().catch(() => ({}))
      throw new Error(body.error ?? `HTTP ${res.status}`)
    }
    return res.json()
  }, [])

  const deleteClip = useCallback(async (tripId: string, clipId: string, force = false) => {
    await mutate(`/videos/${clipId}${force ? '?force=1' : ''}`, { method: 'DELETE' })
    patchDetail(tripId, d => ({ ...d, videos: d.videos.filter(v => v.clip_id !== clipId) }))
  }, [mutate, patchDetail])

  const deleteTripVideos = useCallback(async (tripId: string, force = false) => {
    await mutate(`/trips/${tripId}/videos${force ? '?force=1' : ''}`, { method: 'DELETE' })
    patchDetail(tripId, d => ({ ...d, videos: [] }))
  }, [mutate, patchDetail])

  const setFootageProtected = useCallback(async (tripId: string, isProtected: boolean) => {
    await mutate(`/trips/${tripId}/videos/protect`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ protected: isProtected }),
    })
    patchDetail(tripId, d => ({
      ...d,
      videos: d.videos.map(v => ({ ...v, protected: isProtected ? 1 : 0 })),
    }))
  }, [mutate, patchDetail])

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
      deleteClip,
      deleteTripVideos,
      setFootageProtected,
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

export function useDashcam() {
  const ctx = useContext(TripContext)
  if (!ctx) throw new Error('useDashcam must be used within TripProvider')
  return {
    deleteClip:          ctx.deleteClip,
    deleteTripVideos:    ctx.deleteTripVideos,
    setFootageProtected: ctx.setFootageProtected,
  }
}
