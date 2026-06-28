// client/src/components/BottomNav.tsx
//
// Bottom tab bar for the in-cab touchscreen. Large, glanceable tap targets
// for switching between the live gauges, trip history, and diagnostics.
// Rendered once by Layout so it persists across every route.

import { NavLink } from 'react-router-dom'
import { IconGauge, IconRoute, IconAlertTriangle } from '@tabler/icons-react'
import { cn } from '@/lib/utils'

interface Tab {
  to:    string
  label: string
  icon:  typeof IconGauge
  // `end` makes the link active only on an exact path match. The Gauges tab
  // lives at the index route, so without it every route would mark it active.
  // Trips omits it on purpose so the tab stays lit on /trips/:id detail pages.
  end:   boolean
}

const TABS: Tab[] = [
  { to: '/',            label: 'Gauges',      icon: IconGauge,         end: true  },
  { to: '/trips',       label: 'Trips',       icon: IconRoute,         end: false },
  { to: '/diagnostics', label: 'Diagnostics', icon: IconAlertTriangle, end: false },
]

export function BottomNav() {
  return (
    <nav className="grid grid-cols-3 h-14 border-t bg-background shrink-0">
      {TABS.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            cn(
              'flex flex-col items-center justify-center gap-0.5 select-none transition-colors active:bg-accent',
              isActive ? 'text-primary' : 'text-muted-foreground',
            )
          }
        >
          {({ isActive }) => (
            <>
              <Icon size={24} stroke={isActive ? 2.2 : 1.8} />
              <span className="text-[11px] font-medium uppercase tracking-wide leading-none">
                {label}
              </span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}
