import { useEffect, useState } from 'react'
import { api } from '../api'
import { confidenceTone, fmtDateTime, fmtValue } from '../format'
import type { FieldSpec, RecordDetail } from '../types'

interface Props {
  taskId: string
  recordKey: string
  columns: FieldSpec[]
  onClose: () => void
}

export function DetailDrawer({ taskId, recordKey, columns, onClose }: Props) {
  const [detail, setDetail] = useState<RecordDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setError(null)
    api
      .record(taskId, recordKey)
      .then((response) => {
        if (!cancelled) setDetail(response)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Помилка')
      })
    return () => {
      cancelled = true
    }
  }, [taskId, recordKey])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const record = detail?.record
  const payload = record?.payload ?? {}
  const title = String(payload.name ?? payload.company ?? payload.title ?? recordKey)
  const orderedKeys = [
    ...columns.map((column) => column.key).filter((key) => key in payload),
    ...Object.keys(payload).filter((key) => !columns.some((column) => column.key === key)),
  ]

  return (
    <div className="drawer-overlay" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside className="drawer" role="dialog" aria-modal="true">
        <div className="drawer-head">
          <div>
            <div className="eyebrow">Походження запису</div>
            <h2>{title}</h2>
            <code className="mono-small muted">{recordKey}</code>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Закрити">
            ✕
          </button>
        </div>

        {error && <div className="alert alert--error">{error}</div>}
        {!detail && !error && (
          <div className="drawer-loading">
            <span className="spinner" /> завантажую історію…
          </div>
        )}

        {record && (
          <div className="drawer-body">
            <div className="drawer-meta">
              <span className={confidenceTone(record.confidence)}>
                впевненість {Math.round(record.confidence * 100)}%
              </span>
              <span className="muted">оновлено {fmtDateTime(record.observed_at)}</span>
              {record.source_url && (
                <a className="link" href={record.source_url} target="_blank" rel="noreferrer">
                  джерело ↗
                </a>
              )}
            </div>

            <h4 className="sub-title">Поля та походження</h4>
            <div className="field-provenance">
              {orderedKeys.map((key) => {
                const column = columns.find((item) => item.key === key)
                const provenance = record.provenance[key]
                return (
                  <div className="prov-row" key={key}>
                    <div className="prov-key">
                      <span className="prov-title">{column?.title ?? key}</span>
                      <code className="mono-small muted">{key}</code>
                    </div>
                    <div className="prov-value">{fmtValue(payload[key], column?.dtype)}</div>
                    <div className="prov-badges">
                      {provenance ? (
                        <>
                          <span className={`method method--${provenance.method}`}>
                            {provenance.method}
                          </span>
                          <span className={confidenceTone(provenance.confidence)}>
                            {Math.round(provenance.confidence * 100)}%
                          </span>
                          {!provenance.verified && <span className="chip chip--warn">unverified</span>}
                          {provenance.note && (
                            <span className="prov-note" title={provenance.note}>
                              {provenance.note}
                            </span>
                          )}
                        </>
                      ) : (
                        <span className="muted">без метаданих</span>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>

            <h4 className="sub-title">Історія версій ({detail.history.length})</h4>
            <div className="history">
              {detail.history.map((version) => (
                <details className="history-item" key={`${version.revision}-${version.dataset_version}`}>
                  <summary>
                    <span className="chip">v{version.dataset_version}</span>
                    <span className="muted">{fmtDateTime(version.observed_at)}</span>
                    <span className="muted">{Object.keys(version.payload).length} полів</span>
                  </summary>
                  <div className="history-fields">
                    {Object.entries(version.payload).map(([key, value]) => (
                      <div className="preview-row" key={key}>
                        <span className="preview-key">{key}</span>
                        <span className="preview-value">{fmtValue(value)}</span>
                      </div>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          </div>
        )}
      </aside>
    </div>
  )
}
