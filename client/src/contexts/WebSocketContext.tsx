// client/src/contexts/WebSocketContext.tsx
//
// Manages the WebSocket connection to the Express bridge.
// Parses incoming MQTT messages and maintains a rolling D3-ready
// buffer of recent readings for live visualization.
//
// Usage:
//   const { connected, lastReading, readings, pollerStatus } = useLiveTelemetry()

import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  useCallback,
  type ReactNode,
} from 'react'
import type { Reading } from './TripContext'
import { speedLimitValue } from '@/lib/utils'

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// How many readings to keep in the rolling buffer.
// At 1Hz this is 5 minutes of live data for D3 charts.
const BUFFER_SIZE = 300

const TAURI  = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const WS_URL =
  import.meta.env.VITE_WS_URL ??
  (TAURI
    ? 'ws://localhost:3000'
    : typeof window !== 'undefined'
    ? `ws://${window.location.host}`
    : 'ws://localhost:3000')

// In a Tauri window the page isn't served from localhost:3000, so relative
// URLs won't reach the Express server. Same pattern as TripContext/useVersion.
const API = (TAURI ? 'http://localhost:3000' : '') + '/api'

// Reconnect backoff — doubles each attempt up to MAX_BACKOFF
const INITIAL_BACKOFF = 1000  // ms
const MAX_BACKOFF     = 30000 // ms

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type PollerStatus = 'connected' | 'connecting' | 'disconnected' | 'unknown'

export interface LiveReading extends Reading {
  // Parsed Date object for D3 time scales
  date: Date
}

// The last speed-limit sign the Jetson confirmed. Held rather than streamed:
// the vision pipeline publishes on change only and never re-publishes a value
// it already holds, so this stays valid — and worth showing — long after the
// message that carried it. date is the capture time, which is what the UI ages
// the sighting by.
export interface SpeedLimitSighting {
  value:      string        // the number on the sign, e.g. "35"
  confidence: number | null // 0-1 fraction, ML convention
  ts:         string
  date:       Date
}

interface WebSocketContextValue {
  // Connection state
  connected:     boolean
  pollerStatus:  PollerStatus

  // Most recent single reading — for stat displays
  lastReading:   LiveReading | null

  // Rolling buffer of recent readings — for D3 charts
  // Array is always sorted chronologically, max length BUFFER_SIZE
  readings:      LiveReading[]

  // Active trip info from MQTT events
  activeTripId:  number | null

  // Latest sign from the Jetson — null until one is seen
  lastSpeedLimit: SpeedLimitSighting | null

  // Manually reconnect if needed
  reconnect:     () => void
}

// ---------------------------------------------------------------------------
// MQTT message shapes from the Express bridge
// ---------------------------------------------------------------------------

interface MqttEntry {
  topic:      string
  message:    unknown
  receivedAt: string
}

interface WsMessage {
  type:      'live' | 'catchup'
  // live — single entry
  topic?:    string
  message?:  unknown
  // catchup — array of entries
  messages?: MqttEntry[]
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const WebSocketContext = createContext<WebSocketContextValue | null>(null)

// ---------------------------------------------------------------------------
// Helper — parse a raw MQTT reading message into a LiveReading
// ---------------------------------------------------------------------------

function parseReading(message: unknown): LiveReading | null {
  if (typeof message !== 'object' || message === null) return null
  const m = message as Record<string, unknown>
  if (typeof m.ts !== 'string') return null

  return {
    id:               0, // not available in live stream
    ts:               m.ts as string,
    date:             new Date(m.ts as string),
    rpm:              typeof m.rpm              === 'number' ? m.rpm              : null,
    speed_mph:        typeof m.speed_mph        === 'number' ? m.speed_mph        : null,
    coolant_temp_f:   typeof m.coolant_temp_f   === 'number' ? m.coolant_temp_f   : null,
    throttle_pct:     typeof m.throttle_pct     === 'number' ? m.throttle_pct     : null,
    fuel_rate_gph:    typeof m.fuel_rate_gph    === 'number' ? m.fuel_rate_gph    : null,
    battery_soc_pct:  typeof m.battery_soc_pct  === 'number' ? m.battery_soc_pct  : null,
    hvb_temp_f:       typeof m.hvb_temp_f       === 'number' ? m.hvb_temp_f       : null,
    pack_voltage_v:   typeof m.pack_voltage_v   === 'number' ? m.pack_voltage_v   : null,
    battery_current_a: typeof m.battery_current_a === 'number' ? m.battery_current_a : null,
  }
}

// ---------------------------------------------------------------------------
// Helper — parse a vision scene message into a sighting, or null
//
// Scene and sign detections share maverick/vision/scene and are told apart only
// by the label prefix, so a scene label is a normal, expected null here. The
// REST seed (/api/vision/speed-limit) is deliberately the same shape, so both
// paths go through this one function.
// ---------------------------------------------------------------------------

function parseSpeedLimit(message: unknown): SpeedLimitSighting | null {
  if (typeof message !== 'object' || message === null) return null
  const m = message as Record<string, unknown>

  const value = speedLimitValue(typeof m.scene_label === 'string' ? m.scene_label : null)
  if (!value) return null

  // No usable capture time means the sighting cannot be aged, and an
  // un-ageable sign is worse than none: a stale limit shown as current is
  // exactly the wrong thing to put in front of a driver.
  if (typeof m.ts !== 'string') return null
  const date = new Date(m.ts)
  if (Number.isNaN(date.getTime())) return null

  return {
    value,
    confidence: typeof m.confidence === 'number' ? m.confidence : null,
    ts:         m.ts,
    date,
  }
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function WebSocketProvider({ children }: { children: ReactNode }) {
  const [connected,    setConnected]    = useState(false)
  const [pollerStatus, setPollerStatus] = useState<PollerStatus>('unknown')
  const [lastReading,  setLastReading]  = useState<LiveReading | null>(null)
  const [readings,     setReadings]     = useState<LiveReading[]>([])
  const [activeTripId, setActiveTripId] = useState<number | null>(null)
  const [lastSpeedLimit, setLastSpeedLimit] = useState<SpeedLimitSighting | null>(null)

  const wsRef      = useRef<WebSocket | null>(null)
  const backoffRef = useRef(INITIAL_BACKOFF)
  const timerRef   = useRef<ReturnType<typeof setTimeout> | null>(null)

  // -------------------------------------------------------------------------
  // Process a single MQTT entry
  // -------------------------------------------------------------------------

  // Keeps whichever sighting was captured later. Guards the REST seed racing
  // a live message on load, and replaying the catch-up buffer out of order.
  const recordSpeedLimit = useCallback((sighting: SpeedLimitSighting | null) => {
    if (!sighting) return
    setLastSpeedLimit(prev =>
      prev && prev.date.getTime() >= sighting.date.getTime() ? prev : sighting)
  }, [])

  const processEntry = useCallback((entry: MqttEntry) => {
    const { topic, message } = entry

    if (topic.endsWith('/reading')) {
      const reading = parseReading(message)
      if (!reading) return

      setLastReading(reading)
      setReadings(prev => {
        const next = [...prev, reading]
        // Keep rolling buffer at max BUFFER_SIZE
        return next.length > BUFFER_SIZE ? next.slice(next.length - BUFFER_SIZE) : next
      })
    }

    else if (topic.endsWith('/poller_status')) {
      const m = message as Record<string, unknown>
      setPollerStatus((m.status as PollerStatus) ?? 'unknown')
    }

    else if (topic.endsWith('/trip_open')) {
      const m = message as Record<string, unknown>
      setActiveTripId(typeof m.id === 'number' ? m.id : null)
    }

    else if (topic.endsWith('/trip_close')) {
      setActiveTripId(null)
    }

    // maverick/vision/scene — the lightweight twin of a confirmed frame. Sign
    // detections are the ones the live view surfaces; scene labels parse to
    // null and are ignored here.
    else if (topic.endsWith('/scene')) {
      recordSpeedLimit(parseSpeedLimit(message))
    }
  }, [recordSpeedLimit])

  // -------------------------------------------------------------------------
  // WebSocket connection
  // -------------------------------------------------------------------------

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(WS_URL)
    wsRef.current = ws

    ws.onopen = () => {
      setConnected(true)
      backoffRef.current = INITIAL_BACKOFF
      // Mock server doesn't emit poller_status events — treat the
      // WebSocket connection itself as proof the poller is up in dev.
      if (import.meta.env.VITE_WS_URL) {
        setPollerStatus('connected')
      }
    }

    ws.onclose = () => {
      setConnected(false)
      if (import.meta.env.VITE_WS_URL) setPollerStatus('disconnected')
      wsRef.current = null
      // Schedule reconnect with backoff
      timerRef.current = setTimeout(() => {
        backoffRef.current = Math.min(backoffRef.current * 2, MAX_BACKOFF)
        connect()
      }, backoffRef.current)
    }

    ws.onerror = (err) => {
      console.error('[ws] Error:', err)
    }

    ws.onmessage = (event) => {
      let msg: WsMessage
      try {
        msg = JSON.parse(event.data)
      } catch {
        console.warn('[ws] Non-JSON message:', event.data)
        return
      }

      if (msg.type === 'live' && msg.topic) {
        processEntry({
          topic:      msg.topic,
          message:    msg.message,
          receivedAt: new Date().toISOString(),
        })
      }

      else if (msg.type === 'catchup' && Array.isArray(msg.messages)) {
        // Process catch-up messages in order — populates initial buffer
        msg.messages.forEach(processEntry)
      }
    }
  }, [processEntry])

  // -------------------------------------------------------------------------
  // Seed the held sign from the bridge
  //
  // The sign in force was confirmed before this page loaded — possibly long
  // before, since the vision pipeline publishes on change only — so the
  // WebSocket cannot supply it and the catch-up buffer has evicted it. One
  // request; live messages take over from there. Failure is silent: an
  // unreachable bridge or an older server just means no sign until the next.
  // -------------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false
    fetch(`${API}/vision/speed-limit`)
      .then(res => (res.ok ? res.json() : null))
      .then(body => { if (!cancelled) recordSpeedLimit(parseSpeedLimit(body)) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [recordSpeedLimit])

  useEffect(() => {
    connect()
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        // Null handlers before closing so a cleanup-triggered close
        // does not fire onclose and schedule a reconnect (React StrictMode
        // runs cleanup+remount in dev, causing a spurious CONNECTING→close).
        ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null
        ws.close()
      }
    }
  }, [connect])

  // -------------------------------------------------------------------------
  // Value
  // -------------------------------------------------------------------------

  return (
    <WebSocketContext.Provider value={{
      connected,
      pollerStatus,
      lastReading,
      readings,
      activeTripId,
      lastSpeedLimit,
      reconnect: connect,
    }}>
      {children}
    </WebSocketContext.Provider>
  )
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useLiveTelemetry() {
  const ctx = useContext(WebSocketContext)
  if (!ctx) throw new Error('useLiveTelemetry must be used within WebSocketProvider')
  return ctx
}
