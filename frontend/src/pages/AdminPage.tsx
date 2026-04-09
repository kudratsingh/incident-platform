import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import { TableRowSkeleton } from '../components/Skeleton'
import { useToast } from '../components/Toast'
import type { AuditLog, Job, User } from '../types'
import { adminApi } from '../api/admin'
import { AppError } from '../api/client'
import { formatDate, JOB_TYPE_LABELS } from '../utils/format'

type Tab = 'jobs' | 'users' | 'audit'

function AuditLogModal({ log, onClose }: { log: AuditLog; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-full max-w-lg mx-4 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-800">
          <div>
            <code className="text-blue-300 font-mono text-sm">{log.action}</code>
            <p className="text-xs text-gray-500 mt-0.5">{new Date(log.created_at).toLocaleString()}</p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-white text-lg leading-none">✕</button>
        </div>

        <div className="px-5 py-4 space-y-3 text-sm">
          <Row label="Log ID" value={log.id} mono />
          <Row label="Action" value={log.action} mono />
          {log.resource_type && (
            <Row label="Resource" value={`${log.resource_type} / ${log.resource_id ?? '—'}`} mono />
          )}
          {log.job_id && <Row label="Job ID" value={log.job_id} mono />}
          {log.user_id && <Row label="User ID" value={log.user_id} mono />}
          {log.request_id && <Row label="Request ID" value={log.request_id} mono />}
          {log.ip_address && <Row label="IP Address" value={log.ip_address} mono />}

          {log.extra_data && Object.keys(log.extra_data).length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-1.5 font-medium uppercase tracking-wide">Metadata</p>
              <pre className="bg-gray-800/70 rounded-lg p-3 text-xs font-mono text-gray-300 overflow-auto max-h-48 whitespace-pre-wrap break-all">
                {JSON.stringify(log.extra_data, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-3">
      <span className="text-gray-500 w-28 shrink-0 text-xs font-medium uppercase tracking-wide pt-0.5">{label}</span>
      <span className={`text-gray-200 break-all text-xs ${mono ? 'font-mono' : ''}`}>{value}</span>
    </div>
  )
}

const ROLE_COLORS: Record<string, string> = {
  admin: 'bg-purple-500/20 text-purple-300 border-purple-500/30',
  support: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  user: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
}

export default function AdminPage() {
  const toast = useToast()
  const [tab, setTab] = useState<Tab>('jobs')
  const [jobs, setJobs] = useState<Job[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [logs, setLogs] = useState<AuditLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')
  const [traceFilter, setTraceFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [selectedLog, setSelectedLog] = useState<AuditLog | null>(null)

  const loadJobs = useCallback(async () => {
    setLoading(true)
    try {
      const res = await adminApi.listJobs({
        page,
        page_size: 20,
        status: statusFilter || undefined,
        trace_id: traceFilter || undefined,
      })
      setJobs(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page, statusFilter, traceFilter])

  const loadUsers = useCallback(async () => {
    setLoading(true)
    try {
      const res = await adminApi.listUsers(page)
      setUsers(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page])

  const loadLogs = useCallback(async () => {
    setLoading(true)
    try {
      const res = await adminApi.listAuditLogs({ page })
      setLogs(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page])

  useEffect(() => {
    if (tab === 'jobs') void loadJobs()
    else if (tab === 'users') void loadUsers()
    else void loadLogs()
  }, [tab, loadJobs, loadUsers, loadLogs])

  async function replay(jobId: string) {
    try {
      await adminApi.replayJob(jobId)
      toast.success(`Job ${jobId.slice(0, 8)}… queued for replay`)
      void loadJobs()
    } catch (err) {
      toast.error(err instanceof AppError ? err.message : 'Replay failed')
    }
  }

  async function resolve(jobId: string) {
    try {
      await adminApi.resolveIncident(jobId)
      toast.success(`Incident ${jobId.slice(0, 8)}… marked resolved`)
      void loadJobs()
    } catch (err) {
      toast.error(err instanceof AppError ? err.message : 'Resolve failed')
    }
  }

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold text-white">Admin Console</h1>
          <p className="text-sm text-gray-500">{total} records</p>
        </div>
        <div className="flex gap-1 bg-gray-800/60 rounded-lg p-1">
          {(['jobs', 'users', 'audit'] as Tab[]).map((t) => (
            <button
              key={t}
              onClick={() => { setTab(t); setPage(1) }}
              className={`px-4 py-1.5 rounded text-sm font-medium transition-colors capitalize ${
                tab === t ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'
              }`}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {tab === 'jobs' && (
        <>
          {/* Filters */}
          <div className="flex gap-3 mb-4">
            <input
              type="text"
              placeholder="Filter by trace ID…"
              value={traceFilter}
              onChange={(e) => { setTraceFilter(e.target.value); setPage(1) }}
              className="bg-gray-800/60 border border-gray-700/50 rounded px-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/50 w-64 font-mono"
            />
            <select
              value={statusFilter}
              onChange={(e) => { setStatusFilter(e.target.value); setPage(1) }}
              className="bg-gray-800/60 border border-gray-700/50 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500/50"
            >
              <option value="">All statuses</option>
              {['pending','running','completed','failed','dead_letter'].map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            {loading ? (
              <table className="w-full text-sm">
                <tbody className="divide-y divide-gray-800/60">
                  {Array.from({ length: 8 }).map((_, i) => <TableRowSkeleton key={i} />)}
                </tbody>
              </table>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-xs text-gray-500">
                    <th className="text-left px-4 py-3 font-medium">Type</th>
                    <th className="text-left px-4 py-3 font-medium">Status</th>
                    <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Trace ID</th>
                    <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">Created</th>
                    <th className="text-right px-4 py-3 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {jobs.map((job) => (
                    <tr key={job.id} className="hover:bg-gray-800/30 transition-colors">
                      <td className="px-4 py-3">
                        <Link to={`/jobs/${job.id}`} className="text-gray-200 hover:text-white">
                          {JOB_TYPE_LABELS[job.type] ?? job.type}
                        </Link>
                        <code className="text-xs text-gray-600 block font-mono">
                          {job.id.slice(0, 8)}…
                        </code>
                      </td>
                      <td className="px-4 py-3"><StatusBadge status={job.status} /></td>
                      <td className="px-4 py-3 hidden md:table-cell">
                        <code className="text-xs font-mono text-gray-500">
                          {job.trace_id ? job.trace_id.slice(0, 14) + '…' : '—'}
                        </code>
                      </td>
                      <td className="px-4 py-3 hidden lg:table-cell text-gray-500 text-xs">
                        {formatDate(job.created_at)}
                      </td>
                      <td className="px-4 py-3 text-right space-x-2">
                        {(job.status === 'failed' || job.status === 'dead_letter') && (
                          <button
                            onClick={() => void replay(job.id)}
                            className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
                          >
                            Replay
                          </button>
                        )}
                        {(job.status === 'failed' || job.status === 'dead_letter') && (
                          <button
                            onClick={() => void resolve(job.id)}
                            className="text-xs text-green-400 hover:text-green-300 transition-colors"
                          >
                            Resolve
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}

      {tab === 'users' && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          {loading ? (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-gray-800/60">
                {Array.from({ length: 8 }).map((_, i) => <TableRowSkeleton key={i} />)}
              </tbody>
            </table>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-xs text-gray-500">
                  <th className="text-left px-4 py-3 font-medium">Email</th>
                  <th className="text-left px-4 py-3 font-medium">Role</th>
                  <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Status</th>
                  <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">Joined</th>
                  <th className="text-left px-4 py-3 font-medium font-mono hidden lg:table-cell">ID</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {users.map((u) => (
                  <tr key={u.id} className="hover:bg-gray-800/30 transition-colors">
                    <td className="px-4 py-3 text-gray-200">{u.email}</td>
                    <td className="px-4 py-3">
                      <span className={`inline-flex px-2 py-0.5 rounded-full text-xs font-medium border ${ROLE_COLORS[u.role] ?? ROLE_COLORS.user}`}>
                        {u.role}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <span className={`text-xs font-medium ${u.is_active ? 'text-green-400' : 'text-red-400'}`}>
                        {u.is_active ? 'active' : 'inactive'}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell text-xs text-gray-500">
                      {formatDate(u.created_at)}
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <code className="text-xs font-mono text-gray-600">{u.id.slice(0, 8)}…</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'audit' && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          {loading ? (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-gray-800/60">
                {Array.from({ length: 8 }).map((_, i) => <TableRowSkeleton key={i} />)}
              </tbody>
            </table>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-xs text-gray-500">
                  <th className="text-left px-4 py-3 font-medium">Action</th>
                  <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Resource</th>
                  <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">Request ID</th>
                  <th className="text-left px-4 py-3 font-medium">Time</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {logs.map((log) => (
                  <tr
                    key={log.id}
                    className="hover:bg-gray-800/30 cursor-pointer transition-colors"
                    onClick={() => setSelectedLog(log)}
                  >
                    <td className="px-4 py-3">
                      <code className="text-xs font-mono text-blue-300">{log.action}</code>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell text-xs text-gray-500 font-mono">
                      {log.resource_type ? `${log.resource_type}:${(log.resource_id ?? '').slice(0, 8)}…` : '—'}
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <code className="text-xs font-mono text-gray-600">
                        {log.request_id ? log.request_id.slice(0, 14) + '…' : '—'}
                      </code>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">
                      {formatDate(log.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* Pagination */}
      {total > 20 && (
        <div className="flex justify-center gap-2 mt-4">
          <button disabled={page === 1} onClick={() => setPage((p) => p - 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30">
            ← Prev
          </button>
          <span className="px-3 py-1 text-sm text-gray-500">Page {page}</span>
          <button disabled={page * 20 >= total} onClick={() => setPage((p) => p + 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30">
            Next →
          </button>
        </div>
      )}
      {selectedLog && <AuditLogModal log={selectedLog} onClose={() => setSelectedLog(null)} />}
    </Layout>
  )
}
