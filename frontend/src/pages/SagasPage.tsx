import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Layout from '../components/Layout'
import StatusBadge from '../components/StatusBadge'
import { TableRowSkeleton } from '../components/Skeleton'
import { sagasApi, type SagaListItem } from '../api/sagas'
import { formatDate } from '../utils/format'

export default function SagasPage() {
  const [items, setItems] = useState<SagaListItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await sagasApi.list(page, 20)
      setItems(res.items)
      setTotal(res.total)
    } finally {
      setLoading(false)
    }
  }, [page])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-lg font-semibold text-white">Sagas</h1>
          <p className="text-sm text-gray-500">
            Multi-step workflows coordinated through Kafka. {total} total.
          </p>
        </div>
        <Link
          to="/sagas/new"
          className="px-4 py-1.5 rounded text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white transition-colors"
        >
          + New saga
        </Link>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        {loading ? (
          <table className="w-full text-sm">
            <tbody className="divide-y divide-gray-800/60">
              {Array.from({ length: 6 }).map((_, i) => (
                <TableRowSkeleton key={i} />
              ))}
            </tbody>
          </table>
        ) : items.length === 0 ? (
          <div className="px-6 py-12 text-center text-sm text-gray-500">
            No sagas yet — start one to see it appear here.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-800 text-xs text-gray-500">
                <th className="text-left px-4 py-3 font-medium">Name</th>
                <th className="text-left px-4 py-3 font-medium">Status</th>
                <th className="text-left px-4 py-3 font-medium hidden md:table-cell">
                  Steps
                </th>
                <th className="text-left px-4 py-3 font-medium hidden lg:table-cell">
                  Created
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800/60">
              {items.map((s) => (
                <tr
                  key={s.id}
                  className="hover:bg-gray-800/30 transition-colors"
                >
                  <td className="px-4 py-3">
                    <Link
                      to={`/sagas/${s.id}`}
                      className="text-gray-200 hover:text-white"
                    >
                      {s.name}
                    </Link>
                    <code className="text-xs text-gray-600 block font-mono">
                      {s.id.slice(0, 8)}…
                    </code>
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={s.status} />
                  </td>
                  <td className="px-4 py-3 hidden md:table-cell text-gray-400 text-xs font-mono">
                    {s.step_count}
                  </td>
                  <td className="px-4 py-3 hidden lg:table-cell text-gray-500 text-xs">
                    {formatDate(s.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {total > 20 && (
        <div className="flex justify-center gap-2 mt-4">
          <button
            disabled={page === 1}
            onClick={() => setPage((p) => p - 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30"
          >
            ← Prev
          </button>
          <span className="px-3 py-1 text-sm text-gray-500">Page {page}</span>
          <button
            disabled={page * 20 >= total}
            onClick={() => setPage((p) => p + 1)}
            className="px-3 py-1 rounded text-sm text-gray-400 hover:text-white disabled:opacity-30"
          >
            Next →
          </button>
        </div>
      )}
    </Layout>
  )
}
