import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import { sagasApi } from '../api/sagas'
import { AppError } from '../api/client'
import { formatDate, JOB_TYPE_LABELS } from '../utils/format'
import type { Saga } from '../types'

const POLL_MS = 2000

export default function SagaDetailPage() {
  const { id } = useParams<{ id: string }>()
  const [saga, setSaga] = useState<Saga | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    if (!id) return
    try {
      const s = await sagasApi.get(id)
      setSaga(s)
      setError(null)
    } catch (err) {
      setError(err instanceof AppError ? err.message : 'Failed to load saga')
    } finally {
      setLoading(false)
    }
  }, [id])

  useEffect(() => {
    void load()
  }, [load])

  // Poll while the saga is still in flight. Stop once it reaches a terminal
  // state — completed/compensated are stable; failed/compensating are kept
  // polling because compensation jobs may still be running.
  useEffect(() => {
    if (!saga) return
    const terminal = saga.status === 'completed' || saga.status === 'compensated'
    if (terminal) return
    const t = setInterval(() => void load(), POLL_MS)
    return () => clearInterval(t)
  }, [saga, load])

  if (loading) {
    return (
      <Layout>
        <div className="text-sm text-gray-500">Loading saga…</div>
      </Layout>
    )
  }

  if (error || !saga) {
    return (
      <Layout>
        <div className="text-sm text-red-400">{error ?? 'Saga not found'}</div>
      </Layout>
    )
  }

  const completedCount = saga.steps.filter((s) => s.status === 'completed').length

  return (
    <Layout>
      <div className="mb-6">
        <Link to="/sagas" className="text-xs text-gray-500 hover:text-gray-300">
          ← All sagas
        </Link>
        <div className="flex items-baseline gap-3 mt-2">
          <h1 className="text-lg font-semibold text-white">{saga.name}</h1>
          <StatusBadge status={saga.status} />
        </div>
        <p className="text-xs text-gray-500 font-mono mt-1">{saga.id}</p>
        <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-3 max-w-2xl">
          <Stat label="Steps" value={saga.steps.length} />
          <Stat label="Completed" value={completedCount} />
          <Stat label="Started" value={formatDate(saga.created_at)} mono={false} small />
          <Stat
            label="Finished"
            value={saga.completed_at ? formatDate(saga.completed_at) : '—'}
            mono={false}
            small
          />
        </div>
      </div>

      {/* Step chain */}
      <div className="space-y-2">
        {saga.steps.map((step, i) => {
          const isFirst = i === 0
          const isLast = i === saga.steps.length - 1
          return (
            <div key={step.id} className="flex gap-3">
              {/* Connector line */}
              <div className="flex flex-col items-center w-6 pt-2">
                <div
                  className={`w-3 h-3 rounded-full border-2 ${
                    step.status === 'completed'
                      ? 'bg-green-500 border-green-500'
                      : step.status === 'running'
                        ? 'bg-blue-500 border-blue-500 animate-pulse'
                        : step.status === 'cancelled'
                          ? 'bg-gray-700 border-gray-700'
                          : step.status === 'failed' || step.status === 'dead_letter'
                            ? 'bg-red-500 border-red-500'
                            : 'bg-gray-900 border-gray-600'
                  }`}
                />
                {!isLast && (
                  <div
                    className={`w-px flex-1 mt-1 ${
                      step.status === 'completed' ? 'bg-green-700' : 'bg-gray-800'
                    }`}
                    style={{ minHeight: 32 }}
                  />
                )}
              </div>

              {/* Step card */}
              <div
                className={`flex-1 bg-gray-900 border rounded-lg p-3 mb-${isLast ? '0' : '2'} ${
                  step.status === 'running'
                    ? 'border-blue-700/50'
                    : 'border-gray-800'
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono text-gray-600 w-5">
                      {i + 1}.
                    </span>
                    <Link
                      to={`/jobs/${step.id}`}
                      className="text-sm text-gray-200 hover:text-white"
                    >
                      {JOB_TYPE_LABELS[step.type] ?? step.type}
                    </Link>
                    <code className="text-xs text-gray-600 font-mono">
                      {step.id.slice(0, 8)}…
                    </code>
                  </div>
                  <StatusBadge status={step.status} />
                </div>
                {step.error_message && (
                  <p className="mt-2 text-xs text-red-300 font-mono whitespace-pre-wrap break-all">
                    {step.error_message}
                  </p>
                )}
                {isFirst === false && step.status === 'waiting' && (
                  <p className="mt-1 text-xs text-gray-500">
                    Waiting on step {i}…
                  </p>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </Layout>
  )
}

function Stat({
  label,
  value,
  mono = true,
  small = false,
}: {
  label: string
  value: string | number
  mono?: boolean
  small?: boolean
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded px-3 py-2">
      <p className="text-[10px] text-gray-500 uppercase tracking-wide">{label}</p>
      <p
        className={`${small ? 'text-xs' : 'text-sm'} text-gray-200 ${mono ? 'font-mono' : ''} mt-0.5 truncate`}
      >
        {value}
      </p>
    </div>
  )
}
