import { Navigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import type { UserRole } from '../types'

interface Props {
  children: React.ReactNode
  requiredRole?: UserRole
}

export default function ProtectedRoute({ children, requiredRole }: Props) {
  const { user, loading } = useAuth()

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-gray-500 font-mono text-sm">Loading…</div>
      </div>
    )
  }

  if (!user) return <Navigate to="/login" replace />

  if (requiredRole) {
    const hierarchy: UserRole[] = ['user', 'support', 'admin']
    if (hierarchy.indexOf(user.role) < hierarchy.indexOf(requiredRole)) {
      return <Navigate to="/jobs" replace />
    }
  }

  return <>{children}</>
}
