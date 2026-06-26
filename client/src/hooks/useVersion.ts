// client/src/hooks/useVersion.ts
//
// Exposes the running app version (a build-time semver from package.json) and
// polls the Express bridge for whether a newer release is available. Also
// triggers the on-demand update (pull-deploy) via the server.

import { useCallback, useEffect, useState } from 'react'

// In a Tauri window the page isn't served from localhost:3000, so relative
// URLs won't reach the Express server. Mirror the pattern used in TripContext.
const TAURI = typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
const API   = (TAURI ? 'http://localhost:3000' : '') + '/api'

// How often to re-check the server for a newer release.
const POLL_MS = 5 * 60 * 1000

export interface VersionInfo {
  current:         string | null
  latest:          string | null
  updateAvailable: boolean
  checkedAt:       string | null
}

export function useVersion() {
  const appVersion = typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : '0.0.0'

  const [info, setInfo]         = useState<VersionInfo | null>(null)
  const [updating, setUpdating] = useState(false)
  const [error, setError]       = useState<string | null>(null)

  const check = useCallback(async () => {
    try {
      const res = await fetch(`${API}/version`)
      if (!res.ok) return
      setInfo(await res.json())
    } catch {
      // Offline, or endpoint missing (e.g. older server) — leave info as-is.
    }
  }, [])

  useEffect(() => {
    check()
    const id = setInterval(check, POLL_MS)
    return () => clearInterval(id)
  }, [check])

  const triggerUpdate = useCallback(async () => {
    setUpdating(true)
    setError(null)
    try {
      const res = await fetch(`${API}/version/update`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.error ?? `HTTP ${res.status}`)
      }
      // Services restart from here; the connection drops and on reconnect the
      // next poll clears the banner. Keep `updating` true until then.
    } catch (err) {
      setUpdating(false)
      setError(err instanceof Error ? err.message : 'Update failed')
    }
  }, [])

  return {
    appVersion,
    current:         info?.current ?? null,
    latest:          info?.latest ?? null,
    updateAvailable: info?.updateAvailable ?? false,
    updating,
    error,
    triggerUpdate,
    refresh: check,
  }
}
