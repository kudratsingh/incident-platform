import { Link, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'

export default function Layout({ children }: { children: React.ReactNode }) {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  function handleLogout() {
    logout()
    navigate('/login')
  }

  const navLink = (to: string, label: string) => {
    const active = location.pathname.startsWith(to)
    return (
      <Link
        to={to}
        className={`px-3 py-1.5 rounded text-sm font-medium transition-colors ${
          active
            ? 'bg-gray-700 text-white'
            : 'text-gray-400 hover:text-white hover:bg-gray-800'
        }`}
      >
        {label}
      </Link>
    )
  }

  return (
    <div className="min-h-screen flex flex-col">
      {/* Nav */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center gap-6">
          <Link to="/" className="font-mono font-semibold text-white tracking-tight">
            incident<span className="text-blue-400">//</span>platform
          </Link>
          <nav className="flex items-center gap-1 ml-4">
            {navLink('/jobs', 'Jobs')}
            {(user?.role === 'admin' || user?.role === 'support') &&
              navLink('/admin', 'Admin')}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <span className="text-xs text-gray-500 font-mono">{user?.email}</span>
            <span className="text-xs px-1.5 py-0.5 rounded bg-gray-800 text-gray-400 font-mono border border-gray-700">
              {user?.role}
            </span>
            <button
              onClick={handleLogout}
              className="text-sm text-gray-400 hover:text-white transition-colors"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 py-8">{children}</main>

      {/* Footer */}
      <footer className="border-t border-gray-800 py-3 text-center text-xs text-gray-600 font-mono">
        incident-platform · internal ops
      </footer>
    </div>
  )
}
