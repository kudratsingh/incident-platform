import { useEffect, useState } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import ProgressBar from '../components/ProgressBar'
import TraceId from '../components/TraceId'
import { jobsApi } from '../api/jobs'
import { adminApi } from '../api/admin'
import { lastMeta } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { useJobStream } from '../hooks/useJobStream'
import type { Job, JobEvent } from '../types'
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
  const { user } = useAuth()
  const [job, setJob] = useState<Job | null>(null)
  const [requestId, setRequestId] = useState<string | undefined>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [timeline, setTimeline] = useState<JobEvent[] | null>(null)
  const [timelineOpen, setTimelineOpen] = useState(false)
  const isPrivileged = user?.role === 'admin' || user?.role === 'support'

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

  // Lazy-load the Kafka event timeline the first time the user opens it.
  useEffect(() => {
    if (!timelineOpen || !id || !isPrivileged || timeline !== null) return
    adminApi
      .jobTimeline(id)
      .then((t) => setTimeline(t.events))
      .catch(() => setTimeline([]))
  }, [timelineOpen, id, isPrivileged, timeline])

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

          {/* Kafka event timeline (event sourcing) — admin/support only */}
          {isPrivileged && (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-5">
              <button
                onClick={() => setTimelineOpen((o) => !o)}
                className="w-full flex items-center justify-between text-left"
              >
                <div>
                  <h2 className="text-sm font-medium text-gray-300">
                    Kafka event timeline
                  </h2>
                  <p className="text-xs text-gray-600">
                    Event-sourced log — replays the durable Kafka events for this job.
                  </p>
                </div>
                <span className="text-xs text-gray-500 font-mono">
                  {timelineOpen ? '−' : '+'}
                </span>
              </button>

              {timelineOpen && (
                <div className="mt-4">
                  {timeline === null ? (
                    <p className="text-xs text-gray-500">Loading…</p>
                  ) : timeline.length === 0 ? (
                    <p className="text-xs text-gray-500">
                      No Kafka events recorded — the event-log consumer hasn't
                      seen this job yet (or none of its events have flowed
                      through Kafka).
                    </p>
                  ) : (
                    <ol className="space-y-2">
                      {timeline.map((ev) => (
                        <li key={ev.id} className="flex items-start gap-3">
                          <span className="w-2 h-2 rounded-full bg-blue-500 mt-1.5 shrink-0" />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-baseline gap-2 flex-wrap">
                              <code className="text-xs font-mono text-blue-300">
                                {ev.event_name}
                              </code>
                              <span className="text-[10px] text-gray-500 font-mono">
                                {ev.kafka_topic} / p{ev.kafka_partition} / o{ev.kafka_offset}
                              </span>
                              <span className="text-[10px] text-gray-500 ml-auto">
                                {formatDate(ev.recorded_at)}
                              </span>
                            </div>
                            <pre className="mt-1 bg-gray-800/40 border border-gray-700/40 rounded px-2 py-1 text-[11px] font-mono text-gray-400 overflow-x-auto whitespace-pre-wrap break-all max-h-32">
                              {JSON.stringify(ev.payload, null, 2)}
                            </pre>
                          </div>
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              )}
            </div>
          )}
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
              {job.saga_id && (
                <div className="flex justify-between gap-2">
                  <dt className="text-gray-500 shrink-0">Saga</dt>
                  <dd className="text-right">
                    <Link
                      to={`/sagas/${job.saga_id}`}
                      className="text-blue-400 hover:text-blue-300 font-mono text-xs"
                    >
                      {job.saga_id.slice(0, 8)}…
                    </Link>
                  </dd>
                </div>
              )}
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
