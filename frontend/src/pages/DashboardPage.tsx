import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Layout from '../components/Layout'
import JobForm from '../components/JobForm'
import StatusBadge from '../components/StatusBadge'
import ErrorState from '../components/ErrorState'
import { TableRowSkeleton } from '../components/Skeleton'
import type { Job } from '../types'
import { jobsApi } from '../api/jobs'
import { useAsyncData } from '../hooks/useAsyncData'
import { formatDate, JOB_TYPE_LABELS } from '../utils/format'

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'running', label: 'Running' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'dead_letter', label: 'Dead Letter' },
]

const PAGE_SIZE = 15

export default function DashboardPage() {
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(
    () =>
      jobsApi.list({
        page,
        page_size: PAGE_SIZE,
        status: statusFilter || undefined,
      }),
    [page, statusFilter],
  )
  const { data, loading, error, reload, setData } = useAsyncData(load, {
    errorMessage: 'Could not load jobs.',
  })

  const jobs = data?.items ?? []
  const total = data?.total ?? 0

  // Auto-refresh while any job is running or pending. Keyed on the boolean,
  // not the array: the array is a fresh reference every render, which would
  // tear down and re-arm the interval on each one.
  const hasActiveJob = jobs.some((j) => j.status === 'running' || j.status === 'pending')
  useEffect(() => {
    if (!hasActiveJob) return
    const id = setInterval(reload, 3000)
    return () => clearInterval(id)
  }, [hasActiveJob, reload])

  function handleCreated(job: Job) {
    setShowForm(false)
    // A new job is `pending`. It only belongs in the list actually on screen
    // when this page and filter would have returned it; anywhere else,
    // prepending it puts a row in a table that claims to exclude it. Refetch
    // instead, so what is shown keeps matching the filter it advertises.
    const belongsHere = page === 1 && (statusFilter === '' || statusFilter === job.status)
    if (belongsHere && data) {
      setData((prev) =>
        prev ? { ...prev, items: [job, ...prev.items], total: prev.total + 1 } : prev,
      )
    } else {
      reload()
    }
  }

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold text-white">Jobs</h1>
          <p className="text-sm text-gray-500">{data ? `${total} total` : '—'}</p>
        </div>
        <button
          onClick={() => setShowForm((v) => !v)}
          className="px-4 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors"
        >
          {showForm ? 'Cancel' : '+ New job'}
        </button>
      </div>

      {/* Job creation form */}
      {showForm && (
        <div className="mb-6 bg-gray-900 border border-gray-800 rounded-lg p-5">
          <h2 className="text-sm font-medium text-gray-300 mb-4">New job</h2>
          <JobForm onCreated={handleCreated} />
        </div>
      )}

      {/* Filters */}
      <div className="flex gap-2 mb-4">
        {STATUS_OPTIONS.map((opt) => (
          <button
            key={opt.value}
            onClick={() => { setStatusFilter(opt.value); setPage(1) }}
            className={`px-3 py-1 rounded-full text-xs font-medium transition-colors border ${
              statusFilter === opt.value
                ? 'bg-blue-600/20 border-blue-500/50 text-blue-300'
                : 'bg-gray-800/60 border-gray-700/50 text-gray-400 hover:text-gray-200'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* A failed auto-refresh keeps the rows we already have — they are still
          the last thing the server told us — but says so rather than letting
          the table quietly go stale. */}
      {error && jobs.length > 0 && (
        <div
          role="alert"
          className="mb-3 flex items-center gap-3 text-xs text-red-400 bg-red-900/20 border border-red-800/30 rounded px-3 py-2"
        >
          <span>{error} Showing the last successful result.</span>
          <button onClick={reload} className="ml-auto underline hover:text-red-300">
            Retry
          </button>
        </div>
      )}

      {/* Table */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {loading && jobs.length === 0 ? (
          <table className="w-full text-sm">
            <tbody className="divide-y divide-gray-800/60">
              {Array.from({ length: 8 }).map((_, i) => <TableRowSkeleton key={i} />)}
            </tbody>
          </table>
        ) : error && jobs.length === 0 ? (
          // "No jobs found" is a statement about the account, so it must not
          // be what a failed GET /jobs produces.
          <ErrorState message={error} onRetry={reload} />
        ) : jobs.length === 0 ? (
          <div className="py-12 text-center text-gray-600 text-sm">No jobs found</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-xs text-gray-500">
                <th className="text-left px-4 py-3 font-medium">Type</th>
                <th className="text-left px-4 py-3 font-medium">Status</th>
                <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Trace ID</th>
                <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">Created</th>
                <th className="text-left px-4 py-3 font-medium">Retries</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/60">
              {jobs.map((job) => (
                <tr
                  key={job.id}
                  className="hover:bg-gray-800/40 transition-colors cursor-pointer"
                >
                  <td className="px-4 py-3">
                    <Link to={`/jobs/${job.id}`} className="block">
                      <span className="font-medium text-gray-200">
                        {JOB_TYPE_LABELS[job.type] ?? job.type}
                      </span>
                      <span className="text-xs text-gray-600 block font-mono truncate max-w-[140px]">
                        {job.id.slice(0, 8)}…
                      </span>
                    </Link>
                  </td>
                  <td className="px-4 py-3">
                    <Link to={`/jobs/${job.id}`}>
                      <StatusBadge status={job.status} />
                    </Link>
                  </td>
                  <td className="px-4 py-3 hidden md:table-cell">
                    <Link to={`/jobs/${job.id}`}>
                      <code className="text-xs font-mono text-gray-500">
                        {job.trace_id ? job.trace_id.slice(0, 12) + '…' : '—'}
                      </code>
                    </Link>
                  </td>
                  <td className="px-4 py-3 hidden lg:table-cell text-gray-500 text-xs">
                    <Link to={`/jobs/${job.id}`}>{formatDate(job.created_at)}</Link>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs font-mono">
                    <Link to={`/jobs/${job.id}`}>
                      {job.retry_count}/{job.max_retries}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Pagination — only a successful response knows how many pages exist. */}
      {data && total > PAGE_SIZE && (
        <div className="flex justify-center gap-2 mt-4">
          <button
            disabled={page === 1}
            onClick={() => setPage((p) => p - 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ← Prev
          </button>
          <span className="px-3 py-1 text-sm text-gray-500">
            Page {page} of {Math.ceil(total / PAGE_SIZE)}
          </span>
          <button
            disabled={page * PAGE_SIZE >= total}
            onClick={() => setPage((p) => p + 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Next →
          </button>
        </div>
      )}
    </Layout>
  )
}
