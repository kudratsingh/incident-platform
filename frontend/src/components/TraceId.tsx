import { useToast } from './Toast'

interface Props {
  label: string
  value: string | null | undefined
}

/**
 * Copy `text` to the clipboard, returning whether it worked.
 *
 * `navigator.clipboard` is only defined in a secure context. The ALB serves
 * plain HTTP (HTTPS/ACM is the unstarted Phase 8), so on a deployed stack the
 * property is `undefined` and the unguarded call threw a TypeError that no
 * one caught — the user got no copy and no message. Even where it is defined
 * it returns a promise that rejects when the permission is denied, which was
 * an unhandled rejection for the same reason.
 *
 * The fallback is the pre-Clipboard-API `execCommand('copy')` path over an
 * offscreen textarea, which works in a non-secure context. It is deprecated
 * but still implemented everywhere, and a deprecated copy beats no copy.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission denied or a non-secure context that still exposes the
      // object. Fall through to the textarea path rather than giving up.
    }
  }

  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    // Keep it out of the viewport so the copy never scrolls the page or
    // flashes a visible control.
    textarea.setAttribute('readonly', '')
    textarea.style.position = 'fixed'
    textarea.style.top = '-9999px'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}

/**
 * Displays a correlation ID (trace_id, request_id) with a copy button.
 * The reason this exists as a component: being able to grab a trace ID and
 * search logs is the core of the "debugging realism" goal in Phase 3.
 */
export default function TraceId({ label, value }: Props) {
  const toast = useToast()

  if (!value) return null

  async function copy() {
    const ok = await copyToClipboard(value!)
    if (ok) {
      toast.info(`Copied ${label}`)
    } else {
      // Never silently claim success: the whole point of the component is
      // that the operator walks away holding the ID.
      toast.error(`Could not copy ${label} — select the value and copy it manually`)
    }
  }

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-500 shrink-0">{label}</span>
      <code className="text-xs font-mono text-gray-400 bg-gray-800/60 px-2 py-0.5 rounded border border-gray-700/50 truncate max-w-xs">
        {value}
      </code>
      <button
        onClick={copy}
        className="text-xs text-gray-500 hover:text-gray-300 transition-colors shrink-0"
      >
        copy
      </button>
    </div>
  )
}
