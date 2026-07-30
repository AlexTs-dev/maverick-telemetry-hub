// client/src/components/SpeedLimitSign.tsx
//
// A US-style speed limit sign. Cheap to draw, and instantly readable on the
// 800x480 in-cab panel in a way a text badge is not.
//
// Shared by the live view (the sign the Jetson last read) and the per-trip
// history (every sign it read during a trip). The label parser that decides
// what counts as a sign is speedLimitValue in lib/utils.
//
// Deliberately dumb about staleness and empty states: the live view fades a
// sighting it considers old, and passes a placeholder value when there is
// none. Both are its judgements to make, not this component's.

import { cn } from '@/lib/utils'

// sm fits the live view's 52px stats strip; md is the trip-history row.
const SIZES = {
  sm: { box: 'w-9 h-11',  caption: 'text-[6px]', value: 'text-base' },
  md: { box: 'w-11 h-14', caption: 'text-[7px]', value: 'text-xl'   },
} as const

interface SpeedLimitSignProps {
  value:      string
  size?:      keyof typeof SIZES
  className?: string
}

export function SpeedLimitSign({ value, size = 'md', className }: SpeedLimitSignProps) {
  const s = SIZES[size]
  return (
    <div className={cn(
      'shrink-0 rounded-[3px] bg-white text-black border-2 border-black',
      'flex flex-col items-center justify-center leading-none select-none',
      s.box,
      className,
    )}>
      <span className={cn('font-semibold tracking-tight', s.caption)}>SPEED</span>
      <span className={cn('font-semibold tracking-tight mb-0.5', s.caption)}>LIMIT</span>
      <span className={cn('font-bold tabular-nums', s.value)}>{value}</span>
    </div>
  )
}
