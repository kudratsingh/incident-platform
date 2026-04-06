import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import ProgressBar from '../components/ProgressBar'
import TraceId from '../components/TraceId'
import { jobsApi } from '../api/jobs'
import { lastMeta } from '../api/client'
import { useJobStream } from '../hooks/useJobStream'
import type { Job } from '../types'
import { formatDate, formatDuration, JOB_TYPE_LABELS } from '../utils/format'

function JsonBlock({ label, data }: { label: string; data: unknown }) {
  if (data == null) return null
  return (
    <div>
      <p className="text-xs text-gray-500 mb-1.5">{label}</p>
      <pre className="bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2.5 text-xs font-mono text-gray-300 overflow-x-auto whitespace-pre-wrap">
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  )
}

export default function JobDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [job, setJob] = useState<Job | null>(null)
  const [requestId, setRequestId] = useState<string | undefined>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const { latest, events, connected } = useJobStream(
    job?.status === 'running' || job?.status === 'pending' ? id ?? null : null,
  )

  useEffect(() => {
    if (!id) return
    jobsApi
      .get(id)
      .then((j) => {
        setJob(j)
        setRequestId(lastMeta.requestId)
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Not found'))
      .finally(() => setLoading(false))
  }, [id])

  // Refresh job record when SSE reports terminal state
  useEffect(() => {
    if (!latest) return
    const terminal = ['completed', 'failed', 'dead_letter']
    if (terminal.includes(latest.status) && id) {
      jobsApi.get(id).then(setJob).catch(() => {})
    }
  }, [latest, id])

  if (loading) {
    return (
      <Layout>
        <div className="py-12 text-center text-gray-500 text-sm">Loading…</div>
      </Layout>
    )
  }

  if (error || !job) {
    return (
      <Layout>
        <div className="py-12 text-center text-red-400 text-sm">{error ?? 'Job not found'}</div>
      </Layout>
    )
  }

  const activeStatus = latest?.status ?? job.status
  const activeProgress = latest?.progress ?? (job.status === 'completed' ? 100 : 0)

  return (
    <Layout>
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <button
            onClick={() => navigate(-1)}
            className="text-xs text-gray-500 hover:text-gray-300 mb-2 block"
          >
            ← Back
          </button>
          <h1 className="text-lg font-semibold text-white">
            {JOB_TYPE_LABELS[job.type] ?? job.type}
          </h1>
          <code className="text-xs font-mono text-gray-500">{job.id}</code>
        </div>
        <StatusBadge status={activeStatus} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: progress + events */}
        <div className="lg:col-span-2 space-y-5">
          {/* Live progress */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-medium text-gray-300">Progress</h2>
              {connected && (
                <span className="text-xs text-blue-400 font-mono flex items-center gap-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse inline-block" />
                  live
                </span>
              )}
            </div>
            <ProgressBar
              progress={activeProgress}
              status={activeStatus}
              message={latest?.message ?? (job.status === 'completed' ? 'Completed' : '')}
            />
          </div>

          {/* Event log */}
          {events.length > 0 && (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-5">
              <h2 className="text-sm font-medium text-gray-300 mb-3">Event log</h2>
              <div className="space-y-1.5 max-h-60 overflow-y-auto scrollbar-thin">
                {events.map((ev, i) => (
                  <div key={i} className="flex items-start gap-3 text-xs font-mono">
                    <span className="text-gray-600 shrink-0">
                      {new Date(ev.timestamp).toLocaleTimeString()}
                    </span>
                    <span className="text-gray-400">{ev.message}</span>
                    <span className="ml-auto text-gray-600 shrink-0">{ev.progress}%</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Payload / Result / Error */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-4">
            <h2 className="text-sm font-medium text-gray-300">Data</h2>
            <JsonBlock label="Payload" data={job.payload} />
            <JsonBlock label="Result" data={job.result} />
            {job.error_message && (
              <div>
                <p className="text-xs text-gray-500 mb-1.5">Error</p>
                <p className="text-sm text-red-400 bg-red-900/20 border border-red-800/30 rounded px-3 py-2 font-mono">
                  {job.error_message}
                </p>
              </div>
            )}
          </div>
        </div>

        {/* Right: metadata */}
        <div className="space-y-5">
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-4">
            <h2 className="text-sm font-medium text-gray-300">Details</h2>

            <dl className="space-y-3 text-sm">
              {[
                ['Status', <StatusBadge key="s" status={activeStatus} />],
                ['Type', JOB_TYPE_LABELS[job.type] ?? job.type],
                ['Priority', job.priority],
                ['Retries', `${job.retry_count} / ${job.max_retries}`],
                ['Created', formatDate(job.created_at)],
                ['Duration', formatDuration(job.started_at, job.completed_at)],
              ].map(([label, value]) => (
                <div key={String(label)} className="flex justify-between gap-2">
                  <dt className="text-gray-500 shrink-0">{label}</dt>
                  <dd className="text-gray-300 text-right">{value}</dd>
                </div>
              ))}
            </dl>
          </div>

          {/* Correlation IDs — the key debugging panel */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-3">
            <h2 className="text-sm font-medium text-gray-300">Correlation IDs</h2>
            <p className="text-xs text-gray-600">Use these to search structured logs end-to-end.</p>
            <div className="space-y-2">
              <TraceId label="trace_id" value={job.trace_id} />
              <TraceId label="job_id" value={job.id} />
              <TraceId label="request_id" value={requestId} />
            </div>
          </div>
        </div>
      </div>
    </Layout>
  )
}
