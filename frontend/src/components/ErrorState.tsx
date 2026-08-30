/**
 * The failed-to-load state for a list or panel.
 *
 * Deliberately distinct from every empty state in the app: an operator looking
 * at a table has to be able to tell "the platform has nothing to show you"
 * apart from "we could not ask". It carries the server's own message where we
 * have one, and a Retry that re-runs the same request.
 */
export default function ErrorState({
  message,
  onRetry,
  className = '',
}: {
  message: string
  onRetry?: () => void
  className?: string
}) {
  return (
    <div role="alert" className={`px-6 py-12 text-center ${className}`}>
      <p className="text-sm text-red-400">{message}</p>
      {onRetry && (
        <button
          onClick={onRetry}
          className="mt-3 text-xs font-medium px-3 py-1.5 rounded border border-gray-700 text-gray-300 hover:text-white hover:border-gray-500 transition-colors"
        >
          Retry
        </button>
      )}
    </div>
  )
}
