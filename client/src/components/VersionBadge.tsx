// client/src/components/VersionBadge.tsx
//
// Small, unobtrusive build-version label fixed to the bottom-right corner.
// Overlays page content without affecting layout (pages use full-screen layouts).

export function VersionBadge({ version }: { version: string }) {
  return (
    <span
      className="fixed bottom-16 right-2 z-40 select-none pointer-events-none
                 text-[10px] leading-none tabular-nums text-muted-foreground/60"
    >
      v{version}
    </span>
  )
}
