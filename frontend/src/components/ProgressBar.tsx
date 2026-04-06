interface Props {
  progress: number   // 0-100
  status: string
  message?: string
}

const BAR_COLOR: Record<string, string> = {
  running: 'bg-blue-500',
  completed: 'bg-green-500',
  failed: 'bg-red-500',
  dead_letter: 'bg-red-800',
  retrying: 'bg-orange-500',
  pending: 'bg-gray-600',
}

export default function ProgressBar({ progress, status, message }: Props) {
  const color = BAR_COLOR[status] ?? 'bg-gray-600'
  const pct = Math.min(100, Math.max(0, progress))

  return (
    <div className="space-y-1.5">
      <div className="flex justify-between items-center text-xs text-gray-400">
        <span className="font-mono truncate max-w-xs">{message ?? ''}</span>
        <span className="font-mono ml-2 shrink-0">{pct}%</span>
      </div>
      <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-300 ${color} ${
            status === 'running' ? 'animate-pulse' : ''
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}
