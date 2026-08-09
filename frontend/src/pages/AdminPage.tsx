import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import { TableRowSkeleton } from '../components/Skeleton'
import { useToast } from '../components/Toast'
import type {
  AuditLog,
  IncidentDigest,
  Job,
  Runbook,
  SLOState,
  SystemStats,
  Tenant,
  User,
} from '../types'
import { adminApi } from '../api/admin'
import { AppError } from '../api/client'
import { useAuth } from '../hooks/useAuth'
import { formatDate, JOB_TYPE_LABELS } from '../utils/format'

type Tab =
  | 'overview'
  | 'jobs'
  | 'dlq'
  | 'runbooks'
  | 'users'
  | 'tenants'
  | 'digests'
  | 'audit'

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

function CreateTenantModal({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (tenant: Tenant) => void
}) {
  const [slug, setSlug] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      const created = await adminApi.createTenant(slug.trim(), name.trim())
      onCreated(created)
    } catch (e2) {
      setErr(e2 instanceof AppError ? e2.message : 'Failed to create tenant')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <form
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleCreate}
        className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-full max-w-md mx-4 overflow-hidden"
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-800">
          <h3 className="text-sm font-medium text-white">New tenant</h3>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-500 hover:text-white text-lg leading-none"
          >
            ✕
          </button>
        </div>
        <div className="px-5 py-4 space-y-3 text-sm">
          <div>
            <label className="block text-xs text-gray-500 mb-1 uppercase tracking-wide">Slug</label>
            <input
              autoFocus
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="acme"
              pattern="[a-zA-Z0-9_-]+"
              required
              className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded font-mono focus:outline-none focus:border-blue-500"
            />
            <p className="text-xs text-gray-600 mt-1">Lowercase alphanumerics, '-', '_'. Permanent.</p>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1 uppercase tracking-wide">Display name</label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme Corp"
              required
              className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded focus:outline-none focus:border-blue-500"
            />
          </div>
          {err && <div className="text-red-400 text-xs">{err}</div>}
        </div>
        <div className="px-5 py-3 border-t border-gray-800 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="text-sm px-3 py-1.5 rounded text-gray-400 hover:text-white"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !slug.trim() || !name.trim()}
            className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
          >
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </form>
    </div>
  )
}

function TenantRow({
  tenant,
  onSave,
}: {
  tenant: Tenant
  onSave: (id: string, rate: number, quota: number) => Promise<void>
}) {
  const [rate, setRate] = useState(String(tenant.rate_limit_per_minute))
  const [quota, setQuota] = useState(String(tenant.quota_jobs_per_month))
  const [busy, setBusy] = useState(false)

  // Reset when the tenant prop refreshes (after save the parent rebroadcasts).
  useEffect(() => {
    setRate(String(tenant.rate_limit_per_minute))
    setQuota(String(tenant.quota_jobs_per_month))
  }, [tenant.rate_limit_per_minute, tenant.quota_jobs_per_month])

  const dirty =
    rate !== String(tenant.rate_limit_per_minute) ||
    quota !== String(tenant.quota_jobs_per_month)

  async function handleSave() {
    const r = Number(rate)
    const q = Number(quota)
    if (!Number.isInteger(r) || r < 0 || !Number.isInteger(q) || q < 0) return
    setBusy(true)
    try {
      await onSave(tenant.id, r, q)
    } finally {
      setBusy(false)
    }
  }

  return (
    <tr className="hover:bg-gray-800/30 transition-colors">
      <td className="px-4 py-3">
        <Link to={`/admin/tenants/${tenant.id}`} className="block hover:underline">
          <div className="text-gray-200">{tenant.name}</div>
          <code className="text-xs font-mono text-blue-300">{tenant.slug}</code>
        </Link>
      </td>
      <td className="px-4 py-3 hidden md:table-cell text-gray-400">{tenant.users}</td>
      <td className="px-4 py-3 hidden md:table-cell text-gray-400">{tenant.jobs}</td>
      <td className="px-4 py-3">
        <input
          type="number"
          min={0}
          value={rate}
          onChange={(e) => setRate(e.target.value)}
          className="w-24 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-sm font-mono focus:outline-none focus:border-blue-500"
        />
      </td>
      <td className="px-4 py-3">
        <input
          type="number"
          min={0}
          value={quota}
          onChange={(e) => setQuota(e.target.value)}
          className="w-32 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-sm font-mono focus:outline-none focus:border-blue-500"
        />
      </td>
      <td className="px-4 py-3 text-right">
        <button
          disabled={!dirty || busy}
          onClick={handleSave}
          className="text-xs font-medium px-2.5 py-1 rounded bg-blue-600 text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
      </td>
    </tr>
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

function UserStatsModal({ user, onClose }: { user: User; onClose: () => void }) {
  const [stats, setStats] = useState<SystemStats | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    adminApi
      .userStats(user.id)
      .then((s) => {
        if (!cancelled) setStats(s)
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof AppError ? e.message : 'Failed to load stats')
      })
    return () => {
      cancelled = true
    }
  }, [user.id])

  const totals = stats?.by_status ?? {}
  const total = Object.values(totals).reduce((a, b) => a + b, 0)

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
            <p className="text-sm font-medium text-white">{user.email}</p>
            <p className="text-xs text-gray-500 mt-0.5">
              {user.role} · joined {formatDate(user.created_at)}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-500 hover:text-white text-lg leading-none">✕</button>
        </div>
        <div className="px-5 py-4 space-y-3 text-sm">
          <Row label="User ID" value={user.id} mono />
          <Row label="Total jobs" value={String(total)} />
          {err && (
            <div className="text-xs text-red-400 bg-red-900/20 border border-red-800/30 rounded px-3 py-2">
              {err}
            </div>
          )}
          {!err && stats === null && (
            <p className="text-xs text-gray-500">Loading…</p>
          )}
          {!err && stats && (
            <div>
              <p className="text-xs text-gray-500 mb-2 font-medium uppercase tracking-wide">By status</p>
              {total === 0 ? (
                <p className="text-xs text-gray-600">No jobs yet.</p>
              ) : (
                <div className="grid grid-cols-2 gap-2">
                  {Object.entries(totals)
                    .filter(([, n]) => n > 0)
                    .sort(([, a], [, b]) => b - a)
                    .map(([status, n]) => (
                      <div
                        key={status}
                        className="flex items-center justify-between bg-gray-800/60 rounded px-3 py-1.5"
                      >
                        <StatusBadge status={status as never} />
                        <span className="font-mono text-xs text-gray-300">{n}</span>
                      </div>
                    ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function AdminPage() {
  const toast = useToast()
  const { user: currentUser } = useAuth()
  const isPlatformAdmin = currentUser?.is_platform_admin === true
  const [tab, setTab] = useState<Tab>('overview')
  const [showCreateTenant, setShowCreateTenant] = useState(false)
  const [stats, setStats] = useState<SystemStats | null>(null)
  const [slos, setSlos] = useState<SLOState[] | null>(null)
  const [runbooks, setRunbooks] = useState<Runbook[]>([])
  const [selectedRunbook, setSelectedRunbook] = useState<Runbook | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [logs, setLogs] = useState<AuditLog[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState('')
  const [traceFilter, setTraceFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [selectedLog, setSelectedLog] = useState<AuditLog | null>(null)
  const [principalFilter, setPrincipalFilter] = useState<
    'all' | 'user' | 'service_account'
  >('all')
  const [selectedUser, setSelectedUser] = useState<User | null>(null)
  const [dlqStats, setDlqStats] = useState<{ total: number; by_type: Record<string, number> } | null>(null)
  const [dlqJobs, setDlqJobs] = useState<Job[]>([])
  const [tenants, setTenants] = useState<Tenant[]>([])
  const [nlQuestion, setNlQuestion] = useState('')
  const [nlBusy, setNlBusy] = useState(false)
  const [nlSpec, setNlSpec] = useState<Record<string, unknown> | null>(null)
  const [nlError, setNlError] = useState<string | null>(null)
  const [digests, setDigests] = useState<IncidentDigest[]>([])
  const [digestBusy, setDigestBusy] = useState(false)
  const [digestError, setDigestError] = useState<string | null>(null)

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
      const res = await adminApi.listAuditLogs({
        page,
        principal_type: principalFilter === 'all' ? undefined : principalFilter,
      })
      setLogs(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page, principalFilter])

  const loadDlq = useCallback(async () => {
    setLoading(true)
    try {
      const [stats, jobsRes] = await Promise.all([
        adminApi.dlqStats(),
        adminApi.listJobs({ page, page_size: 20, status: 'dead_letter' }),
      ])
      setDlqStats(stats)
      setDlqJobs(jobsRes.items)
      setTotal(jobsRes.total)
    } finally {
      setLoading(false)
    }
  }, [page])

  // Keep the badge fresh on every tab switch so admins see the current count.
  useEffect(() => {
    void adminApi.dlqStats().then(setDlqStats).catch(() => {})
  }, [tab])

  const loadOverview = useCallback(async () => {
    setLoading(true)
    try {
      const [s, sloResp] = await Promise.all([
        adminApi.systemStats(),
        adminApi.slos().catch(() => ({ slos: [] })),
      ])
      setStats(s)
      setSlos(sloResp.slos)
    } finally {
      setLoading(false)
    }
  }, [])

  const loadTenants = useCallback(async () => {
    setLoading(true)
    try {
      const r = await adminApi.listTenants(page)
      setTenants(r.items)
      setTotal(r.total)
    } finally {
      setLoading(false)
    }
  }, [page])

  async function runNlQuery(question: string): Promise<void> {
    const trimmed = question.trim()
    if (!trimmed) return
    setNlBusy(true)
    setNlError(null)
    try {
      const resp = await adminApi.nlQuery(trimmed)
      setJobs(resp.items)
      setTotal(resp.total)
      setNlSpec(resp.spec)
      // Clear the structured filters so the user sees that the NL spec is
      // what's currently applied — otherwise it looks ambiguous.
      setStatusFilter('')
      setTraceFilter('')
      setPage(1)
    } catch (err) {
      setNlError(
        err instanceof AppError
          ? err.message
          : 'Could not run that query. Try rephrasing or check that LLM_NL_QUERY_ENABLED is set.',
      )
    } finally {
      setNlBusy(false)
    }
  }

  function clearNl(): void {
    setNlQuestion('')
    setNlSpec(null)
    setNlError(null)
    void loadJobs()
  }

  const loadDigests = useCallback(async () => {
    setLoading(true)
    try {
      const r = await adminApi.listDigests()
      setDigests(r.items)
    } finally {
      setLoading(false)
    }
  }, [])

  async function generateDigestNow(): Promise<void> {
    setDigestBusy(true)
    setDigestError(null)
    try {
      const resp = await adminApi.generateDigest()
      if ('summary' in resp && resp.summary === null) {
        setDigestError(
          'No jobs in the last window — nothing to summarize. Try a wider hours range.',
        )
      } else {
        await loadDigests()
      }
    } catch (err) {
      setDigestError(
        err instanceof AppError
          ? err.message
          : 'Failed to generate digest. Check that LLM_DIGEST_ENABLED is set.',
      )
    } finally {
      setDigestBusy(false)
    }
  }

  async function saveTenantLimits(
    id: string,
    rate: number,
    quota: number,
  ): Promise<void> {
    try {
      const updated = await adminApi.updateTenantLimits(id, {
        rate_limit_per_minute: rate,
        quota_jobs_per_month: quota,
      })
      setTenants((prev) =>
        prev.map((t) => (t.id === id ? { ...t, ...updated } : t)),
      )
      toast.success(`Limits saved for ${updated.slug}`)
    } catch (err) {
      toast.error(err instanceof AppError ? err.message : 'Failed to update limits')
    }
  }

  const loadRunbooks = useCallback(async () => {
    setLoading(true)
    try {
      const r = await adminApi.runbooks()
      setRunbooks(r.items)
    } finally {
      setLoading(false)
    }
  }, [])

  // Auto-refresh the overview every 5s so the CQRS read-model lag is visible.
  useEffect(() => {
    if (tab !== 'overview') return
    void loadOverview()
    const t = setInterval(() => void loadOverview(), 5000)
    return () => clearInterval(t)
  }, [tab, loadOverview])

  useEffect(() => {
    if (tab === 'overview') return  // handled by its own polling effect above
    if (tab === 'jobs') void loadJobs()
    else if (tab === 'dlq') void loadDlq()
    else if (tab === 'runbooks') void loadRunbooks()
    else if (tab === 'users') void loadUsers()
    else if (tab === 'tenants') void loadTenants()
    else if (tab === 'digests') void loadDigests()
    else void loadLogs()
  }, [tab, loadJobs, loadDlq, loadRunbooks, loadUsers, loadTenants, loadDigests, loadLogs])

  async function replay(jobId: string) {
    try {
      await adminApi.replayJob(jobId)
      toast.success(`Job ${jobId.slice(0, 8)}… queued for replay`)
      if (tab === 'dlq') void loadDlq()
      else void loadJobs()
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
          {(
            [
              'overview',
              'jobs',
              'dlq',
              'runbooks',
              'users',
              ...(isPlatformAdmin ? (['tenants'] as Tab[]) : []),
              'digests',
              'audit',
            ] as Tab[]
          ).map((t) => (
            <button
              key={t}
              onClick={() => { setTab(t); setPage(1) }}
              className={`px-4 py-1.5 rounded text-sm font-medium transition-colors capitalize inline-flex items-center gap-2 ${
                tab === t ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'
              }`}
            >
              {t === 'dlq' ? 'DLQ' : t}
              {t === 'dlq' && dlqStats && dlqStats.total > 0 && (
                <span className="bg-red-500/20 text-red-300 border border-red-500/40 rounded-full px-2 py-0.5 text-[10px] font-mono leading-none">
                  {dlqStats.total}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {tab === 'overview' && (
        <>
          <div className="mb-4 flex items-center gap-2">
            <span className="text-xs text-gray-500">
              CQRS read model — denormalized Redis sets, refreshed every 5s.
            </span>
            <span className="ml-auto text-xs text-gray-600 flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-pulse" />
              live
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
            {(['running', 'completed', 'failed', 'dead_letter'] as const).map(
              (st) => (
                <StatCard
                  key={st}
                  label={st.replace('_', ' ')}
                  value={stats?.by_status?.[st] ?? 0}
                  loading={loading && stats === null}
                  color={
                    st === 'completed'
                      ? 'green'
                      : st === 'running'
                        ? 'blue'
                        : st === 'failed'
                          ? 'red'
                          : 'red-dark'
                  }
                />
              ),
            )}
          </div>

          <div className="bg-gray-900 border border-gray-800 rounded-lg p-5">
            <p className="text-xs text-gray-500">
              These counts come from <code className="text-blue-300">/admin/stats</code>{' '}
              which reads denormalized job-status sets in Redis. No SQL aggregate
              on the jobs table — eventually consistent with the write side, with
              latency dominated by Kafka consumer-group lag.
            </p>
          </div>

          {/* SLOs */}
          {slos && slos.length > 0 && (
            <div className="mt-6">
              <h2 className="text-sm font-medium text-gray-300 mb-3">
                Service Level Objectives
              </h2>
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                {slos.map((s) => (
                  <SLOCard
                    key={s.id}
                    slo={s}
                    onOpenRunbook={async () => {
                      try {
                        const rb = await adminApi.runbook(s.runbook_id)
                        setSelectedRunbook(rb)
                      } catch {
                        /* runbook missing — silently ignore */
                      }
                    }}
                  />
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {tab === 'runbooks' && (
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          {loading ? (
            <div className="px-6 py-12 text-center text-sm text-gray-500">Loading…</div>
          ) : runbooks.length === 0 ? (
            <div className="px-6 py-12 text-center text-sm text-gray-500">
              No runbooks found.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-800 text-xs text-gray-500">
                  <th className="text-left px-4 py-3 font-medium">ID</th>
                  <th className="text-left px-4 py-3 font-medium">Title</th>
                  <th className="text-left px-4 py-3 font-medium hidden md:table-cell">
                    Severity
                  </th>
                  <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">
                    Alarm
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/60">
                {runbooks.map((rb) => (
                  <tr
                    key={rb.id}
                    onClick={() => setSelectedRunbook(rb)}
                    className="hover:bg-gray-800/30 cursor-pointer transition-colors"
                  >
                    <td className="px-4 py-3">
                      <code className="text-xs font-mono text-blue-300">{rb.id}</code>
                    </td>
                    <td className="px-4 py-3 text-gray-200">{rb.title}</td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <SeverityPill severity={rb.severity} />
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell text-xs text-gray-500 font-mono">
                      {rb.alarm ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'jobs' && (
        <>
          {/* Natural-language query */}
          <form
            onSubmit={(e) => {
              e.preventDefault()
              void runNlQuery(nlQuestion)
            }}
            className="mb-3 flex gap-2"
          >
            <div className="relative flex-1">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-purple-400 text-xs font-mono pointer-events-none">
                ask
              </span>
              <input
                type="text"
                value={nlQuestion}
                onChange={(e) => setNlQuestion(e.target.value)}
                placeholder='e.g. "dead-lettered CSV uploads in the last hour with at least 2 retries"'
                className="w-full bg-gray-800/60 border border-purple-900/40 rounded pl-12 pr-3 py-1.5 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500/60"
              />
            </div>
            <button
              type="submit"
              disabled={nlBusy || !nlQuestion.trim()}
              className="text-sm px-3 py-1.5 rounded bg-purple-600 text-white hover:bg-purple-500 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
            >
              {nlBusy ? 'Asking…' : 'Ask'}
            </button>
            {nlSpec && (
              <button
                type="button"
                onClick={clearNl}
                className="text-xs text-gray-400 hover:text-white px-2"
              >
                Clear
              </button>
            )}
          </form>
          {nlError && (
            <div className="mb-3 text-xs text-red-400 bg-red-900/20 border border-red-800/30 rounded px-3 py-2">
              {nlError}
            </div>
          )}
          {nlSpec && (
            <div className="mb-3 text-xs text-gray-400 bg-purple-900/15 border border-purple-800/40 rounded px-3 py-2">
              <span className="text-purple-300 font-mono mr-2">filter</span>
              <code className="font-mono text-gray-300">{JSON.stringify(nlSpec)}</code>
            </div>
          )}

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

      {tab === 'dlq' && (
        <>
          {dlqStats && dlqStats.total > 0 && (
            <div className="mb-4 flex flex-wrap gap-2">
              {Object.entries(dlqStats.by_type).map(([type, n]) => (
                <span
                  key={type}
                  className="bg-red-500/10 border border-red-500/20 text-red-200 rounded-full px-3 py-1 text-xs font-mono"
                >
                  {JOB_TYPE_LABELS[type] ?? type}: {n}
                </span>
              ))}
            </div>
          )}

          <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            {loading ? (
              <table className="w-full text-sm">
                <tbody className="divide-y divide-gray-800/60">
                  {Array.from({ length: 8 }).map((_, i) => <TableRowSkeleton key={i} />)}
                </tbody>
              </table>
            ) : dlqJobs.length === 0 ? (
              <div className="px-6 py-12 text-center text-sm text-gray-500">
                Dead letter queue is empty.
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-xs text-gray-500">
                    <th className="text-left px-4 py-3 font-medium">Job</th>
                    <th className="text-left px-4 py-3 font-medium">Error</th>
                    <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Attempts</th>
                    <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">Failed</th>
                    <th className="text-right px-4 py-3 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {dlqJobs.map((job) => (
                    <tr key={job.id} className="hover:bg-gray-800/30 transition-colors">
                      <td className="px-4 py-3 align-top">
                        <Link to={`/jobs/${job.id}`} className="text-gray-200 hover:text-white">
                          {JOB_TYPE_LABELS[job.type] ?? job.type}
                        </Link>
                        <code className="text-xs text-gray-600 block font-mono">
                          {job.id.slice(0, 8)}…
                        </code>
                      </td>
                      <td className="px-4 py-3 align-top max-w-md">
                        <p className="text-xs text-red-300 font-mono whitespace-pre-wrap break-all">
                          {job.error_message ?? '—'}
                        </p>
                      </td>
                      <td className="px-4 py-3 hidden md:table-cell align-top">
                        <span className="text-xs font-mono text-gray-400">
                          {job.retry_count}/{job.max_retries}
                        </span>
                        {job.dead_lettered_by === 'llm_retry_policy' && (
                          <span
                            className="ml-2 text-[10px] px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-300 font-mono border border-purple-800/60"
                            title="LLM-guided retry policy dead-lettered this job before its retries were exhausted. See audit log for reasoning."
                          >
                            LLM
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 hidden lg:table-cell text-gray-500 text-xs align-top">
                        {job.completed_at ? formatDate(job.completed_at) : '—'}
                      </td>
                      <td className="px-4 py-3 text-right space-x-2 align-top">
                        <button
                          onClick={() => void replay(job.id)}
                          className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
                        >
                          Replay
                        </button>
                        <button
                          onClick={() => void resolve(job.id)}
                          className="text-xs text-green-400 hover:text-green-300 transition-colors"
                        >
                          Resolve
                        </button>
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
                  <tr
                    key={u.id}
                    onClick={() => setSelectedUser(u)}
                    className="hover:bg-gray-800/30 transition-colors cursor-pointer"
                  >
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

      {tab === 'tenants' && (
        <>
          <div className="flex justify-end mb-3">
            <button
              onClick={() => setShowCreateTenant(true)}
              className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white hover:bg-blue-500 font-medium"
            >
              + New tenant
            </button>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            {loading ? (
              <table className="w-full text-sm">
                <tbody className="divide-y divide-gray-800/60">
                  {Array.from({ length: 6 }).map((_, i) => <TableRowSkeleton key={i} />)}
                </tbody>
              </table>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800 text-xs text-gray-500">
                    <th className="text-left px-4 py-3 font-medium">Tenant</th>
                    <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Users</th>
                    <th className="text-left px-4 py-3 font-medium hidden md:table-cell">Jobs</th>
                    <th className="text-left px-4 py-3 font-medium">Rate (per min)</th>
                    <th className="text-left px-4 py-3 font-medium">Monthly quota</th>
                    <th className="px-4 py-3" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800/60">
                  {tenants.map((t) => (
                    <TenantRow key={t.id} tenant={t} onSave={saveTenantLimits} />
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {showCreateTenant && (
            <CreateTenantModal
              onClose={() => setShowCreateTenant(false)}
              onCreated={(created) => {
                setTenants((prev) => [created, ...prev])
                setShowCreateTenant(false)
                toast.success(`Tenant ${created.slug} created`)
              }}
            />
          )}
        </>
      )}

      {tab === 'digests' && (
        <>
          <div className="flex justify-between items-center mb-3">
            <p className="text-xs text-gray-500">
              Daily LLM-written digests of failures, recurring patterns, and recommended actions for your tenant.
            </p>
            <button
              onClick={() => void generateDigestNow()}
              disabled={digestBusy}
              className="text-sm px-3 py-1.5 rounded bg-purple-600 text-white hover:bg-purple-500 disabled:opacity-40 disabled:cursor-not-allowed font-medium"
            >
              {digestBusy ? 'Generating…' : 'Generate now'}
            </button>
          </div>
          {digestError && (
            <div className="mb-3 text-xs text-red-400 bg-red-900/20 border border-red-800/30 rounded px-3 py-2">
              {digestError}
            </div>
          )}
          {loading ? (
            <div className="space-y-3">
              {Array.from({ length: 3 }).map((_, i) => (
                <div
                  key={i}
                  className="bg-gray-900 border border-gray-800 rounded-lg h-32 animate-pulse"
                />
              ))}
            </div>
          ) : digests.length === 0 ? (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-8 text-sm text-gray-500 text-center">
              No digests yet. Click "Generate now" to write one for the last 24 hours.
            </div>
          ) : (
            <div className="space-y-3">
              {digests.map((d) => (
                <article
                  key={d.id}
                  className="bg-gray-900 border border-gray-800 rounded-lg p-4"
                >
                  <header className="flex items-center justify-between mb-2 text-xs text-gray-500">
                    <span className="font-mono">
                      {formatDate(d.window_start)} → {formatDate(d.window_end)}
                    </span>
                    <span className="font-mono text-gray-600">
                      {d.model_used}
                    </span>
                  </header>
                  <p className="text-sm text-gray-200 leading-relaxed">
                    {d.summary}
                  </p>
                  {!!d.highlights?.key_concerns?.length && (
                    <div className="mt-3">
                      <h4 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
                        Key concerns
                      </h4>
                      <ul className="list-disc list-inside text-xs text-gray-300 space-y-0.5">
                        {d.highlights.key_concerns!.map((c, i) => (
                          <li key={i}>{c}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {!!d.highlights?.recommended_actions?.length && (
                    <div className="mt-3">
                      <h4 className="text-xs uppercase tracking-wide text-gray-500 mb-1">
                        Recommended actions
                      </h4>
                      <ul className="list-disc list-inside text-xs text-green-300 space-y-0.5">
                        {d.highlights.recommended_actions!.map((a, i) => (
                          <li key={i}>{a}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
        </>
      )}

      {tab === 'audit' && (
        <>
        <div className="flex items-center gap-2 mb-3">
          <span className="text-xs text-gray-500 uppercase tracking-wide">Actor</span>
          {(['all', 'user', 'service_account'] as const).map((v) => (
            <button
              key={v}
              onClick={() => {
                setPrincipalFilter(v)
                setPage(1)
              }}
              className={`px-2 py-1 rounded text-xs transition-colors ${
                principalFilter === v
                  ? 'bg-blue-500/20 text-blue-300 border border-blue-500/40'
                  : 'text-gray-400 border border-gray-800 hover:text-white'
              }`}
            >
              {v === 'all' ? 'All' : v === 'user' ? 'Human' : 'Agent'}
            </button>
          ))}
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
                      <div className="flex items-center gap-2">
                        <code className="text-xs font-mono text-blue-300">{log.action}</code>
                        {log.principal_type === 'service_account' && (
                          <span
                            className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wider bg-purple-500/15 text-purple-300 border border-purple-500/30"
                            title="Machine principal (service account)"
                          >
                            agent
                          </span>
                        )}
                      </div>
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
        </>
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
      {selectedUser && (
        <UserStatsModal user={selectedUser} onClose={() => setSelectedUser(null)} />
      )}
      {selectedRunbook && (
        <RunbookModal runbook={selectedRunbook} onClose={() => setSelectedRunbook(null)} />
      )}
    </Layout>
  )
}

function SeverityPill({ severity }: { severity?: string }) {
  const sev = (severity ?? 'unknown').toLowerCase()
  const colors: Record<string, string> = {
    critical: 'bg-red-900/40 text-red-300 border-red-800/40',
    high: 'bg-orange-500/20 text-orange-300 border-orange-500/30',
    medium: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
    low: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
    unknown: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
  }
  return (
    <span
      className={`inline-flex px-2 py-0.5 rounded-full text-[10px] uppercase tracking-wide font-medium border ${
        colors[sev] ?? colors.unknown
      }`}
    >
      {sev}
    </span>
  )
}

function SLOCard({ slo, onOpenRunbook }: { slo: SLOState; onOpenRunbook: () => void }) {
  const pct = (slo.current * 100).toFixed(slo.current >= 0.999 ? 3 : 2)
  const targetPct = (slo.target * 100).toFixed(1)
  const burn = slo.burn_rate
  const burnLabel =
    burn === null
      ? '∞'
      : burn < 0.01
        ? '0.0×'
        : `${burn.toFixed(burn < 10 ? 2 : 1)}×`
  const budgetPctLabel = `${slo.budget_remaining_pct.toFixed(0)}%`
  const isHealthy = slo.healthy
  const isHot = !isHealthy || (burn !== null && burn >= 14.4)

  return (
    <div
      className={`bg-gray-900 border rounded-lg p-4 ${
        isHot ? 'border-red-700/40' : isHealthy ? 'border-green-700/30' : 'border-gray-800'
      }`}
    >
      <div className="flex items-baseline justify-between mb-1">
        <h3 className="text-sm font-medium text-gray-200">{slo.name}</h3>
        <span
          className={`text-xs font-mono ${isHealthy ? 'text-green-400' : 'text-red-400'}`}
        >
          {isHealthy ? 'meeting SLO' : 'breaching'}
        </span>
      </div>
      <p className="text-xs text-gray-500 mb-3">{slo.description}</p>
      <div className="grid grid-cols-3 gap-3 mb-3">
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-500">Current</p>
          <p className="text-lg font-mono text-gray-200">{pct}%</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-500">Target</p>
          <p className="text-lg font-mono text-gray-400">{targetPct}%</p>
        </div>
        <div>
          <p className="text-[10px] uppercase tracking-wide text-gray-500">Burn rate</p>
          <p
            className={`text-lg font-mono ${burn === null || burn >= 14.4 ? 'text-red-400' : burn >= 1 ? 'text-yellow-300' : 'text-gray-300'}`}
          >
            {burnLabel}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2 mb-2">
        <p className="text-[10px] text-gray-500 w-20 shrink-0">Budget remaining</p>
        <div className="flex-1 h-1.5 bg-gray-800 rounded overflow-hidden">
          <div
            className={`h-full ${
              slo.budget_remaining_pct > 50
                ? 'bg-green-500'
                : slo.budget_remaining_pct > 0
                  ? 'bg-yellow-400'
                  : 'bg-red-500'
            }`}
            style={{ width: `${Math.max(0, Math.min(100, slo.budget_remaining_pct))}%` }}
          />
        </div>
        <span className="text-xs font-mono text-gray-400 w-12 text-right">
          {budgetPctLabel}
        </span>
      </div>
      <div className="flex items-center justify-between mt-3">
        <p className="text-[11px] text-gray-600 font-mono">
          {slo.total.toLocaleString()} jobs · {slo.failed.toLocaleString()} failed ·{' '}
          {slo.window_hours}h
        </p>
        <button
          onClick={onOpenRunbook}
          className="text-xs text-blue-400 hover:text-blue-300"
        >
          Open runbook →
        </button>
      </div>
    </div>
  )
}

function RunbookModal({ runbook, onClose }: { runbook: Runbook; onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm overflow-y-auto p-4"
      onClick={onClose}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl shadow-2xl w-full max-w-2xl my-8 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-800">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <code className="text-xs text-blue-300 font-mono">{runbook.id}</code>
              <SeverityPill severity={runbook.severity} />
            </div>
            <p className="text-sm text-gray-200 mt-0.5 truncate">{runbook.title}</p>
            {runbook.alarm && (
              <p className="text-xs text-gray-500 font-mono mt-0.5">
                Alarm: {runbook.alarm}
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-white text-lg leading-none ml-3 shrink-0"
          >
            ✕
          </button>
        </div>

        <div className="px-5 py-4 space-y-4 text-sm max-h-[70vh] overflow-y-auto">
          {runbook.summary && (
            <Section title="Summary">
              <p className="text-gray-300 whitespace-pre-wrap">{runbook.summary}</p>
            </Section>
          )}
          {runbook.symptoms && runbook.symptoms.length > 0 && (
            <Section title="Symptoms">
              <ul className="list-disc list-inside text-gray-300 space-y-1">
                {runbook.symptoms.map((s, i) => (
                  <li key={i}>{s}</li>
                ))}
              </ul>
            </Section>
          )}
          {runbook.diagnosis_steps && runbook.diagnosis_steps.length > 0 && (
            <Section title="Diagnosis">
              <ol className="space-y-3">
                {runbook.diagnosis_steps.map((step, i) => (
                  <li key={i} className="bg-gray-800/40 border border-gray-700/40 rounded p-3">
                    <p className="text-xs text-gray-300">
                      <span className="font-mono text-gray-500">{i + 1}.</span>{' '}
                      {step.description}
                    </p>
                    {step.command && (
                      <pre className="mt-2 text-[11px] font-mono text-blue-300 whitespace-pre-wrap break-all">
                        $ {step.command}
                      </pre>
                    )}
                  </li>
                ))}
              </ol>
            </Section>
          )}
          {runbook.mitigation && runbook.mitigation.length > 0 && (
            <Section title="Mitigation">
              <ul className="list-disc list-inside text-gray-300 space-y-1">
                {runbook.mitigation.map((m, i) => (
                  <li key={i}>{m}</li>
                ))}
              </ul>
            </Section>
          )}
          {runbook.escalation && runbook.escalation.length > 0 && (
            <Section title="Escalation">
              <ul className="list-disc list-inside text-gray-300 space-y-1">
                {runbook.escalation.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </Section>
          )}
          {runbook.related_dashboards && runbook.related_dashboards.length > 0 && (
            <Section title="Related dashboards">
              <ul className="text-gray-400 text-xs space-y-1 font-mono">
                {runbook.related_dashboards.map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
              </ul>
            </Section>
          )}
        </div>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-gray-500 mb-1.5 font-medium">
        {title}
      </p>
      {children}
    </div>
  )
}

const CARD_COLORS: Record<string, string> = {
  blue: 'border-blue-700/40 from-blue-900/30 text-blue-300',
  green: 'border-green-700/40 from-green-900/30 text-green-300',
  red: 'border-red-700/40 from-red-900/30 text-red-300',
  'red-dark': 'border-red-800/40 from-red-950/50 text-red-400',
}

function StatCard({
  label,
  value,
  loading,
  color,
}: {
  label: string
  value: number
  loading: boolean
  color: 'blue' | 'green' | 'red' | 'red-dark'
}) {
  return (
    <div
      className={`bg-gradient-to-br to-transparent border rounded-lg p-4 ${CARD_COLORS[color]}`}
    >
      <p className="text-[10px] uppercase tracking-wider opacity-70">{label}</p>
      <p className="text-2xl font-semibold font-mono mt-1">
        {loading ? '—' : value.toLocaleString()}
      </p>
    </div>
  )
}
