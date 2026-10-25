import { useState } from 'react'
import { api } from '../api'
import { fmtNumber, piiLabel } from '../format'
import type { EnricherInfo, FieldSpec, TaskInfo, ToastMessage } from '../types'

interface Props {
  task: TaskInfo
  quality: Record<string, number>
  enrichers: EnricherInfo[]
  onRefresh: () => Promise<void>
  onToast: (toast: ToastMessage) => void
}

const DTYPES = ['string', 'number', 'integer', 'boolean', 'url', 'email', 'phone', 'datetime', 'array']

export function PlanPanel({ task, quality, enrichers, onRefresh, onToast }: Props) {
  const spec = task.spec
  const [showAiForm, setShowAiForm] = useState(false)
  const [aiTitle, setAiTitle] = useState('')
  const [aiKey, setAiKey] = useState('')
  const [aiPrompt, setAiPrompt] = useState('')
  const [aiDtype, setAiDtype] = useState('string')
  const [busy, setBusy] = useState(false)
  const [seedsDraft, setSeedsDraft] = useState(spec.seeds.join('\n'))
  const [limits, setLimits] = useState({ ...spec.limits })
  const [editingSeeds, setEditingSeeds] = useState(false)

  const run = async (action: () => Promise<unknown>, message: string) => {
    setBusy(true)
    try {
      await action()
      await onRefresh()
      onToast({ kind: 'success', text: message })
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка' })
    } finally {
      setBusy(false)
    }
  }

  const addAiColumn = async () => {
    const title = aiTitle.trim()
    if (!title || !aiPrompt.trim()) {
      onToast({ kind: 'error', text: 'Заповніть назву та промпт колонки' })
      return
    }
    const key =
      aiKey.trim() ||
      title
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/^_+|_+$/g, '')
        .slice(0, 30) ||
      `ai_${Date.now().toString(36).slice(-5)}`
    const field: Partial<FieldSpec> = {
      key,
      title,
      dtype: aiDtype as FieldSpec['dtype'],
      method: 'llm',
      pii: 'none',
      prompt: aiPrompt.trim(),
      description: aiPrompt.trim(),
      aliases: [],
    }
    await run(async () => {
      await api.addColumn(task.id, field)
      setAiTitle('')
      setAiKey('')
      setAiPrompt('')
      setShowAiForm(false)
    }, `AI-колонку «${title}» додано`)
  }

  const deleteColumn = (key: string) => {
    void run(() => api.deleteColumn(task.id, key), `Колонку ${key} видалено`)
  }

  const saveSeeds = () =>
    run(async () => {
      const next = structuredClone(spec)
      next.seeds = seedsDraft
        .split(/[\n,]+/)
        .map((value) => value.trim())
        .filter(Boolean)
      await api.updateTask(task.id, { spec: next })
      setEditingSeeds(false)
    }, 'Стартові URL збережено. Перезапустіть калібрування.')

  const saveLimits = () =>
    run(async () => {
      await api.updateLimits(task.id, limits)
    }, 'Ліміти оновлено')

  const enricherTitle = (key: string) =>
    enrichers.find((enricher) => enricher.key === key)?.title ?? key

  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">План задачі</h3>
          <span className="chip">{spec.entity}</span>
        </div>
        <p className="lede">{spec.rationale || spec.prompt}</p>
        {spec.warning && <div className="alert alert--warning">{spec.warning}</div>}
        {spec.assumptions.length > 0 && (
          <ul className="assumption-list">
            {spec.assumptions.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        )}
        <div className="meta-row">
          <span>{spec.columns.length} колонок</span>
          <span>{spec.seeds.length} джерел</span>
          <span>{spec.enrichment.length} збагачувачів</span>
          <span>extraction: {spec.extraction}</span>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Колонки датасету</h3>
          <button className="btn btn--ghost" onClick={() => setShowAiForm((value) => !value)}>
            + AI-колонка
          </button>
        </div>

        {showAiForm && (
          <div className="ai-form">
            <div className="form-row">
              <label className="field">
                <span className="field-label">Назва колонки</span>
                <input
                  className="input"
                  value={aiTitle}
                  onChange={(event) => setAiTitle(event.target.value)}
                  placeholder="Оцінка болю"
                />
              </label>
              <label className="field">
                <span className="field-label">Ключ (snake_case)</span>
                <input
                  className="input mono"
                  value={aiKey}
                  onChange={(event) => setAiKey(event.target.value)}
                  placeholder="pain_score"
                />
              </label>
              <label className="field field--small">
                <span className="field-label">Тип</span>
                <select
                  className="select"
                  value={aiDtype}
                  onChange={(event) => setAiDtype(event.target.value)}
                >
                  {DTYPES.map((dtype) => (
                    <option key={dtype} value={dtype}>
                      {dtype}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <label className="field">
              <span className="field-label">
                Промпт для кожного рядка
                <span className="hint">модель не отримує персональні дані</span>
              </span>
              <textarea
                className="textarea"
                rows={2}
                value={aiPrompt}
                onChange={(event) => setAiPrompt(event.target.value)}
                placeholder="Оціни 0-100, наскільки компанія потребує наших послуг. Поясни одним реченням."
              />
            </label>
            <div className="form-actions">
              <button className="btn btn--primary" onClick={addAiColumn} disabled={busy}>
                Додати
              </button>
              <button className="btn" onClick={() => setShowAiForm(false)}>
                Закрити
              </button>
            </div>
          </div>
        )}

        <div className="table-wrap">
          <table className="table table--compact">
            <thead>
              <tr>
                <th>Колонка</th>
                <th>Тип</th>
                <th>Метод</th>
                <th>PII</th>
                <th className="th-bar">Заповненість</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {spec.columns.map((column) => {
                const fill = quality[column.key]
                return (
                  <tr key={column.key}>
                    <td>
                      <div className="cell-title">{column.title}</div>
                      <code className="mono-small">{column.key}</code>
                    </td>
                    <td>
                      <span className="chip">{column.dtype}</span>
                    </td>
                    <td>
                      <span className={`method method--${column.method}`}>{column.method}</span>
                    </td>
                    <td>
                      {piiLabel(column.pii) ? (
                        <span className="chip chip--pii">{piiLabel(column.pii)}</span>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td>
                      <div className="fill">
                        <div className="fill-bar">
                          <span style={{ width: `${Math.min(100, fill ?? 0)}%` }} />
                        </div>
                        <span className="fill-value">
                          {fill === undefined ? '—' : `${fmtNumber(Math.round(fill))}%`}
                        </span>
                      </div>
                    </td>
                    <td className="td-actions">
                      <button
                        className="icon-btn icon-btn--danger"
                        title="Видалити колонку"
                        onClick={() => deleteColumn(column.key)}
                        disabled={busy || spec.columns.length <= 1}
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid-2">
        <section className="card">
          <div className="card-head">
            <h3 className="card-title">Стартові URL</h3>
            <button className="tiny-btn" onClick={() => setEditingSeeds((value) => !value)}>
              {editingSeeds ? 'скасувати' : 'редагувати'}
            </button>
          </div>
          {editingSeeds ? (
            <>
              <textarea
                className="textarea textarea--mono"
                rows={4}
                value={seedsDraft}
                onChange={(event) => setSeedsDraft(event.target.value)}
              />
              <div className="form-actions">
                <button className="btn btn--primary" onClick={saveSeeds} disabled={busy}>
                  Зберегти
                </button>
              </div>
            </>
          ) : (
            <ul className="link-list">
              {spec.seeds.length === 0 && <li className="muted">Не задано — додайте URL</li>}
              {spec.seeds.map((seed) => (
                <li key={seed}>
                  <a className="link" href={seed} target="_blank" rel="noreferrer">
                    {seed}
                  </a>
                </li>
              ))}
            </ul>
          )}

          <div className="divider" />
          <h4 className="sub-title">Ліміти прогону</h4>
          <div className="form-row">
            <label className="field field--small">
              <span className="field-label">Сторінок</span>
              <input
                className="input"
                type="number"
                min={1}
                value={limits.max_pages}
                onChange={(event) =>
                  setLimits({ ...limits, max_pages: Number(event.target.value) || 1 })
                }
              />
            </label>
            <label className="field field--small">
              <span className="field-label">Глибина</span>
              <input
                className="input"
                type="number"
                min={0}
                max={3}
                value={limits.max_depth}
                onChange={(event) =>
                  setLimits({ ...limits, max_depth: Number(event.target.value) || 0 })
                }
              />
            </label>
            <label className="field field--small">
              <span className="field-label">Бюджет, $</span>
              <input
                className="input"
                type="number"
                min={0}
                step={0.05}
                value={limits.budget_usd}
                onChange={(event) =>
                  setLimits({ ...limits, budget_usd: Number(event.target.value) || 0 })
                }
              />
            </label>
            <label className="field field--small">
              <span className="field-label">Семпл</span>
              <input
                className="input"
                type="number"
                min={1}
                max={20}
                value={limits.sample_size}
                onChange={(event) =>
                  setLimits({ ...limits, sample_size: Number(event.target.value) || 1 })
                }
              />
            </label>
          </div>
          <div className="form-actions">
            <button className="btn" onClick={saveLimits} disabled={busy}>
              Зберегти ліміти
            </button>
          </div>
        </section>

        <section className="card">
          <h3 className="card-title">Waterfall збагачення</h3>
          {spec.enrichment.length === 0 && (
            <div className="empty-hint">Збагачення не налаштовано.</div>
          )}
          <ul className="enrich-list">
            {spec.enrichment.map((item) => (
              <li key={item.enricher}>
                <div className="enrich-title">
                  {enricherTitle(item.enricher)}
                  {!item.enabled && <span className="chip chip--muted">вимк.</span>}
                </div>
                <div className="enrich-fields">
                  {item.fields.map((field) => (
                    <span className="chip" key={field}>
                      {field}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
          <div className="divider" />
          <h4 className="sub-title">Як працює</h4>
          <ol className="step-list">
            <li>Калібрування: 2-4 сторінки → детермінована CSS-схема.</li>
            <li>Збір: масовий парсинг без LLM, self-healing при зміні верстки.</li>
            <li>Збагачення: контакти, технології, AI-колонки.</li>
            <li>Снапшот: Parquet + виявлення змін до попереднього зрізу.</li>
          </ol>
        </section>
      </div>
    </div>
  )
}
