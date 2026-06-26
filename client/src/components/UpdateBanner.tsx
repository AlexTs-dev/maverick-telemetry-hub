// client/src/components/UpdateBanner.tsx
//
// Slim top banner shown when the server reports a newer release is available.
// Offers an "Update now" action (triggers pull-deploy via the bridge) and a dismiss.

import { Button } from '@/components/ui/button'

interface UpdateBannerProps {
  current:   string | null
  latest:    string | null
  updating:  boolean
  error:     string | null
  onUpdate:  () => void
  onDismiss: () => void
}

// Release tags look like "deploy-<hash>"; show just the hash for brevity.
function shortTag(tag: string | null) {
  return tag ? tag.replace(/^deploy-/, '') : '—'
}

export function UpdateBanner({ current, latest, updating, error, onUpdate, onDismiss }: UpdateBannerProps) {
  return (
    <div
      className="fixed top-0 inset-x-0 z-50 flex items-center justify-center gap-3
                 bg-primary text-primary-foreground px-4 py-2 text-sm shadow-md"
      role="status"
    >
      <span aria-hidden>⬆</span>
      <span className="font-medium">{updating ? 'Updating…' : 'Update available'}</span>

      {!updating && (
        <span className="opacity-80 tabular-nums hidden sm:inline">
          {shortTag(current)} → {shortTag(latest)}
        </span>
      )}

      {error && <span className="opacity-90">{error}</span>}

      {!updating && (
        <>
          <Button size="sm" variant="secondary" onClick={onUpdate} className="h-7">
            Update now
          </Button>
          <button
            onClick={onDismiss}
            aria-label="Dismiss"
            className="ml-1 leading-none opacity-70 hover:opacity-100"
          >
            ✕
          </button>
        </>
      )}
    </div>
  )
}
