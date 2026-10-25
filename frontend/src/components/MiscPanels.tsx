import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmtDateTime } from '../format'
import type { DuplicateCandidate, EventInfo, SourcePolicyView, ToastMessage } from '../types'

export function ChangesPanel({
  taskId,
  refreshKey,
}: {
  taskId: string
  refreshKey: number
}) {
  const [items, setItems] = useState<EventInfo[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .changes(taskId)
      .then((response) => {
        if (!cancelled) setItems(response.items.slice().reverse())
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [taskId, refreshKey])

  if (loading && items.length === 0) {
    return (
      <section className="card">
        <div className="spinner" />
      </section>
    )
  }

  if (items.length === 0) {
    return (
      <section className="card empty-card">
        <div className="empty-icon">Δ</div>
        <h3>Змін ще не виявлено</h3>
        <p>
          Кожен новий збір або снапшот порівнюється з попередньою версією. Тут з'являться нові
          записи, зміни полів і падіння цін.
        </p>
      </section>
    )
  }

  return (
    <section className="card">
      <div className="card-head">
        <h3 className="card-title">Динаміка даних</h3>
        <span className="chip">{items.length} подій</span>
      </div>
      <ul className="change-list">
        {items.map((event) => (
          <li key={event.id} className={`change change--${event.type}`}>
            <span className={`change-icon change-icon--${event.type}`}>
              {event.type === 'new_entity'
                ? '+'
                : event.type === 'removed_entity'
                  ? '−'
                  : event.type === 'price_drop'
                    ? '↓'
                    : '≠'}
            </span>
            <div className="change-body">
              <div className="change-message">{event.message}</div>
              <div className="change-meta">
                <span>{event.type}</span>
                <span>{fmtDateTime(event.created_at)}</span>
                {typeof event.data?.delta_pct === 'number' && (
                  <span className="chip chip--warn">{event.data.delta_pct}%</span>
                )}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function DuplicatesPanel({
  taskId,
  refreshKey,
}: {
  taskId: string
  refreshKey: number
}) {
  const [items, setItems] = useState<DuplicateCandidate[]>([])
  const [scanned, setScanned] = useState(0)
  const [note, setNote] = useState('')
  const [threshold, setThreshold] = useState(88)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .duplicates(taskId, threshold)
      .then((response) => {
        if (cancelled) return
        setItems(response.items)
        setScanned(response.scanned)
        setNote(response.note)
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [taskId, threshold, refreshKey])

  return (
    <section className="card">
      <div className="card-head">
        <h3 className="card-title">Кандидати на дублікати</h3>
        <div className="card-actions">
          <select
            className="select select--auto"
            value={threshold}
            onChange={(event) => setThreshold(Number(event.target.value))}
          >
            <option value={80}>≥ 80%</option>
            <option value={88}>≥ 88%</option>
            <option value={92}>≥ 92%</option>
            <option value={96}>≥ 96%</option>
          </select>
          {loading && <span className="spinner" />}
        </div>
      </div>
      <p className="lede">
        Перевірено {scanned} записів. {note}
      </p>
      {items.length === 0 ? (
        <div className="empty-hint">Схожих записів не знайдено — добре.</div>
      ) : (
        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>Схожість</th>
                <th>Запис A</th>
                <th>Запис B</th>
                <th>Ключі</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.left_key}-${item.right_key}`}>
                  <td>
                    <span className={`conf ${item.score >= 0.95 ? 'conf--high' : 'conf--mid'}`}>
                      {Math.round(item.score * 100)}%
                    </span>
                  </td>
                  <td>{item.left_name}</td>
                  <td>{item.right_name}</td>
                  <td>
                    <code className="mono-small">
                      {item.left_key} · {item.right_key}
                    </code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function SourcesPanel({ onToast }: { onToast: (toast: ToastMessage) => void }) {
  const [items, setItems] = useState<SourcePolicyView[]>([])
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      const response = await api.sources()
      setItems(response.items)
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка' })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  return (
    <section className="card">
      <div className="card-head">
        <h3 className="card-title">Реєстр джерел</h3>
        <span className="chip">{items.length}</span>
      </div>
      <p className="lede">
        Політика кожного домену: ризик ToS, ліцензія, нотатки. Це не формальність — саме тут
        фіксується, що можна продавати, а що ні. Robots.txt перевіряється автоматично перед
        кожним запитом.
      </p>
      {loading && <div className="spinner" />}
      <div className="stack">
        {items.map((item) => (
          <SourceRow key={item.domain} item={item} onSaved={load} onToast={onToast} />
        ))}
        {!loading && items.length === 0 && (
          <div className="empty-hint">Джерела з'являться після створення задачі з URL.</div>
        )}
      </div>
    </section>
  )
}

function SourceRow({
  item,
  onSaved,
  onToast,
}: {
  item: SourcePolicyView
  onSaved: () => Promise<void>
  onToast: (toast: ToastMessage) => void
}) {
  const [risk, setRisk] = useState(item.tos_risk)
  const [license, setLicense] = useState(item.license)
  const [notes, setNotes] = useState(item.notes)
  const [saving, setSaving] = useState(false)

  const save = async (reviewed: boolean) => {
    setSaving(true)
    try {
      await api.updateSource(item.domain, {
        tos_risk: risk,
        license,
        notes,
        reviewed,
      })
      await onSaved()
      onToast({ kind: 'success', text: `Політику ${item.domain} збережено` })
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка' })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="source-row">
      <div className="source-head">
        <code className="source-domain">{item.domain}</code>
        {item.reviewed_at ? (
          <span className="chip chip--yes">перевірено {fmtDateTime(item.reviewed_at)}</span>
        ) : (
          <span className="chip chip--warn">не перевірено</span>
        )}
      </div>
      <div className="form-row">
        <label className="field field--small">
          <span className="field-label">Ризик ToS</span>
          <select className="select" value={risk} onChange={(event) => setRisk(event.target.value)}>
            <option value="unknown">unknown</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
            <option value="forbidden">forbidden</option>
          </select>
        </label>
        <label className="field">
          <span className="field-label">Ліцензія / умови</span>
          <input
            className="input"
            value={license}
            onChange={(event) => setLicense(event.target.value)}
            placeholder="public data / CC BY / unknown"
          />
        </label>
      </div>
      <label className="field">
        <span className="field-label">Нотатки</span>
        <input
          className="input"
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          placeholder="Що дозволено використовувати, що ні"
        />
      </label>
      <div className="form-actions">
        <button className="btn btn--tiny" onClick={() => save(false)} disabled={saving}>
          Зберегти
        </button>
        <button className="btn btn--tiny btn--primary" onClick={() => save(true)} disabled={saving}>
          Позначити перевіреним
        </button>
      </div>
    </div>
  )
}
