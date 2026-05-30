// client/src/contexts/WebSocketContext.tsx
//
// Manages the WebSocket connection to the Express bridge.
// Parses incoming MQTT messages and maintains a rolling D3-ready
// buffer of recent readings for live visualization.
//
// Usage:
//   const { connected, lastReading, readings, pollerStatus } = useLiveTelemetry()

import React, {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  useCallback,
  type ReactNode,
} from 'react'
import type { Reading } from './TripContext'

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

// How many readings to keep in the rolling buffer.
// At 1Hz this is 5 minutes of live data for D3 charts.
const BUFFER_SIZE = 300

const WS_URL =
  typeof window !== 'undefined'
    ? `ws://${window.location.host}`
    : 'ws://localhost:3000'

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
    battery_soc_pct:  typeof m.battery_soc_pct  === 'number' ? m.battery_soc_pct  : null,
    ev_mode:          typeof m.ev_mode          === 'number' ? m.ev_mode          : null,
    regen_kw:         typeof m.regen_kw         === 'number' ? m.regen_kw         : null,
    fuel_rate_gph:    typeof m.fuel_rate_gph    === 'number' ? m.fuel_rate_gph    : null,
    pack_voltage_v:   typeof m.pack_voltage_v   === 'number' ? m.pack_voltage_v   : null,
    battery_current_a: typeof m.battery_current_a === 'number' ? m.battery_current_a : null,
    motor_speed_rpm:  typeof m.motor_speed_rpm  === 'number' ? m.motor_speed_rpm  : null,
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

  const wsRef      = useRef<WebSocket | null>(null)
  const backoffRef = useRef(INITIAL_BACKOFF)
  const timerRef   = useRef<ReturnType<typeof setTimeout> | null>(null)

  // -------------------------------------------------------------------------
  // Process a single MQTT entry
  // -------------------------------------------------------------------------

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
  }, [])

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
    }

    ws.onclose = () => {
      setConnected(false)
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

  useEffect(() => {
    connect()
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current)
      wsRef.current?.close()
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
