import { Component } from 'react'
import type { ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

/**
 * Catches unhandled render errors and shows a recovery UI instead of a
 * blank screen.  Wraps the entire app in main.tsx.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // In production you'd ship this to Sentry / Datadog here
    console.error('[ErrorBoundary]', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="min-h-screen flex items-center justify-center px-4">
        <div className="max-w-md w-full text-center space-y-4">
          <p className="font-mono text-2xl text-red-400">!</p>
          <h1 className="text-lg font-semibold text-white">Something went wrong</h1>
          <p className="text-sm text-gray-500">{error.message}</p>
          <button
            onClick={() => this.setState({ error: null })}
            className="px-4 py-2 rounded bg-gray-800 border border-gray-700 text-sm text-gray-300 hover:text-white transition-colors"
          >
            Try again
          </button>
        </div>
      </div>
    )
  }
}
