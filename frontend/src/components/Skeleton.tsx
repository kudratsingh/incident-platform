/**
 * Skeleton loading placeholders.
 * Used instead of "Loading…" text for a more polished feel.
 */

function Bone({ className = '' }: { className?: string }) {
  return (
    <div className={`bg-gray-800 rounded animate-pulse ${className}`} />
  )
}

/** A skeleton row that mimics the job table row shape. */
export function TableRowSkeleton() {
  return (
    <tr>
      <td className="px-4 py-3">
        <Bone className="h-4 w-32 mb-1.5" />
        <Bone className="h-3 w-20" />
      </td>
      <td className="px-4 py-3"><Bone className="h-5 w-20 rounded-full" /></td>
      <td className="px-4 py-3 hidden md:table-cell"><Bone className="h-3 w-28" /></td>
      <td className="px-4 py-3 hidden lg:table-cell"><Bone className="h-3 w-36" /></td>
      <td className="px-4 py-3"><Bone className="h-3 w-8" /></td>
    </tr>
  )
}

/** Skeleton for the job detail page right-hand metadata panel. */
export function DetailSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="flex justify-between gap-4">
          <Bone className="h-3 w-16" />
          <Bone className="h-3 w-24" />
        </div>
      ))}
    </div>
  )
}
