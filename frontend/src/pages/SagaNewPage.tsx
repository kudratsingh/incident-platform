import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Layout from '../components/Layout'
import { useToast } from '../components/Toast'
import { sagasApi } from '../api/sagas'
import { AppError } from '../api/client'
import { JOB_TYPE_LABELS } from '../utils/format'
import type { JobType, SagaStepRequest } from '../types'

const JOB_TYPES: JobType[] = ['csv_upload', 'bulk_api_sync', 'doc_analysis', 'report_gen']

/**
 * A step as the form holds it. The payload stays RAW TEXT while the user
 * types — parsing on every keystroke would reject every intermediate state of
 * hand-typed JSON and snap the controlled textarea back to the last valid
 * value. It is parsed once, at submit, into the `SagaStepRequest` wire shape.
 */
interface StepDraft {
  type: JobType
  payloadText: string
}

const EMPTY_STEP: StepDraft = { type: 'csv_upload', payloadText: '{}' }

export default function SagaNewPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const [name, setName] = useState('')
  const [steps, setSteps] = useState<StepDraft[]>([EMPTY_STEP])
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  function addStep() {
    setSteps((s) => [...s, EMPTY_STEP])
  }

  function removeStep(i: number) {
    setSteps((s) => s.filter((_, idx) => idx !== i))
  }

  function updateStep(i: number, patch: Partial<StepDraft>) {
    setSteps((s) => s.map((st, idx) => (idx === i ? { ...st, ...patch } : st)))
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    if (!name.trim() || steps.length === 0) return

    const payloadSteps: SagaStepRequest[] = []
    for (const [i, step] of steps.entries()) {
      try {
        payloadSteps.push({ type: step.type, payload: JSON.parse(step.payloadText) })
      } catch {
        setError(`Step ${i + 1} payload must be valid JSON`)
        return
      }
    }

    setSubmitting(true)
    try {
      const saga = await sagasApi.create({ name: name.trim(), steps: payloadSteps })
      toast.success(`Saga "${saga.name}" created`)
      navigate(`/sagas/${saga.id}`)
    } catch (err) {
      toast.error(err instanceof AppError ? err.message : 'Failed to create saga')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Layout>
      <div className="max-w-2xl">
        <div className="mb-6">
          <h1 className="text-lg font-semibold text-white">New saga</h1>
          <p className="text-sm text-gray-500">
            Define a chain of jobs. Each step depends on the previous —
            the next step only runs once the prior one completes.
          </p>
        </div>

        <form onSubmit={submit} className="space-y-6">
          <div>
            <label className="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">
              Name
            </label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. quarterly-import-pipeline"
              className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/50"
              required
            />
          </div>

          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-medium text-gray-500 uppercase tracking-wide">
                Steps
              </label>
              <button
                type="button"
                onClick={addStep}
                className="text-xs text-blue-400 hover:text-blue-300"
              >
                + Add step
              </button>
            </div>
            <div className="space-y-2">
              {steps.map((step, i) => (
                <div
                  key={i}
                  className="flex items-start gap-2 bg-gray-900 border border-gray-800 rounded p-3"
                >
                  <span className="text-xs font-mono text-gray-600 mt-2 w-6">
                    {i + 1}.
                  </span>
                  <div className="flex-1 space-y-2">
                    <select
                      value={step.type}
                      onChange={(e) =>
                        updateStep(i, { type: e.target.value as JobType })
                      }
                      className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500/50"
                    >
                      {JOB_TYPES.map((t) => (
                        <option key={t} value={t}>
                          {JOB_TYPE_LABELS[t] ?? t}
                        </option>
                      ))}
                    </select>
                    <textarea
                      value={step.payloadText}
                      onChange={(e) => updateStep(i, { payloadText: e.target.value })}
                      rows={3}
                      placeholder='{"file": "data.csv"}'
                      className="w-full bg-gray-800/60 border border-gray-700/50 rounded px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-500/50 font-mono"
                    />
                  </div>
                  {steps.length > 1 && (
                    <button
                      type="button"
                      onClick={() => removeStep(i)}
                      className="text-xs text-gray-500 hover:text-red-400 mt-1.5"
                      title="Remove step"
                    >
                      ✕
                    </button>
                  )}
                </div>
              ))}
            </div>
          </div>

          {error && <p className="text-sm text-red-400">{error}</p>}

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 rounded text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50 transition-colors"
            >
              {submitting ? 'Creating…' : 'Create saga'}
            </button>
            <button
              type="button"
              onClick={() => navigate('/sagas')}
              className="px-4 py-2 rounded text-sm font-medium text-gray-400 hover:text-white"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </Layout>
  )
}
