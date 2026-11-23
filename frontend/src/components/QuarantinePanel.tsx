import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmtDateTime, fmtNumber, fmtValue } from '../format'
import type { QuarantineItem, ToastMessage } from '../types'

interface Props {
  taskId: string
  refreshKey: number
  onRefresh: () => Promise<void>
  onToast: (toast: ToastMessage) => void
}

const PAGE_SIZE = 50

export function QuarantinePanel({ taskId, refreshKey, onRefresh, onToast }: Props) {
  const [items, setItems] = useState<QuarantineItem[]>([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const load = async (nextOffset = offset) => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.quarantine(taskId, PAGE_SIZE, nextOffset)
      setItems(response.items)
      setTotal(response.total)
      if (response.items.length === 0 && nextOffset > 0 && response.total > 0) {
        const prev = Math.max(0, nextOffset - PAGE_SIZE)
        setOffset(prev)
        const retry = await api.quarantine(taskId, PAGE_SIZE, prev)
        setItems(retry.items)
        setTotal(retry.total)
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Не вдалося завантажити карантин'
      setError(message)
      onToast({ kind: 'error', text: message })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setOffset(0)
  }, [taskId])

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      setLoading(true)
      setError(null)
      try {
        const response = await api.quarantine(taskId, PAGE_SIZE, offset)
        if (!cancelled) {
          setItems(response.items)
          setTotal(response.total)
        }
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : 'Не вдалося завантажити карантин'
          setError(message)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [taskId, offset, refreshKey])

  const approve = async (item: QuarantineItem) => {
    setBusyId(item.id)
    try {
      const result = await api.approveQuarantine(taskId, item.id)
      onToast({
        kind: 'success',
        text: `Запис перенесено в датасет (рядків: ${fmtNumber(result.row_count)})`,
      })
      await Promise.all([load(offset), onRefresh()])
    } catch (err) {
      onToast({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Не вдалося підтвердити запис',
      })
    } finally {
      setBusyId(null)
    }
  }

  const dismiss = async (item: QuarantineItem) => {
    setBusyId(item.id)
    try {
      await api.dismissQuarantine(taskId, item.id)
      onToast({ kind: 'info', text: 'Запис відхилено з карантину' })
      await Promise.all([load(offset), onRefresh()])
    } catch (err) {
      onToast({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Не вдалося відхилити запис',
      })
    } finally {
      setBusyId(null)
    }
  }

  if (loading && items.length === 0) {
    return (
      <section className="card">
        <div className="drawer-loading">
          <span className="spinner" /> Завантаження карантину…
        </div>
      </section>
    )
  }

  if (error && items.length === 0) {
    return (
      <section className="card stack">
        <div className="alert alert--error">{error}</div>
        <div>
          <button className="btn btn--tiny" onClick={() => void load(offset)}>
            Спробувати ще раз
          </button>
        </div>
      </section>
    )
  }

  if (total === 0 && items.length === 0) {
    return (
      <section className="card empty-card">
        <div className="empty-icon">✓</div>
        <h3>Карантин порожній</h3>
        <p>
          Підозрілі або частково пошкоджені записи (невалідні типи полів, брак ключових полів чи
          низька впевненість екстракції) автоматично потрапляють сюди замість того, щоб
          засмічувати основний датасет.
        </p>
      </section>
    )
  }

  return (
    <section className="card">
      <div className="card-head">
        <h3 className="card-title">Карантин підозрілих записів</h3>
        <div className="card-actions">
          <span className="chip chip--warn">{fmtNumber(total)} записів</span>
          {loading && <span className="spinner" />}
        </div>
      </div>
      <p className="lede">
        Перевірте причину відбраковування. «Підтвердити в датасет» перенесе запис до основних даних
        як ручну правку, «Відхилити» — видалить назавжди.
      </p>

      {error && <div className="alert alert--error">{error}</div>}

      <div className="quarantine-list">
        {items.map((item) => {
          const isBusy = busyId === item.id
          const entries = Object.entries(item.payload ?? {})
          return (
            <article key={item.id} className="quarantine-card">
              <div className="quarantine-head">
                <span className="chip chip--warn">{item.reason}</span>
                <span className="mono-small">{fmtDateTime(item.created_at)}</span>
              </div>

              {item.source_url && (
                <div className="quarantine-source">
                  {item.source_url.startsWith('http') ? (
                    <a className="link mono-small" href={item.source_url} target="_blank" rel="noreferrer">
                      {item.source_url}
                    </a>
                  ) : (
                    <code className="mono-small">{item.source_url}</code>
                  )}
                </div>
              )}

              {entries.length > 0 && (
                <div className="quarantine-payload">
                  {entries.map(([key, value]) => (
                    <div className="preview-row" key={key}>
                      <span className="preview-key">{key}</span>
                      <span className="preview-value">{fmtValue(value)}</span>
                    </div>
                  ))}
                </div>
              )}

              <details className="quarantine-json">
                <summary className="mono-small">Сирий JSON ({item.id})</summary>
                <pre className="mono-small">{JSON.stringify(item.payload, null, 2)}</pre>
              </details>

              <div className="quarantine-actions">
                <button
                  className="btn btn--tiny btn--primary"
                  onClick={() => void approve(item)}
                  disabled={busyId !== null}
                >
                  {isBusy ? 'Обробка…' : 'Підтвердити в датасет'}
                </button>
                <button
                  className="btn btn--tiny btn--danger"
                  onClick={() => void dismiss(item)}
                  disabled={busyId !== null}
                >
                  Відхилити
                </button>
              </div>
            </article>
          )
        })}
      </div>

      {total > PAGE_SIZE && (
        <div className="pager">
          <button
            className="btn btn--tiny"
            disabled={offset === 0 || loading}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← Попередні
          </button>
          <span className="muted">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} з {fmtNumber(total)}
          </span>
          <button
            className="btn btn--tiny"
            disabled={offset + PAGE_SIZE >= total || loading}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Наступні →
          </button>
        </div>
      )}
    </section>
  )
}
