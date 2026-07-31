/**
 * Windscreen colour correction for dashcam playback.
 *
 * The Jetson corrects the frames it hands the detector (camera.py,
 * VISION_WB_GAIN_*), but deliberately records the sensor's real output: a
 * correction fitted against one scene does not belong baked into dashcam
 * evidence, and leaving it out keeps the stored clip reversible if the gains
 * later prove wrong. So the correction is applied here instead, at playback.
 *
 * feColorMatrix rather than a CSS filter: `filter: saturate()/hue-rotate()`
 * cannot express a per-channel gain, which is exactly and only what this is.
 * The browser runs it on the GPU, so it costs the Pi and the Jetson nothing.
 *
 * Gains live in lib/windscreen.ts — see the note there on why this file
 * exports a component and nothing else.
 */
import { WINDSCREEN_FILTER_ID, WINDSCREEN_WB_GAINS } from '@/lib/windscreen'

/**
 * The filter definition. Render once per page; `<video>` references it by id.
 */
export function WindscreenFilter() {
  const { r, g, b } = WINDSCREEN_WB_GAINS
  return (
    <svg aria-hidden="true" focusable="false" className="absolute size-0">
      <defs>
        {/*
          color-interpolation-filters="sRGB" is load-bearing, not boilerplate.
          SVG filters default to linearRGB, which would linearise, scale, then
          re-encode — a different and wrong transform. The Jetson's LUT scales
          the stored 8-bit sRGB values directly, so this must too, or corrected
          playback would not match the frames the detector was tuned against.
        */}
        <filter id={WINDSCREEN_FILTER_ID} colorInterpolationFilters="sRGB">
          <feColorMatrix
            type="matrix"
            values={`${r} 0 0 0 0
                     0 ${g} 0 0 0
                     0 0 ${b} 0 0
                     0 0 0 1 0`}
          />
        </filter>
      </defs>
    </svg>
  )
}
