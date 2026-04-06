import { STATUS_COLORS, STATUS_LABELS } from '../utils/format'

interface Props {
  status: string
  pulse?: boolean
}

export default function StatusBadge({ status, pulse }: Props) {
  const colors = STATUS_COLORS[status] ?? 'bg-gray-500/20 text-gray-300 border-gray-500/30'
  const label = STATUS_LABELS[status] ?? status

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium border ${colors}`}
    >
      {(status === 'running' || pulse) && (
        <span className="relative flex h-1.5 w-1.5">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-blue-400" />
        </span>
      )}
      {label}
    </span>
  )
}
