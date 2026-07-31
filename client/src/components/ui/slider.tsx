// client/src/components/ui/slider.tsx
//
// Range control. Built on Radix rather than <input type="range"> for the
// pointer capture and touch handling — the primary consumer is a video
// scrubber on a touchscreen mounted in a vehicle, where a dropped drag means
// the footage jumps somewhere the driver did not ask for.
//
// Imported from the unified `radix-ui` package rather than
// @radix-ui/react-slider, for the same reason as alert-dialog.tsx: only the
// former is a direct dependency here.
//
// `children` render INSIDE the track, above the filled range and below the
// thumb. That is how DashcamSection draws segment boundaries on the scrubber
// without the ticks painting over the handle.

import * as React from 'react'
import { Slider as SliderPrimitive } from 'radix-ui'
import { cn } from '@/lib/utils'

function Slider({
  className,
  children,
  // The thumb is what carries role="slider", so that is where the label has to
  // land — on the root it would name an element with no role at all.
  'aria-label': ariaLabel,
  ...props
}: React.ComponentProps<typeof SliderPrimitive.Root>) {
  return (
    <SliderPrimitive.Root
      className={cn(
        'relative flex w-full touch-none items-center select-none',
        'data-[disabled]:pointer-events-none data-[disabled]:opacity-50',
        className,
      )}
      {...props}
    >
      <SliderPrimitive.Track className="relative h-1.5 w-full grow overflow-hidden rounded-full bg-muted">
        <SliderPrimitive.Range className="absolute h-full bg-primary" />
        {children}
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb
        aria-label={ariaLabel}
        className="block size-3.5 shrink-0 rounded-full border border-primary/60 bg-background
                   shadow-sm transition-[color,box-shadow] hover:ring-4 hover:ring-primary/20
                   focus-visible:ring-4 focus-visible:ring-primary/30 focus-visible:outline-none"
      />
    </SliderPrimitive.Root>
  )
}

export { Slider }
