import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth'
import { AppError } from '../api/client'

type Mode = 'join' | 'create'

export default function RegisterPage() {
  const { register } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [mode, setMode] = useState<Mode>('join')
  const [tenantSlug, setTenantSlug] = useState('default')
  const [newTenantName, setNewTenantName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (password.length < 8) {
      setError('Password must be at least 8 characters')
      return
    }
    setLoading(true)
    try {
      await register(email, password, {
        tenantSlug,
        newTenantName: mode === 'create' ? newTenantName : undefined,
      })
      navigate('/jobs')
    } catch (err) {
      setError(err instanceof AppError ? err.message : 'Registration failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <h1 className="font-mono font-semibold text-xl text-white mb-1">
          incident<span className="text-blue-400">//</span>platform
        </h1>
        <p className="text-sm text-gray-500 mb-8">Create an account</p>

        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-xs text-gray-400 mb-1.5">Email</label>
            <input
              type="email"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/60"
              placeholder="you@example.com"
              required
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1.5">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/60"
              placeholder="min 8 characters"
              required
            />
          </div>

          <div>
            <div className="flex gap-1 mb-2">
              <button
                type="button"
                onClick={() => setMode('join')}
                className={`flex-1 text-xs py-1.5 rounded ${
                  mode === 'join'
                    ? 'bg-gray-700 text-white'
                    : 'bg-gray-800/40 text-gray-400 hover:text-white'
                }`}
              >
                Join existing
              </button>
              <button
                type="button"
                onClick={() => setMode('create')}
                className={`flex-1 text-xs py-1.5 rounded ${
                  mode === 'create'
                    ? 'bg-gray-700 text-white'
                    : 'bg-gray-800/40 text-gray-400 hover:text-white'
                }`}
              >
                Start new workspace
              </button>
            </div>
            <label className="block text-xs text-gray-400 mb-1.5">
              Workspace slug
            </label>
            <input
              value={tenantSlug}
              onChange={(e) => setTenantSlug(e.target.value)}
              pattern="[a-zA-Z0-9_-]+"
              className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2.5 text-sm text-gray-200 font-mono placeholder-gray-600 focus:outline-none focus:border-blue-500/60"
              placeholder={mode === 'create' ? 'acme' : 'default'}
              required
            />
            {mode === 'create' && (
              <>
                <label className="block text-xs text-gray-400 mb-1.5 mt-3">
                  Workspace name
                </label>
                <input
                  value={newTenantName}
                  onChange={(e) => setNewTenantName(e.target.value)}
                  className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/60"
                  placeholder="Acme Corp"
                  required
                />
                <p className="text-xs text-gray-600 mt-1.5">
                  You'll be the admin of the new workspace.
                </p>
              </>
            )}
          </div>

          {error && (
            <p className="text-sm text-red-400 bg-red-900/20 border border-red-800/30 rounded px-3 py-2">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 rounded bg-blue-600 hover:bg-blue-500 disabled:bg-blue-800 disabled:cursor-not-allowed text-white text-sm font-medium transition-colors"
          >
            {loading ? 'Creating account…' : 'Create account'}
          </button>
        </form>

        <p className="text-sm text-gray-500 text-center mt-6">
          Already have an account?{' '}
          <Link to="/login" className="text-blue-400 hover:text-blue-300">
            Sign in
          </Link>
        </p>
      </div>
    </div>
  )
}
