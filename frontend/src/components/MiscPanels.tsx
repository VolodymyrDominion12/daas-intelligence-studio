import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmtDateTime, fmtNumber, fmtValue } from '../format'
import type {
  DuplicateCandidate,
  EntityLinkInfo,
  EventInfo,
  MergePolicy,
  SourcePolicyView,
  ToastMessage,
} from '../types'

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

const POLICY_LABELS: Record<MergePolicy, string> = {
  confidence: 'Найвища довіра',
  freshness: 'Найсвіжіші дані',
  completeness: 'Найповніший запис',
  prefer_left: 'Пріоритет канонічного',
  prefer_right: 'Пріоритет поглинутого',
}

const LINK_STATUS_LABELS: Record<EntityLinkInfo['status'], string> = {
  merged: 'Злито',
  dismissed: 'Відхилено',
  unmerged: 'Роз’єднано',
}

export function DuplicatesPanel({
  taskId,
  refreshKey,
  onRefresh,
  onToast,
}: {
  taskId: string
  refreshKey: number
  onRefresh: () => Promise<void>
  onToast: (toast: ToastMessage) => void
}) {
  const [items, setItems] = useState<DuplicateCandidate[]>([])
  const [links, setLinks] = useState<EntityLinkInfo[]>([])
  const [scanned, setScanned] = useState(0)
  const [note, setNote] = useState('')
  const [threshold, setThreshold] = useState(88)
  const [policy, setPolicy] = useState<MergePolicy>('confidence')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)

  const load = async (targetThreshold = threshold) => {
    setLoading(true)
    setError(null)
    try {
      const response = await api.duplicates(taskId, targetThreshold)
      setItems(response.items)
      setLinks(response.links ?? [])
      setScanned(response.scanned)
      setNote(response.note)
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Не вдалося завантажити дублікати'
      setError(message)
      onToast({ kind: 'error', text: message })
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      setLoading(true)
      setError(null)
      try {
        const response = await api.duplicates(taskId, threshold)
        if (cancelled) return
        setItems(response.items)
        setLinks(response.links ?? [])
        setScanned(response.scanned)
        setNote(response.note)
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Не вдалося завантажити дублікати')
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [taskId, threshold, refreshKey])

  const handleMerge = async (
    item: DuplicateCandidate,
    canonicalKey: string,
    mergedKey: string,
  ) => {
    const opKey = `${item.left_key}-${item.right_key}`
    setBusyKey(opKey)
    try {
      const res = await api.mergeDuplicates(taskId, {
        canonical_key: canonicalKey,
        merged_key: mergedKey,
        policy,
        score: item.score,
        rule: 'ui_queue',
      })
      onToast({
        kind: 'success',
        text: `Злито ${mergedKey} → ${canonicalKey} (рядків: ${fmtNumber(res.row_count)})`,
      })
      await Promise.all([load(threshold), onRefresh()])
    } catch (err) {
      onToast({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Не вдалося злити записи',
      })
    } finally {
      setBusyKey(null)
    }
  }

  const handleDismiss = async (item: DuplicateCandidate) => {
    const opKey = `${item.left_key}-${item.right_key}`
    setBusyKey(opKey)
    try {
      await api.dismissDuplicate(taskId, {
        left_key: item.left_key,
        right_key: item.right_key,
        score: item.score,
        rule: 'ui_queue',
      })
      onToast({ kind: 'info', text: 'Пару позначено як не дублікат' })
      await load(threshold)
    } catch (err) {
      onToast({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Не вдалося відхилити пару',
      })
    } finally {
      setBusyKey(null)
    }
  }

  const handleUnmerge = async (link: EntityLinkInfo) => {
    setBusyKey(link.id)
    try {
      const res = await api.unmergeLink(taskId, link.id)
      onToast({
        kind: 'success',
        text: `Роз’єднано ${link.canonical_key} ⇄ ${link.merged_key} (рядків: ${fmtNumber(res.row_count)})`,
      })
      await Promise.all([load(threshold), onRefresh()])
    } catch (err) {
      onToast({
        kind: 'error',
        text: err instanceof Error ? err.message : 'Не вдалося роз’єднати зв’язок',
      })
    } finally {
      setBusyKey(null)
    }
  }

  if (loading && items.length === 0 && links.length === 0) {
    return (
      <section className="card">
        <div className="drawer-loading">
          <span className="spinner" /> Пошук дублікатів…
        </div>
      </section>
    )
  }

  if (error && items.length === 0 && links.length === 0) {
    return (
      <section className="card stack">
        <div className="alert alert--error">{error}</div>
        <div>
          <button className="btn btn--tiny" onClick={() => void load(threshold)}>
            Спробувати ще раз
          </button>
        </div>
      </section>
    )
  }

  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Черга перевірки дублікатів</h3>
          <div className="card-actions">
            <select
              className="select select--auto"
              value={policy}
              onChange={(event) => setPolicy(event.target.value as MergePolicy)}
              title="Політика вирішення конфліктів полів"
            >
              {Object.entries(POLICY_LABELS).map(([key, label]) => (
                <option key={key} value={key}>
                  Політика: {label}
                </option>
              ))}
            </select>
            <select
              className="select select--auto"
              value={threshold}
              onChange={(event) => setThreshold(Number(event.target.value))}
              title="Поріг схожості"
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
          Перевірено {fmtNumber(scanned)} активних записів. {note}
        </p>
        {error && <div className="alert alert--error">{error}</div>}
        {items.length === 0 ? (
          <div className="empty-hint">
            Нових кандидатів на злиття при порозі ≥ {threshold}% не знайдено.
          </div>
        ) : (
          <div className="dup-list">
            {items.map((item) => {
              const opKey = `${item.left_key}-${item.right_key}`
              const isBusy = busyKey === opKey
              const leftPayload = item.left_payload ?? {}
              const rightPayload = item.right_payload ?? {}
              const fieldKeys = Array.from(
                new Set([...Object.keys(leftPayload), ...Object.keys(rightPayload)]),
              )
              return (
                <article key={opKey} className="dup-card">
                  <div className="dup-head">
                    <div className="chip-row">
                      <span className={`conf ${item.score >= 0.95 ? 'conf--high' : 'conf--mid'}`}>
                        {Math.round(item.score * 100)}%
                      </span>
                      <strong>{item.left_name}</strong>
                      <span className="muted">⇄</span>
                      <strong>{item.right_name}</strong>
                    </div>
                    <code className="mono-small">
                      {item.left_key} · {item.right_key}
                    </code>
                  </div>

                  <div className="dup-compare">
                    <div className="dup-side">
                      <div className="dup-side-head">
                        <span className="chip">A: {item.left_key}</span>
                        {item.left_quality != null && (
                          <span className="mono-small">
                            якість {Math.round(item.left_quality * 100)}%
                          </span>
                        )}
                      </div>
                      {fieldKeys.map((field) => {
                        const lVal = leftPayload[field]
                        const rVal = rightPayload[field]
                        const differs = JSON.stringify(lVal ?? null) !== JSON.stringify(rVal ?? null)
                        return (
                          <div
                            className={`preview-row ${differs ? 'dup-diff' : ''}`}
                            key={`l-${field}`}
                          >
                            <span className="preview-key">{field}</span>
                            <span className="preview-value">{fmtValue(lVal)}</span>
                          </div>
                        )
                      })}
                    </div>

                    <div className="dup-side">
                      <div className="dup-side-head">
                        <span className="chip">B: {item.right_key}</span>
                        {item.right_quality != null && (
                          <span className="mono-small">
                            якість {Math.round(item.right_quality * 100)}%
                          </span>
                        )}
                      </div>
                      {fieldKeys.map((field) => {
                        const lVal = leftPayload[field]
                        const rVal = rightPayload[field]
                        const differs = JSON.stringify(lVal ?? null) !== JSON.stringify(rVal ?? null)
                        return (
                          <div
                            className={`preview-row ${differs ? 'dup-diff' : ''}`}
                            key={`r-${field}`}
                          >
                            <span className="preview-key">{field}</span>
                            <span className="preview-value">{fmtValue(rVal)}</span>
                          </div>
                        )
                      })}
                    </div>
                  </div>

                  <div className="quarantine-actions">
                    <button
                      className="btn btn--tiny btn--primary"
                      onClick={() => void handleMerge(item, item.left_key, item.right_key)}
                      disabled={busyKey !== null}
                    >
                      {isBusy ? 'Обробка…' : 'Злити в A'}
                    </button>
                    <button
                      className="btn btn--tiny"
                      onClick={() => void handleMerge(item, item.right_key, item.left_key)}
                      disabled={busyKey !== null}
                    >
                      Злити в B
                    </button>
                    <button
                      className="btn btn--tiny"
                      onClick={() => void handleDismiss(item)}
                      disabled={busyKey !== null}
                    >
                      Не дублікат
                    </button>
                  </div>
                </article>
              )
            })}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Зв’язки сутностей (merge / unmerge)</h3>
          <span className="chip">{links.length}</span>
        </div>
        {links.length === 0 ? (
          <div className="empty-hint">
            Ще не було злитих або відхилених пар. Усі рішення фіксуються тут із можливістю
            роз’єднання без втрати історії.
          </div>
        ) : (
          <div className="table-wrap">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Статус</th>
                  <th>Злиття / Пара</th>
                  <th>Політика</th>
                  <th>Коли</th>
                  <th>Дія</th>
                </tr>
              </thead>
              <tbody>
                {links.map((link) => (
                  <tr key={link.id}>
                    <td>
                      <span
                        className={`status ${
                          link.status === 'merged'
                            ? 'status--ok'
                            : link.status === 'dismissed'
                              ? 'status--muted'
                              : 'status--partial'
                        }`}
                      >
                        {LINK_STATUS_LABELS[link.status] ?? link.status}
                      </span>
                    </td>
                    <td>
                      <code className="mono-small">
                        {link.merged_key} → {link.canonical_key}
                      </code>
                    </td>
                    <td>
                      <span className="chip">{POLICY_LABELS[link.policy] ?? link.policy}</span>
                    </td>
                    <td className="mono-small">{fmtDateTime(link.updated_at)}</td>
                    <td>
                      {link.status === 'merged' ? (
                        <button
                          className="btn btn--tiny btn--danger"
                          onClick={() => void handleUnmerge(link)}
                          disabled={busyKey !== null}
                        >
                          {busyKey === link.id ? 'Відновлення…' : 'Роз’єднати'}
                        </button>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
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
