import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Layout from '../components/Layout'
import JobForm from '../components/JobForm'
import StatusBadge from '../components/StatusBadge'
import type { Job } from '../types'
import { jobsApi } from '../api/jobs'
import { formatDate, JOB_TYPE_LABELS } from '../utils/format'

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'pending', label: 'Pending' },
  { value: 'running', label: 'Running' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'dead_letter', label: 'Dead Letter' },
]

export default function DashboardPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await jobsApi.list({
        page,
        page_size: 15,
        status: statusFilter || undefined,
      })
      setJobs(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page, statusFilter])

  useEffect(() => { void load() }, [load])

  // Auto-refresh while any job is running or pending
  useEffect(() => {
    const hasActive = jobs.some((j) => j.status === 'running' || j.status === 'pending')
    if (!hasActive) return
    const id = setInterval(() => void load(), 3000)
    return () => clearInterval(id)
  }, [jobs, load])

  function handleCreated(job: Job) {
    setShowForm(false)
    setJobs((prev) => [job, ...prev])
    setTotal((t) => t + 1)
  }

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold text-white">Jobs</h1>
          <p className="text-sm text-gray-500">{total} total</p>
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

      {/* Table */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {loading && jobs.length === 0 ? (
          <div className="py-12 text-center text-gray-600 text-sm">Loading…</div>
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

      {/* Pagination */}
      {total > 15 && (
        <div className="flex justify-center gap-2 mt-4">
          <button
            disabled={page === 1}
            onClick={() => setPage((p) => p - 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ← Prev
          </button>
          <span className="px-3 py-1 text-sm text-gray-500">
            Page {page} of {Math.ceil(total / 15)}
          </span>
          <button
            disabled={page * 15 >= total}
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
