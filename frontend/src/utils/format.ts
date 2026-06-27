export function formatDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatDuration(start: string | null, end: string | null): string {
  if (!start) return '—'
  const endTime = end ? new Date(end) : new Date()
  const ms = endTime.getTime() - new Date(start).getTime()
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.floor((ms % 60_000) / 1000)}s`
}

export const STATUS_LABELS: Record<string, string> = {
  waiting: 'Waiting',
  pending: 'Pending',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  dead_letter: 'Dead Letter',
  retrying: 'Retrying',
  cancelled: 'Cancelled',
  // Saga statuses
  compensating: 'Compensating',
  compensated: 'Compensated',
}

export const STATUS_COLORS: Record<string, string> = {
  waiting: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
  pending: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  running: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  completed: 'bg-green-500/20 text-green-300 border-green-500/30',
  failed: 'bg-red-500/20 text-red-300 border-red-500/30',
  dead_letter: 'bg-red-900/30 text-red-400 border-red-800/40',
  retrying: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  cancelled: 'bg-gray-600/20 text-gray-500 border-gray-600/30',
  compensating: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  compensated: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
}

export const JOB_TYPE_LABELS: Record<string, string> = {
  csv_upload: 'CSV Upload',
  report_gen: 'Report Gen',
  bulk_api_sync: 'Bulk API Sync',
  doc_analysis: 'Doc Analysis',
}
