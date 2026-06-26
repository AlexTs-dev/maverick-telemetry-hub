// client/src/components/Layout.tsx
//
// Root layout wrapper. Renders the current route via <Outlet />, plus a
// build-version badge and (when the server reports a newer release) an
// update banner with an on-demand "Update now" action.

import { useState } from 'react'
import { Outlet } from 'react-router-dom'
import { useVersion } from '@/hooks/useVersion'
import { VersionBadge } from './VersionBadge'
import { UpdateBanner } from './UpdateBanner'

export function Layout() {
  const { appVersion, current, latest, updateAvailable, updating, error, triggerUpdate } = useVersion()
  const [dismissed, setDismissed] = useState(false)

  // Keep showing while updating even if the user dismissed it.
  const showBanner = updateAvailable && (!dismissed || updating)

  return (
    <div className="min-h-screen">
      {showBanner && (
        <UpdateBanner
          current={current}
          latest={latest}
          updating={updating}
          error={error}
          onUpdate={triggerUpdate}
          onDismiss={() => setDismissed(true)}
        />
      )}
      <main>
        <Outlet />
      </main>
      <VersionBadge version={appVersion} />
    </div>
  )
}
