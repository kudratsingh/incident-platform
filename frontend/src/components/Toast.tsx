/**
 * Lightweight toast notification system.
 *
 * Usage:
 *   const toast = useToast()
 *   toast.success('Job queued for replay')
 *   toast.error('Replay failed: not found')
 *   toast.info('Copied to clipboard')
 */

import { createContext, useCallback, useContext, useState } from 'react'
import type { ReactNode } from 'react'

type ToastKind = 'success' | 'error' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  message: string
}

interface ToastApi {
  success: (message: string) => void
  error: (message: string) => void
  info: (message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

let _next = 0

const KIND_STYLES: Record<ToastKind, string> = {
  success: 'border-green-700/50 bg-green-900/30 text-green-300',
  error: 'border-red-700/50 bg-red-900/30 text-red-300',
  info: 'border-blue-700/50 bg-blue-900/30 text-blue-300',
}

const KIND_ICON: Record<ToastKind, string> = {
  success: '✓',
  error: '✕',
  info: 'ℹ',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const dismiss = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const add = useCallback((kind: ToastKind, message: string) => {
    const id = ++_next
    setToasts((t) => [...t, { id, kind, message }])
    setTimeout(() => dismiss(id), 4000)
  }, [dismiss])

  const api: ToastApi = {
    success: (m) => add('success', m),
    error: (m) => add('error', m),
    info: (m) => add('info', m),
  }

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* Toast container — fixed bottom-right, stacks upward */}
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 items-end pointer-events-none">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto flex items-start gap-2.5 px-4 py-3 rounded-lg border text-sm max-w-sm shadow-xl ${KIND_STYLES[t.kind]}`}
          >
            <span className="font-mono font-bold shrink-0 mt-px">{KIND_ICON[t.kind]}</span>
            <span className="leading-snug">{t.message}</span>
            <button
              onClick={() => dismiss(t.id)}
              className="ml-2 shrink-0 opacity-50 hover:opacity-100 transition-opacity"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside ToastProvider')
  return ctx
}
