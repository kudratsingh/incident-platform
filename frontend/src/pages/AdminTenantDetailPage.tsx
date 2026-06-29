import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import { TableRowSkeleton } from '../components/Skeleton'
import { useAuth } from '../hooks/useAuth'
import { adminApi } from '../api/admin'
import { formatDate, JOB_TYPE_LABELS } from '../utils/format'
import type { Job, SystemStats, Tenant, User } from '../types'

/**
 * Platform-admin view: drill into a single tenant.
 *
 * Pulls everything via the existing admin endpoints with `?tenant_id=`
 * so the backend can resolve the cross-tenant scope through its
 * resolve_admin_tenant helper (which also resets the RLS session var).
 */
export default function AdminTenantDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { user } = useAuth()
  const [tenant, setTenant] = useState<Tenant | null>(null)
  const [stats, setStats] = useState<SystemStats | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!id) return
    let cancelled = false
    setLoading(true)
    setErr(null)
    Promise.all([
      adminApi.getTenant(id),
      adminApi.systemStats(id),
      adminApi.listJobs({ page: 1, page_size: 20, tenant_id: id }),
      adminApi.listUsers(1, id),
    ])
      .then(([t, s, jobsRes, usersRes]) => {
        if (cancelled) return
        setTenant(t)
        setStats(s)
        setJobs(jobsRes.items)
        setUsers(usersRes.items)
      })
      .catch((e: Error) => !cancelled && setErr(e.message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [id])

  if (!user?.is_platform_admin) {
    return (
      <Layout>
        <div className="text-sm text-gray-400">Platform admin role required.</div>
      </Layout>
    )
  }

  return (
    <Layout>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <Link
            to="/admin"
            className="text-xs text-gray-500 hover:text-gray-300 font-mono"
          >
            ← admin / tenants
          </Link>
          <h1 className="mt-1 text-xl font-semibold text-white">
            {tenant?.name ?? '…'}
          </h1>
          <code className="text-xs font-mono text-blue-300">{tenant?.slug}</code>
        </div>
        {tenant && (
          <div className="text-xs text-gray-500 font-mono">
            Created {formatDate(tenant.created_at)}
          </div>
        )}
      </div>

      {err && <div className="text-red-400 text-sm mb-4">{err}</div>}

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        {stats &&
          Object.entries(stats.by_status).map(([status, count]) => (
            <div
              key={status}
              className="bg-gray-900 border border-gray-800 rounded-lg p-3"
            >
              <div className="text-xs text-gray-500 uppercase tracking-wide">
                {status}
              </div>
              <div className="text-xl font-mono text-white mt-1">{count}</div>
            </div>
          ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Recent jobs */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <header className="px-4 py-3 border-b border-gray-800 text-xs text-gray-400 uppercase tracking-wide font-medium">
            Recent jobs
          </header>
          {loading ? (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-gray-800/60">
                {Array.from({ length: 5 }).map((_, i) => (
                  <TableRowSkeleton key={i} />
                ))}
              </tbody>
            </table>
          ) : jobs.length === 0 ? (
            <div className="px-4 py-6 text-sm text-gray-500">
              No jobs in this tenant yet.
            </div>
          ) : (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-gray-800/60">
                {jobs.map((j) => (
                  <tr key={j.id} className="hover:bg-gray-800/30">
                    <td className="px-4 py-2.5">
                      <Link
                        to={`/jobs/${j.id}`}
                        className="text-xs font-mono text-blue-300 hover:underline"
                      >
                        {j.id.slice(0, 8)}…
                      </Link>
                    </td>
                    <td className="px-4 py-2.5 text-gray-300">
                      {JOB_TYPE_LABELS[j.type] ?? j.type}
                    </td>
                    <td className="px-4 py-2.5">
                      <StatusBadge status={j.status} />
                    </td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">
                      {formatDate(j.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {/* Users */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <header className="px-4 py-3 border-b border-gray-800 text-xs text-gray-400 uppercase tracking-wide font-medium">
            Users
          </header>
          {loading ? (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-gray-800/60">
                {Array.from({ length: 5 }).map((_, i) => (
                  <TableRowSkeleton key={i} />
                ))}
              </tbody>
            </table>
          ) : users.length === 0 ? (
            <div className="px-4 py-6 text-sm text-gray-500">No users.</div>
          ) : (
            <table className="w-full text-sm">
              <tbody className="divide-y divide-gray-800/60">
                {users.map((u) => (
                  <tr key={u.id} className="hover:bg-gray-800/30">
                    <td className="px-4 py-2.5 text-gray-200">{u.email}</td>
                    <td className="px-4 py-2.5 text-xs text-gray-500">{u.role}</td>
                    <td className="px-4 py-2.5 text-xs text-gray-600">
                      {formatDate(u.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </Layout>
  )
}
