// client/src/components/VisionSection.tsx
//
// What the Jetson's camera recognised during a trip.
//
// Two kinds of detection arrive on the same endpoint, distinguished by the
// "speed_limit_" label convention — see SpeedLimitSign.tsx, which owns both the
// parser and the sign graphic. Speed limits get rendered as an actual sign
// because that is the thing worth recognising at a glance.
//
// Snapshots are served by the Pi from /api/snapshots/<path>, written by
// db_writer. They are genuinely optional — a frame row can exist with a missing
// or unwritten JPEG — so every image degrades to a label-only chip rather than
// a broken-image icon.

import { useState } from 'react'
import type { VisionFrame } from '@/contexts/TripContext'
import { Badge } from '@/components/ui/badge'
import { SpeedLimitSign } from '@/components/SpeedLimitSign'
import { speedLimitValue } from '@/lib/utils'

function formatTime(ts: string) {
  return new Date(ts).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
}

function prettyLabel(label: string | null) {
  if (!label) return 'unlabelled'
  return label.replace(/_/g, ' ')
}

function Snapshot({ frame }: { frame: VisionFrame }) {
  const [failed, setFailed] = useState(false)
  if (!frame.snapshot_url || failed) {
    return (
      <div className="w-20 h-[45px] shrink-0 rounded bg-muted flex items-center justify-center">
        <span className="text-[8px] text-muted-foreground">no image</span>
      </div>
    )
  }
  return (
    <img
      src={frame.snapshot_url}
      alt={prettyLabel(frame.scene_label)}
      loading="lazy"
      onError={() => setFailed(true)}
      className="w-20 h-[45px] shrink-0 rounded object-cover bg-black"
    />
  )
}

function VisionRow({ frame }: { frame: VisionFrame }) {
  const limit = speedLimitValue(frame.scene_label)
  return (
    <div className="flex items-center gap-2.5">
      <Snapshot frame={frame} />
      {limit && <SpeedLimitSign value={limit} />}
      <div className="min-w-0 flex-1">
        {!limit && (
          <p className="text-xs capitalize truncate">{prettyLabel(frame.scene_label)}</p>
        )}
        {limit && <p className="text-xs">{limit} mph zone</p>}
        <p className="text-[10px] text-muted-foreground tabular-nums">
          {formatTime(frame.ts)}
          {frame.confidence != null && ` · ${Math.round(frame.confidence * 100)}% confident`}
        </p>
      </div>
    </div>
  )
}

export function VisionSection({ frames }: { frames: VisionFrame[] }) {
  if (frames.length === 0) return null

  const signs = frames.filter(f => speedLimitValue(f.scene_label) !== null)
  const scenes = frames.filter(f => speedLimitValue(f.scene_label) === null)

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between px-0.5">
        <p className="text-[10px] text-muted-foreground uppercase tracking-wider">
          Vision
        </p>
        {signs.length > 0 && (
          <Badge variant="secondary" className="text-[9px] px-1.5 py-0">
            {signs.length} sign{signs.length === 1 ? '' : 's'}
          </Badge>
        )}
      </div>

      <div className="rounded-lg border bg-card p-3 space-y-3">
        {signs.length > 0 && (
          <div className="space-y-2">
            {signs.map(frame => <VisionRow key={frame.id} frame={frame} />)}
          </div>
        )}

        {signs.length > 0 && scenes.length > 0 && <div className="border-t" />}

        {scenes.length > 0 && (
          <div className="space-y-2">
            {scenes.map(frame => <VisionRow key={frame.id} frame={frame} />)}
          </div>
        )}
      </div>
    </div>
  )
}
