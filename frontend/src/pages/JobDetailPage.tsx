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
import { useJobStream, TERMINAL_STATUSES } from '../hooks/useJobStream'
import type { Job, JobEvent, JobTriage } from '../types'
import { formatDate, formatDuration, JOB_TYPE_LABELS } from '../utils/format'

/**
 * How often to re-read the job row while it is live but not being streamed.
 *
 * The stream is the primary channel; this is the floor under it, for the gap
 * before the first connect and for any window where the connection is down and
 * the hook is backing off. Without it a job whose stream never opened sat
 * frozen at its load-time snapshot indefinitely.
 */
const POLL_MS = 5000

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
  const [triage, setTriage] = useState<JobTriage | null>(null)
  const isPrivileged = user?.role === 'admin' || user?.role === 'support'

  // Stream every NON-terminal job, not just running/pending. `waiting` (held
  // on a dependency) and `retrying` (between attempts) are live states that
  // move on their own; opening the stream only for running/pending left a job
  // viewed in either of them frozen at its load-time snapshot forever.
  const jobStatus = job?.status
  const isLive = jobStatus !== undefined && !TERMINAL_STATUSES.has(jobStatus)
  const { latest, events, connected } = useJobStream(isLive ? id ?? null : null)

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

  // Poll the row whenever the job is live but the stream is not carrying it.
  // Keyed on primitives so a fresh job object does not re-arm the interval.
  useEffect(() => {
    if (!id || !isLive || connected) return
    const t = setInterval(() => {
      void jobsApi.get(id).then(setJob).catch(() => {})
    }, POLL_MS)
    return () => clearInterval(t)
  }, [id, isLive, connected])

  // Refresh the job record when SSE reports a terminal state, so the row's
  // durable fields (result, error, completed_at) replace the live snapshot.
  useEffect(() => {
    if (!latest || !id) return
    if (TERMINAL_STATUSES.has(latest.status)) {
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

  // Fetch triage as soon as we know we're an admin viewing a dead-lettered
  // job. 404 just means "no analysis yet" — silent.
  useEffect(() => {
    if (!id || !isPrivileged || !job) return
    if (job.status !== 'dead_letter') return
    adminApi
      .jobTriage(id)
      .then(setTriage)
      .catch(() => setTriage(null))
  }, [id, isPrivileged, job])

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

          {/* LLM triage analysis — admin/support only, dead_letter jobs */}
          {isPrivileged && triage && <TriageCard triage={triage} />}

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

const CATEGORY_LABELS: Record<string, string> = {
  external_api_failure: 'External API',
  validation_error: 'Validation',
  infrastructure: 'Infrastructure',
  data_corruption: 'Data Corruption',
  configuration: 'Configuration',
  transient: 'Transient',
  unknown: 'Unknown',
}

const CATEGORY_COLORS: Record<string, string> = {
  external_api_failure: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
  validation_error: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  infrastructure: 'bg-red-500/20 text-red-300 border-red-500/30',
  data_corruption: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
  configuration: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  transient: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
  unknown: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
}

function TriageCard({ triage }: { triage: JobTriage }) {
  const confidencePct = Math.round(triage.confidence * 100)
  const cat = triage.root_cause_category
  return (
    <div className="bg-gradient-to-br from-purple-900/15 to-transparent border border-purple-700/30 rounded-lg p-5">
      <div className="flex items-start justify-between mb-3 gap-3">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-[10px] uppercase tracking-wider text-purple-300 font-medium">
              AI Triage
            </span>
            <span
              className={`inline-flex px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wide font-medium border ${CATEGORY_COLORS[cat] ?? CATEGORY_COLORS.unknown}`}
            >
              {CATEGORY_LABELS[cat] ?? cat}
            </span>
            <span
              className={`inline-flex px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wide font-medium border ${
                triage.is_retryable
                  ? 'bg-green-500/20 text-green-300 border-green-500/30'
                  : 'bg-red-500/20 text-red-300 border-red-500/30'
              }`}
            >
              {triage.is_retryable ? 'Retryable' : 'Not retryable'}
            </span>
          </div>
          <p className="text-sm text-gray-200 mt-2 leading-relaxed">{triage.summary}</p>
        </div>
        <div className="text-right shrink-0">
          <p className="text-[10px] text-gray-500 uppercase tracking-wide">
            Confidence
          </p>
          <p
            className={`text-lg font-mono ${
              confidencePct >= 80
                ? 'text-green-400'
                : confidencePct >= 50
                  ? 'text-yellow-300'
                  : 'text-red-400'
            }`}
          >
            {confidencePct}%
          </p>
        </div>
      </div>

      <div className="bg-gray-800/40 border border-gray-700/40 rounded p-3 mt-3">
        <p className="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5">
          Suggested fix
        </p>
        <p className="text-sm text-gray-200 leading-relaxed">{triage.suggested_fix}</p>
      </div>

      <div className="flex items-center justify-between mt-3 text-[11px] text-gray-600 font-mono flex-wrap gap-2">
        <span>{triage.model_used}</span>
        {triage.usage && (
          <span>
            in: {triage.usage.input_tokens ?? 0} · out: {triage.usage.output_tokens ?? 0}
            {(triage.usage.cache_read_input_tokens ?? 0) > 0 && (
              <> · cached: {triage.usage.cache_read_input_tokens}</>
            )}
          </span>
        )}
      </div>
    </div>
  )
}
