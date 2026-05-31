import { useRef, useEffect, useState } from 'react'
import * as d3 from 'd3'
import { useLiveTelemetry } from '@/contexts/WebSocketContext'
import type { LiveReading, PollerStatus } from '@/contexts/WebSocketContext'
import { Badge } from '@/components/ui/badge'
import { cn }   from '@/lib/utils'

// ---------------------------------------------------------------------------
// D3 chart config
// ---------------------------------------------------------------------------

const VB_W = 380, VB_H = 110
const M    = { top: 4, right: 8, bottom: 18, left: 38 }
const IW   = VB_W - M.left - M.right
const IH   = VB_H - M.top  - M.bottom

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmt(n: number | null | undefined, dec = 0, suffix = '') {
  return n != null ? `${n.toFixed(dec)}${suffix}` : '—'
}

// ---------------------------------------------------------------------------
// Arc gauge — pure SVG, no D3 needed for static arcs
// ---------------------------------------------------------------------------
// Angles in D3 arc convention: 0 = top (12 o'clock), clockwise positive.
// SA = -135° = bottom-left start, EA = +135° = bottom-right end.

const SA = -(3 * Math.PI) / 4
const EA =  (3 * Math.PI) / 4

// Convert D3-style angle to {x, y} relative to arc center
function pt(r: number, a: number) {
  return { x: r * Math.sin(a), y: -r * Math.cos(a) }
}

// Build a filled annular arc path
function ringPath(ro: number, ri: number, a1: number, a2: number): string {
  if (Math.abs(a2 - a1) < 0.001) return ''
  const large = Math.abs(a2 - a1) > Math.PI ? 1 : 0
  const os = pt(ro, a1), oe = pt(ro, a2)
  const is = pt(ri, a2), ie = pt(ri, a1)
  return [
    `M ${os.x} ${os.y}`,
    `A ${ro} ${ro} 0 ${large} 1 ${oe.x} ${oe.y}`,
    `L ${is.x} ${is.y}`,
    `A ${ri} ${ri} 0 ${large} 0 ${ie.x} ${ie.y}`,
    'Z',
  ].join(' ')
}

interface GaugeProps {
  value:  number | null | undefined
  max:    number
  label:  string
  unit:   string
  color:  string
  ticks?: number[]   // fractions 0–1 where to draw tick marks
  tickLabels?: { f: number; text: string }[]
}

function ArcGauge({ value, max, label, unit, color, ticks, tickLabels }: GaugeProps) {
  // ViewBox sized to the container's 2:1 aspect ratio (400px wide, 176px tall)
  const VW = 400, VH = 176
  const cx = 200, cy = 116
  const RO = 82, RI = 64

  const pct = value != null ? Math.min(Math.max(value / max, 0), 1) : 0
  const va  = SA + pct * (EA - SA)

  const defaultTicks = [0, 0.25, 0.5, 0.75, 1]
  const tickFracs = ticks ?? defaultTicks

  const defaultLabels = [
    { f: 0,   text: '0' },
    { f: 1,   text: max >= 1000 ? `${max / 1000}K` : String(max) },
  ]
  const labels = tickLabels ?? defaultLabels

  // Needle tip and line
  const tip = pt(RI - 6, va)

  return (
    <svg viewBox={`0 0 ${VW} ${VH}`} className="w-full h-full" preserveAspectRatio="xMidYMid meet">
      <g transform={`translate(${cx},${cy})`}>

        {/* Background ring */}
        <path d={ringPath(RO, RI, SA, EA)} fill="hsl(var(--muted))" />

        {/* Value ring */}
        {pct > 0.001 && (
          <path d={ringPath(RO, RI, SA, va)} style={{ fill: color }} opacity={0.9} />
        )}

        {/* Tick marks */}
        {tickFracs.map((f, i) => {
          const a = SA + f * (EA - SA)
          const inner = pt(RO + 3, a)
          const outer = pt(RO + 10, a)
          return (
            <line key={i}
              x1={inner.x} y1={inner.y} x2={outer.x} y2={outer.y}
              stroke="hsl(var(--muted-foreground))" strokeWidth={1} opacity={0.4}
            />
          )
        })}

        {/* Tick labels */}
        {labels.map(({ f, text }, i) => {
          const a = SA + f * (EA - SA)
          const p = pt(RO + 22, a)
          return (
            <text key={i} x={p.x} y={p.y}
              textAnchor="middle" dominantBaseline="middle"
              style={{ fill: 'hsl(var(--muted-foreground))', fontSize: '11px' }}>
              {text}
            </text>
          )
        })}

        {/* Needle */}
        <line x1={0} y1={0} x2={tip.x} y2={tip.y}
          stroke="hsl(var(--foreground))" strokeWidth={2.5} strokeLinecap="round" />
        <circle r={7} fill="hsl(var(--background))"
          stroke="hsl(var(--foreground))" strokeWidth={2} />

        {/* Value text */}
        <text y={26} textAnchor="middle"
          style={{ fill: 'hsl(var(--foreground))', fontSize: '28px', fontWeight: 700,
                   fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.5px' }}>
          {value != null ? Math.round(value) : '—'}
        </text>
        <text y={42} textAnchor="middle"
          style={{ fill: 'hsl(var(--muted-foreground))', fontSize: '10px', letterSpacing: '0.08em' }}>
          {unit.toUpperCase()}
        </text>
      </g>

      {/* Label pinned to bottom centre */}
      <text x={cx} y={VH - 6} textAnchor="middle"
        style={{ fill: 'hsl(var(--muted-foreground))', fontSize: '9px', letterSpacing: '0.1em' }}>
        {label.toUpperCase()}
      </text>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Status dot
// ---------------------------------------------------------------------------

function StatusDot({ status }: { status: PollerStatus }) {
  return (
    <span className={cn(
      'inline-block w-2 h-2 rounded-full shrink-0',
      status === 'connected'    && 'bg-green-500',
      status === 'connecting'   && 'bg-yellow-400 animate-pulse',
      status === 'disconnected' && 'bg-destructive',
      status === 'unknown'      && 'bg-muted-foreground',
    )} />
  )
}

// ---------------------------------------------------------------------------
// Stat cell (small strip below gauges)
// ---------------------------------------------------------------------------

function StatCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col items-center justify-center border-r last:border-r-0">
      <span className="text-lg font-semibold tabular-nums leading-none">{value}</span>
      <span className="text-[9px] text-muted-foreground uppercase tracking-wider mt-1">{label}</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// D3 live line chart
// ---------------------------------------------------------------------------

interface LiveChartProps {
  readings: LiveReading[]
  accessor: (r: LiveReading) => number | null
  color:    string
  label:    string
  unit?:    string
  yDomain?: [number, number]
}

function LiveChart({ readings, accessor, color, label, unit, yDomain }: LiveChartProps) {
  const svgRef   = useRef<SVGSVGElement>(null)
  const initRef  = useRef(false)
  const gRef     = useRef<d3.Selection<SVGGElement,    unknown, null, undefined> | null>(null)
  const xAxisRef = useRef<d3.Selection<SVGGElement,    unknown, null, undefined> | null>(null)
  const yAxisRef = useRef<d3.Selection<SVGGElement,    unknown, null, undefined> | null>(null)
  const pathRef  = useRef<d3.Selection<SVGPathElement, unknown, null, undefined> | null>(null)
  const clipId   = `clip-${label.replace(/\W+/g, '')}`

  useEffect(() => {
    if (!svgRef.current) return
    const svg = d3.select(svgRef.current)

    if (!initRef.current) {
      initRef.current = true
      svg.append('defs').append('clipPath').attr('id', clipId)
        .append('rect').attr('width', IW).attr('height', IH + 2)
      const g = svg.append('g').attr('transform', `translate(${M.left},${M.top})`)
      gRef.current = g
      xAxisRef.current = g.append('g').attr('class', 'x-axis').attr('transform', `translate(0,${IH})`)
      yAxisRef.current = g.append('g').attr('class', 'y-axis')
      pathRef.current  = g.append('path')
        .attr('fill', 'none').attr('clip-path', `url(#${clipId})`)
        .attr('stroke-width', 1.5).attr('stroke-linejoin', 'round').attr('stroke-linecap', 'round')
        .style('stroke', color)
    }

    if (readings.length < 2) return
    const valid = readings.filter(r => accessor(r) != null)
    if (valid.length < 2) return

    const x = d3.scaleTime()
      .domain(d3.extent(readings, r => r.date) as [Date, Date]).range([0, IW])

    const vals  = valid.map(r => accessor(r) as number)
    const rawLo = Math.min(...vals), rawHi = Math.max(...vals)
    const pad   = (rawHi - rawLo) * 0.08 || 1
    const y = d3.scaleLinear()
      .domain(yDomain ?? [rawLo - pad, rawHi + pad]).range([IH, 0]).nice()

    const line = d3.line<LiveReading>()
      .defined(r => accessor(r) != null)
      .x(r => x(r.date)).y(r => y(accessor(r) as number))
      .curve(d3.curveMonotoneX)

    pathRef.current?.attr('d', line(readings))

    const fg  = 'var(--muted-foreground)'
    const bdr = 'var(--border)'

    xAxisRef.current
      ?.call(d3.axisBottom(x).ticks(4).tickSize(-IH)
        .tickFormat(d => d3.timeFormat('%M:%S')(d as Date)))
      .call(g => g.select('.domain').remove())
      .call(g => g.selectAll<SVGLineElement, unknown>('.tick line').style('stroke', bdr).style('stroke-dasharray', '2,3'))
      .call(g => g.selectAll<SVGTextElement, unknown>('.tick text').style('fill', fg).style('font-size', '9px'))

    yAxisRef.current
      ?.call(d3.axisLeft(y).ticks(3).tickSize(-IW))
      .call(g => g.select('.domain').remove())
      .call(g => g.selectAll<SVGLineElement, unknown>('.tick line').style('stroke', bdr).style('stroke-dasharray', '2,3'))
      .call(g => g.selectAll<SVGTextElement, unknown>('.tick text').style('fill', fg).style('font-size', '9px'))

  }, [readings])

  return (
    <div className="rounded-lg border bg-card p-2 flex flex-col gap-1 min-h-0 overflow-hidden">
      <p className="text-[10px] text-muted-foreground uppercase tracking-wider leading-none px-1 shrink-0">
        {label}{unit && ` · ${unit}`}
      </p>
      {readings.every(r => accessor(r) == null) ? (
        <div className="flex-1 flex items-center justify-center text-[10px] text-muted-foreground">
          waiting for data…
        </div>
      ) : (
        <svg ref={svgRef} viewBox={`0 0 ${VB_W} ${VB_H}`}
          preserveAspectRatio="none" className="w-full flex-1 min-h-0" />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function LivePage() {
  const { connected, pollerStatus, lastReading, readings, activeTripId } = useLiveTelemetry()
  const r = lastReading

  // Tick every second so data-age re-evaluates even when readings stop arriving
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const dataAgeMs  = lastReading ? now - lastReading.date.getTime() : Infinity
  const isLive     = dataAgeMs < 4_000   // data within the last 4 poll cycles

  // Derived status — data freshness beats MQTT events for accuracy
  const derivedStatus: PollerStatus =
    !connected                    ? 'disconnected' :
    pollerStatus === 'connecting' ? 'connecting'   :
    isLive                        ? 'connected'    :
    pollerStatus === 'disconnected' ? 'disconnected' :
    'unknown'

  return (
    <div className="flex flex-col h-screen">

      {/* Header — 48px */}
      <div className="flex items-center justify-between px-4 h-12 border-b shrink-0">
        <div className="flex items-center gap-2">
          <StatusDot status={derivedStatus} />
          <h1 className="text-base font-semibold">Live</h1>
          {activeTripId != null && (
            <span className="text-xs text-muted-foreground">Trip #{activeTripId}</span>
          )}
        </div>
        <Badge variant={derivedStatus === 'connected' ? 'outline' : 'destructive'} className="text-xs">
          {derivedStatus === 'connected'    ? 'Live'         :
           derivedStatus === 'connecting'   ? 'Connecting…'  :
           derivedStatus === 'disconnected' ? 'Disconnected' : 'Waiting…'}
        </Badge>
      </div>

      {/* Gauges — 176px, fills width 50/50 */}
      <div className="grid grid-cols-2 h-[176px] border-b shrink-0 divide-x">
        <ArcGauge
          value={r?.speed_mph}
          max={80}
          label="Speed"
          unit="mph"
          color="var(--chart-1)"
          tickLabels={[
            { f: 0,    text: '0'  },
            { f: 0.5,  text: '40' },
            { f: 1,    text: '80' },
          ]}
        />
        <ArcGauge
          value={r?.rpm}
          max={5000}
          label="Engine"
          unit="rpm"
          color="var(--chart-3)"
          tickLabels={[
            { f: 0,   text: '0'  },
            { f: 0.5, text: '2K' },
            { f: 1,   text: '5K' },
          ]}
        />
      </div>

      {/* Stats strip — 52px */}
      <div className="grid grid-cols-3 h-[52px] border-b shrink-0">
        <StatCell label="coolant"  value={fmt(r?.coolant_temp_f, 0, '°F')} />
        <StatCell label="throttle" value={fmt(r?.throttle_pct, 0, '%')} />
        <StatCell label="fuel"     value={fmt(r?.fuel_rate_gph, 3, ' gph')} />
      </div>

      {/* Charts — fill remaining ~204px */}
      <div className="flex-1 grid grid-cols-2 gap-2 p-2 min-h-0">
        <LiveChart
          readings={readings}
          accessor={r => r.speed_mph}
          color="var(--chart-1)"
          label="Speed" unit="mph"
          yDomain={[0, 80]}
        />
        <LiveChart
          readings={readings}
          accessor={r => r.rpm}
          color="var(--chart-3)"
          label="Engine RPM"
          yDomain={[0, 5000]}
        />
      </div>

    </div>
  )
}
