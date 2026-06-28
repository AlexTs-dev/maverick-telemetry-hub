import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'

export function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <div className="flex flex-col items-center justify-center h-full gap-4 text-muted-foreground">
      <span className="text-6xl font-bold text-foreground/10 tabular-nums">404</span>
      <p className="text-sm">Page not found</p>
      <Button variant="outline" size="sm" onClick={() => navigate('/trips')}>
        Back to trips
      </Button>
    </div>
  )
}
