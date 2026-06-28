// client/src/router.tsx
import { createBrowserRouter } from 'react-router-dom'
import { Layout } from './components/Layout'
import { TripListPage } from './pages/TripListPage'
import { TripDetailPage } from './pages/TripDetailPage'
import { LivePage } from './pages/LivePage'
import { DiagnosticsPage } from './pages/DiagnosticsPage'
import { NotFoundPage } from './pages/NotFoundPage'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <Layout />,
    children: [
      {
        index: true,
        element: <LivePage />,
      },
      {
        path: 'trips',
        element: <TripListPage />,
      },
      {
        path: 'trips/:id',
        element: <TripDetailPage />,
      },
      {
        path: 'live',
        element: <LivePage />,
      },
      {
        path: 'diagnostics',
        element: <DiagnosticsPage />,
      },
      {
        path: '*',
        element: <NotFoundPage />,
      },
    ],
  },
])
