import { useEffect, useState } from 'react'
import { api, withToken } from '../api'
import { fmtBytes, fmtNumber, timeAgo } from '../format'
import type { ExportItem, ExportResult, ToastMessage } from '../types'

interface Props {
  taskId: string
  rows: number
  onToast: (toast: ToastMessage) => void
}

const FORMATS = [
  { key: 'xlsx', label: 'Excel', hint: 'з форматуванням і маніфестом' },
  { key: 'csv', label: 'CSV', hint: 'UTF-8 BOM для Excel' },
  { key: 'parquet', label: 'Parquet', hint: 'для аналітики' },
  { key: 'json', label: 'JSON', hint: 'для API' },
]

export function ExportPanel({ taskId, rows, onToast }: Props) {
  const [format, setFormat] = useState('xlsx')
  const [mask, setMask] = useState(true)
  const [limit, setLimit] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ExportResult | null>(null)
  const [history, setHistory] = useState<ExportItem[]>([])

  const loadHistory = async () => {
    try {
      const response = await api.exports()
      setHistory(response.items)
    } catch {
      /* history is non-critical */
    }
  }

  useEffect(() => {
    void loadHistory()
  }, [])

  const create = async () => {
    setBusy(true)
    try {
      const parsedLimit = limit.trim() ? Math.max(1, Number(limit)) : null
      const exported = await api.export(taskId, {
        format,
        mask_pii: mask,
        limit: parsedLimit,
      })
      setResult(exported)
      onToast({ kind: 'success', text: `Експорт готовий: ${exported.file}` })
      await loadHistory()
    } catch (error) {
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Не вдалося створити експорт',
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Експорт датасету</h3>
          <span className="chip">{fmtNumber(rows)} рядків</span>
        </div>
        <p className="lede">
          Файл супроводжується маніфестом: схема, джерела, ліцензія, заповненість полів і час
          генерації. Це те, що перетворює таблицю на дата-продукт.
        </p>

        <div className="format-grid">
          {FORMATS.map((item) => (
            <button
              key={item.key}
              className={`format-card ${format === item.key ? 'format-card--active' : ''}`}
              onClick={() => setFormat(item.key)}
            >
              <span className="format-label">{item.label}</span>
              <span className="format-hint">{item.hint}</span>
            </button>
          ))}
        </div>

        <div className="form-row form-row--baseline">
          <label className="checkbox">
            <input type="checkbox" checked={mask} onChange={(event) => setMask(event.target.checked)} />
            <span>Маскувати PII (email, телефони)</span>
          </label>
          <label className="field field--small">
            <span className="field-label">Обмежити рядків</span>
            <input
              className="input"
              type="number"
              min={1}
              placeholder="всі"
              value={limit}
              onChange={(event) => setLimit(event.target.value)}
            />
          </label>
        </div>

        <div className="form-actions">
          <button className="btn btn--primary" onClick={create} disabled={busy || rows === 0}>
            {busy ? 'Генерую…' : 'Створити експорт'}
          </button>
        </div>

        {rows === 0 && <div className="alert alert--warning">Датасет порожній — спершу запустіть збір.</div>}

        {result && (
          <div className="export-result">
            <div className="export-result-main">
              <span className="export-file">{result.file}</span>
              <span className="muted">
                {fmtNumber(result.rows)} рядків · {fmtBytes(result.bytes)} ·{' '}
                {result.masked_pii ? 'PII замасковано' : 'PII відкрито'}
              </span>
            </div>
            <a className="btn btn--primary" href={withToken(result.download_url)} download>
              Завантажити
            </a>
            <details className="manifest">
              <summary>Маніфест і ліцензія</summary>
              <pre className="mono-small">{JSON.stringify(result.manifest, null, 2)}</pre>
            </details>
          </div>
        )}
      </section>

      <section className="card">
        <h3 className="card-title">Останні файли</h3>
        {history.length === 0 && <div className="empty-hint">Експортів ще не було.</div>}
        <ul className="file-list">
          {history.slice(0, 12).map((item) => (
            <li key={item.file}>
              <a className="link" href={withToken(item.download_url)} download>
                {item.file}
              </a>
              <span className="muted">
                {fmtBytes(item.bytes)} · {timeAgo(new Date(item.modified * 1000).toISOString())}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
