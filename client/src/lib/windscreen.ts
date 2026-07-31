/**
 * Windscreen colour-correction constants, shared by the SVG filter definition
 * and by whatever references it.
 *
 * Separate from WindscreenFilter.tsx so that file exports only a component:
 * mixing constants and components in one module breaks React Fast Refresh
 * (react-refresh/only-export-components).
 */

/**
 * Must match VISION_WB_GAIN_R/G/B in the Jetson's ~/.maverick-env.
 *
 * Laminated automotive glass is green-tinted and absorbs red. Measured through
 * the truck's screen against a white speed-limit sign, R read 0.799 of neutral
 * while G and B tracked each other — a red deficit, which is why raw footage
 * looks cyan. These gains are normalised to preserve mean luma.
 *
 * Not fetched from the device: they live in a systemd EnvironmentFile the
 * Express bridge never reads, and inventing an endpoint to publish three
 * constants would be a lot of moving parts for a value that changes only when
 * somebody re-measures the windscreen. Overridable at build time so a second
 * vehicle with different glass needs no code edit.
 */
export const WINDSCREEN_WB_GAINS = {
  r: Number(import.meta.env.VITE_WB_GAIN_R ?? 1.252),
  g: Number(import.meta.env.VITE_WB_GAIN_G ?? 0.891),
  b: Number(import.meta.env.VITE_WB_GAIN_B ?? 0.927),
}

export const WINDSCREEN_FILTER_ID = 'windscreen-wb'
