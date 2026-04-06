import { useState } from 'react'
import { jobsApi } from '../api/jobs'
import type { Job, JobType } from '../types'
import { AppError } from '../api/client'
import { JOB_TYPE_LABELS } from '../utils/format'

const JOB_TYPES: JobType[] = ['csv_upload', 'report_gen', 'bulk_api_sync', 'doc_analysis']

const DEFAULT_PAYLOADS: Record<JobType, Record<string, unknown>> = {
  csv_upload: { row_count: 500, chunk_size: 100 },
  report_gen: { row_count: 10000, group_count: 10, format: 'pdf' },
  bulk_api_sync: { endpoint_count: 5 },
  doc_analysis: { page_count: 10 },
}

interface Props {
  onCreated: (job: Job) => void
}

export default function JobForm({ onCreated }: Props) {
  const [type, setType] = useState<JobType>('csv_upload')
  const [priority, setPriority] = useState(0)
  const [idempotencyKey, setIdempotencyKey] = useState('')
  const [payloadText, setPayloadText] = useState(
    JSON.stringify(DEFAULT_PAYLOADS.csv_upload, null, 2),
  )
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  function handleTypeChange(t: JobType) {
    setType(t)
    setPayloadText(JSON.stringify(DEFAULT_PAYLOADS[t], null, 2))
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)

    let payload: Record<string, unknown> | undefined
    try {
      payload = JSON.parse(payloadText)
    } catch {
      setError('Payload must be valid JSON')
      return
    }

    setSubmitting(true)
    try {
      const job = await jobsApi.create({
        type,
        payload,
        priority,
        idempotency_key: idempotencyKey || undefined,
      })
      onCreated(job)
      setIdempotencyKey('')
    } catch (err) {
      setError(err instanceof AppError ? err.message : 'Failed to create job')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      {/* Job type */}
      <div>
        <label className="block text-xs text-gray-400 mb-1.5">Job type</label>
        <div className="grid grid-cols-2 gap-2">
          {JOB_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => handleTypeChange(t)}
              className={`px-3 py-2 rounded text-sm text-left transition-colors border ${
                type === t
                  ? 'bg-blue-600/20 border-blue-500/50 text-blue-300'
                  : 'bg-gray-800/60 border-gray-700/50 text-gray-400 hover:text-gray-200'
              }`}
            >
              {JOB_TYPE_LABELS[t]}
            </button>
          ))}
        </div>
      </div>

      {/* Payload */}
      <div>
        <label className="block text-xs text-gray-400 mb-1.5">Payload (JSON)</label>
        <textarea
          value={payloadText}
          onChange={(e) => setPayloadText(e.target.value)}
          rows={5}
          className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2 text-sm font-mono text-gray-200 focus:outline-none focus:border-blue-500/50 resize-none"
        />
      </div>

      {/* Priority + idempotency key */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-gray-400 mb-1.5">Priority (0–100)</label>
          <input
            type="number"
            min={0}
            max={100}
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
            className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500/50"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-400 mb-1.5">Idempotency key</label>
          <input
            type="text"
            placeholder="optional"
            value={idempotencyKey}
            onChange={(e) => setIdempotencyKey(e.target.value)}
            className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2 text-sm font-mono text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/50"
          />
        </div>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      <button
        type="submit"
        disabled={submitting}
        className="w-full py-2 rounded bg-blue-600 hover:bg-blue-500 disabled:bg-blue-800 disabled:cursor-not-allowed text-white text-sm font-medium transition-colors"
      >
        {submitting ? 'Submitting…' : 'Submit job'}
      </button>
    </form>
  )
}
