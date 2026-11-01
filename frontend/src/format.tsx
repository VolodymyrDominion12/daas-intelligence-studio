import type { ReactNode } from 'react'
import type { DType, EventInfo, RunInfo, RunStatus } from './types'

export function fmtValue(value: unknown, dtype?: DType): ReactNode {
  if (value === null || value === undefined || value === '') return <span className="muted">—</span>
  if (typeof value === 'boolean') {
    return <span className={`chip ${value ? 'chip--yes' : 'chip--no'}`}>{value ? 'так' : 'ні'}</span>
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">—</span>
    return (
      <span className="chip-row">
        {value.slice(0, 4).map((item, index) => (
          <span className="chip" key={`${String(item)}-${index}`}>
            {String(item)}
          </span>
        ))}
        {value.length > 4 && <span className="chip chip--more">+{value.length - 4}</span>}
      </span>
    )
  }
  if (typeof value === 'object') {
    return <code className="mono-small">{JSON.stringify(value)}</code>
  }
  if (dtype === 'url' && typeof value === 'string' && value.startsWith('http')) {
    return (
      <a className="link" href={value} target="_blank" rel="noreferrer" onClick={(event) => event.stopPropagation()}>
        {value.replace(/^https?:\/\/(www\.)?/, '').slice(0, 42)}
      </a>
    )
  }
  if (dtype === 'number' && typeof value === 'number') {
    return <span className="num">{value.toLocaleString('uk-UA', { maximumFractionDigits: 2 })}</span>
  }
  const text = String(value)
  return (
    <span className="cell-text" title={text}>
      {text}
    </span>
  )
}

export function fmtNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '0'
  return value.toLocaleString('uk-UA')
}

export function fmtBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('uk-UA', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso).getTime()
  if (Number.isNaN(date)) return iso
  const seconds = Math.round((Date.now() - date) / 1000)
  if (seconds < 60) return 'щойно'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} хв тому`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} год тому`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days} дн тому`
  return new Date(iso).toLocaleDateString('uk-UA')
}

export const RUN_LABELS: Record<string, string> = {
  calibrate: 'Калібрування',
  collect: 'Збір даних',
  enrich: 'Збагачення',
  snapshot: 'Снапшот',
  prepare: 'Підготовка',
}

export function statusClass(status: RunStatus | string): string {
  switch (status) {
    case 'running':
      return 'status status--running'
    case 'queued':
      return 'status status--queued'
    case 'succeeded':
    case 'done':
    case 'ready':
      return 'status status--ok'
    case 'partial':
      return 'status status--partial'
    case 'failed':
      return 'status status--error'
    case 'cancelled':
      return 'status status--muted'
    default:
      return 'status status--muted'
  }
}

export const STATUS_LABELS: Record<string, string> = {
  draft: 'чернетка',
  ready: 'готова',
  running: 'виконується',
  done: 'готово',
  failed: 'помилка',
  queued: 'у черзі',
  succeeded: 'успішно',
  partial: 'частково',
  cancelled: 'скасовано',
}

export function runSummary(run: RunInfo | null): string {
  if (!run) return 'Запусків ще не було'
  const stats = run.stats
  const parts = [
    `${RUN_LABELS[run.kind] ?? run.kind}: ${STATUS_LABELS[run.status] ?? run.status}`,
    `${fmtNumber(stats.pages_fetched)} стор.`,
    `+${fmtNumber(stats.records_new)} / ↻${fmtNumber(stats.records_updated)}`,
  ]
  if (run.spent_usd > 0) parts.push(`$${run.spent_usd.toFixed(3)}`)
  return parts.join(' · ')
}

export const LEVEL_LABELS: Record<string, string> = {
  debug: 'debug',
  info: 'info',
  warning: 'warn',
  error: 'error',
}

export function eventTone(event: EventInfo): string {
  if (event.level === 'error') return 'log-row log-row--error'
  if (event.level === 'warning') return 'log-row log-row--warning'
  if (event.type.includes('finished') || event.type === 'page_extracted') return 'log-row log-row--ok'
  return 'log-row'
}

export function piiLabel(pii: string): string | null {
  if (pii === 'personal') return 'PII'
  if (pii === 'business_contact') return 'PII·biz'
  return null
}

export function confidenceTone(confidence: number): string {
  if (confidence >= 0.85) return 'conf conf--high'
  if (confidence >= 0.6) return 'conf conf--mid'
  return 'conf conf--low'
}
