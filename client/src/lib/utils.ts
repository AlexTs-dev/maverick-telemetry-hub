import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// "speed_limit_35" -> "35". Two kinds of detection share the vision pipeline's
// scene_label field — a "speed_limit_" prefix means the two-stage YOLO sign
// pipeline, anything else is a whole-frame scene label — and that convention is
// set in jetson/vision_publisher.py. Null for scene labels and malformed
// suffixes alike: both mean "not a sign".
//
// Stays a string because it is only ever rendered, never arithmetic. Lives here
// rather than beside SpeedLimitSign so that component file exports components
// only (react-refresh/only-export-components).
export function speedLimitValue(label: string | null | undefined): string | null {
  const PREFIX = 'speed_limit_'
  if (!label || !label.startsWith(PREFIX)) return null
  const value = label.slice(PREFIX.length)
  return /^\d+$/.test(value) ? value : null
}
