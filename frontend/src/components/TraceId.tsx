import { useState } from 'react'

interface Props {
  label: string
  value: string | null | undefined
}

/**
 * Displays a correlation ID (trace_id, request_id) with a copy button.
 * The reason this exists as a component: being able to grab a trace ID and
 * search logs is the core of the "debugging realism" goal in Phase 3.
 */
export default function TraceId({ label, value }: Props) {
  const [copied, setCopied] = useState(false)

  if (!value) return null

  function copy() {
    navigator.clipboard.writeText(value!)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-500 shrink-0">{label}</span>
      <code className="text-xs font-mono text-gray-400 bg-gray-800/60 px-2 py-0.5 rounded border border-gray-700/50 truncate max-w-xs">
        {value}
      </code>
      <button
        onClick={copy}
        className="text-xs text-gray-500 hover:text-gray-300 transition-colors shrink-0"
      >
        {copied ? '✓' : 'copy'}
      </button>
    </div>
  )
}
