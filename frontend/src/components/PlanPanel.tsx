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

const DTYPES: FieldSpec['dtype'][] = [
  'string',
  'number',
  'integer',
  'boolean',
  'url',
  'email',
  'phone',
  'datetime',
  'array',
  'json',
]

const METHODS: { value: FieldSpec['method']; label: string }[] = [
  { value: 'css', label: 'css (селектор / авто)' },
  { value: 'llm', label: 'llm (AI-колонка)' },
  { value: 'enrichment', label: 'enrichment (збагачення)' },
  { value: 'regex', label: 'regex' },
  { value: 'json_ld', label: 'json_ld' },
  { value: 'manual', label: 'manual' },
  { value: 'api_json', label: 'api_json' },
]

const PII_OPTIONS: { value: FieldSpec['pii']; label: string }[] = [
  { value: 'none', label: 'немає' },
  { value: 'business_contact', label: 'бізнес-контакт' },
  { value: 'personal', label: 'персональні (PII)' },
]

interface ColumnDraft {
  key: string
  title: string
  dtype: FieldSpec['dtype']
  method: FieldSpec['method']
  pii: FieldSpec['pii']
  required: boolean
  selector: string
  attr: string
  prompt: string
  description: string
  aliases: string
}

const emptyDraft = (method: FieldSpec['method'] = 'css'): ColumnDraft => ({
  key: '',
  title: '',
  dtype: 'string',
  method,
  pii: 'none',
  required: false,
  selector: '',
  attr: '',
  prompt: '',
  description: '',
  aliases: '',
})

const draftFromField = (field: FieldSpec): ColumnDraft => ({
  key: field.key,
  title: field.title,
  dtype: field.dtype,
  method: field.method,
  pii: field.pii,
  required: field.required,
  selector: field.selector ?? '',
  attr: field.attr ?? '',
  prompt: field.prompt ?? '',
  description: field.description ?? '',
  aliases: field.aliases.join(', '),
})

const slugKey = (title: string): string => {
  const cleaned = title
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 30)
  return /^[a-z][a-z0-9_]{1,}$/.test(cleaned)
    ? cleaned
    : `col_${Date.now().toString(36).slice(-5)}`
}

export function PlanPanel({ task, quality, enrichers, onRefresh, onToast }: Props) {
  const spec = task.spec
  const [showAddForm, setShowAddForm] = useState(false)
  const [addDraft, setAddDraft] = useState<ColumnDraft>(() => emptyDraft('css'))
  const [editingKey, setEditingKey] = useState<string | null>(null)
  const [editDraft, setEditDraft] = useState<ColumnDraft | null>(null)
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

  const openAddForm = (method: FieldSpec['method']) => {
    setAddDraft(emptyDraft(method))
    setShowAddForm(true)
    setEditingKey(null)
    setEditDraft(null)
  }

  const addColumn = async () => {
    const title = addDraft.title.trim()
    if (!title) {
      onToast({ kind: 'error', text: 'Вкажіть назву колонки' })
      return
    }
    if (addDraft.method === 'llm' && !addDraft.prompt.trim()) {
      onToast({ kind: 'error', text: 'Для AI-колонки потрібен промпт' })
      return
    }
    const key = addDraft.key.trim() || slugKey(title)
    const aliases = addDraft.aliases
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    const field: Partial<FieldSpec> = {
      key,
      title,
      dtype: addDraft.dtype,
      method: addDraft.method,
      pii: addDraft.pii,
      required: addDraft.required,
      selector: addDraft.selector.trim() || null,
      attr: addDraft.attr.trim() || null,
      prompt: addDraft.prompt.trim() || null,
      description: (addDraft.description || addDraft.prompt).trim(),
      aliases,
    }
    await run(async () => {
      await api.addColumn(task.id, field)
      setAddDraft(emptyDraft('css'))
      setShowAddForm(false)
    }, `Колонку «${title}» додано`)
  }

  const startEdit = (column: FieldSpec) => {
    if (editingKey === column.key) {
      setEditingKey(null)
      setEditDraft(null)
      return
    }
    setShowAddForm(false)
    setEditingKey(column.key)
    setEditDraft(draftFromField(column))
  }

  const saveEdit = async () => {
    if (!editingKey || !editDraft) return
    const title = editDraft.title.trim()
    if (!title) {
      onToast({ kind: 'error', text: 'Назва колонки не може бути порожньою' })
      return
    }
    if (editDraft.method === 'llm' && !editDraft.prompt.trim()) {
      onToast({ kind: 'error', text: 'Для методу llm потрібен промпт' })
      return
    }
    const aliases = editDraft.aliases
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    await run(async () => {
      await api.updateColumn(task.id, editingKey, {
        title,
        dtype: editDraft.dtype,
        method: editDraft.method,
        pii: editDraft.pii,
        required: editDraft.required,
        selector: editDraft.selector.trim() || null,
        attr: editDraft.attr.trim() || null,
        prompt: editDraft.prompt.trim() || null,
        description: editDraft.description.trim(),
        aliases,
      })
      setEditingKey(null)
      setEditDraft(null)
    }, `Колонку «${title}» оновлено`)
  }

  const moveColumn = (index: number, delta: -1 | 1) => {
    const target = index + delta
    if (target < 0 || target >= spec.columns.length) return
    const keys = spec.columns.map((column) => column.key)
    const [moved] = keys.splice(index, 1)
    keys.splice(target, 0, moved)
    void run(() => api.reorderColumns(task.id, keys), 'Порядок колонок оновлено')
  }

  const deleteColumn = (key: string) => {
    void run(async () => {
      await api.deleteColumn(task.id, key)
      if (editingKey === key) {
        setEditingKey(null)
        setEditDraft(null)
      }
    }, `Колонку ${key} видалено`)
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
        {spec.api_source && (
          <div className="api-summary">
            <div className="meta-row">
              <span className="chip chip--yes">JSON API</span>
              <span className="mono-small">items: {spec.api_source.items_path}</span>
              <span className="mono-small">id: {spec.api_source.id_path}</span>
              {spec.api_source.cursor_path && (
                <span className="mono-small">
                  cursor: {spec.api_source.cursor_path} ({spec.api_source.cursor_param})
                </span>
              )}
              <span className="chip">
                {spec.api_source.incremental ? 'інкрементальний' : 'повний прохід'}
              </span>
            </div>
            {Object.keys(spec.api_source.field_map).length > 0 && (
              <div className="enrich-fields">
                {Object.entries(spec.api_source.field_map).map(([key, path]) => (
                  <span className="chip" key={key}>
                    {key} ← {path}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Колонки датасету (Spec Editor)</h3>
          <div className="card-actions">
            <button
              className="btn btn--ghost"
              onClick={() => (showAddForm ? setShowAddForm(false) : openAddForm('css'))}
            >
              + Додати колонку
            </button>
            <button className="btn btn--ghost" onClick={() => openAddForm('llm')}>
              + AI-колонка
            </button>
          </div>
        </div>

        {showAddForm && (
          <div className="ai-form">
            <div className="form-row">
              <label className="field">
                <span className="field-label">Назва колонки</span>
                <input
                  className="input"
                  value={addDraft.title}
                  onChange={(event) => setAddDraft({ ...addDraft, title: event.target.value })}
                  placeholder="Напр. Тариф або Оцінка болю"
                />
              </label>
              <label className="field">
                <span className="field-label">Ключ (snake_case)</span>
                <input
                  className="input mono"
                  value={addDraft.key}
                  onChange={(event) => setAddDraft({ ...addDraft, key: event.target.value })}
                  placeholder="tier_badge"
                />
              </label>
              <label className="field field--small">
                <span className="field-label">Тип</span>
                <select
                  className="select"
                  value={addDraft.dtype}
                  onChange={(event) =>
                    setAddDraft({ ...addDraft, dtype: event.target.value as FieldSpec['dtype'] })
                  }
                >
                  {DTYPES.map((dtype) => (
                    <option key={dtype} value={dtype}>
                      {dtype}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Метод</span>
                <select
                  className="select"
                  value={addDraft.method}
                  onChange={(event) =>
                    setAddDraft({ ...addDraft, method: event.target.value as FieldSpec['method'] })
                  }
                >
                  {METHODS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">PII</span>
                <select
                  className="select"
                  value={addDraft.pii}
                  onChange={(event) =>
                    setAddDraft({ ...addDraft, pii: event.target.value as FieldSpec['pii'] })
                  }
                >
                  {PII_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            {addDraft.method !== 'llm' && (
              <div className="form-row">
                <label className="field">
                  <span className="field-label">Ручний CSS-селектор (опціонально)</span>
                  <input
                    className="input mono"
                    value={addDraft.selector}
                    onChange={(event) => setAddDraft({ ...addDraft, selector: event.target.value })}
                    placeholder=".clinic-card__badge або a.website"
                  />
                </label>
                <label className="field field--small">
                  <span className="field-label">Атрибут (опціонально)</span>
                  <input
                    className="input mono"
                    value={addDraft.attr}
                    onChange={(event) => setAddDraft({ ...addDraft, attr: event.target.value })}
                    placeholder="href, data-tier"
                  />
                </label>
              </div>
            )}

            {addDraft.method === 'llm' && (
              <label className="field">
                <span className="field-label">
                  Промпт для кожного рядка
                  <span className="hint">модель не отримує персональні дані</span>
                </span>
                <textarea
                  className="textarea"
                  rows={2}
                  value={addDraft.prompt}
                  onChange={(event) => setAddDraft({ ...addDraft, prompt: event.target.value })}
                  placeholder="Оціни 0-100, наскільки компанія потребує наших послуг. Поясни одним реченням."
                />
              </label>
            )}

            <div className="form-row form-row--baseline">
              <label className="checkbox-inline">
                <input
                  type="checkbox"
                  checked={addDraft.required}
                  onChange={(event) => setAddDraft({ ...addDraft, required: event.target.checked })}
                />
                <span>Обовʼязкова колонка</span>
              </label>
              <label className="field">
                <span className="field-label">Синоніми / аліаси (через кому)</span>
                <input
                  className="input"
                  value={addDraft.aliases}
                  onChange={(event) => setAddDraft({ ...addDraft, aliases: event.target.value })}
                  placeholder="тариф, план"
                />
              </label>
            </div>

            <div className="form-actions">
              <button className="btn btn--primary" onClick={addColumn} disabled={busy}>
                Додати
              </button>
              <button className="btn" onClick={() => setShowAddForm(false)}>
                Закрити
              </button>
            </div>
          </div>
        )}

        {editingKey && editDraft && (
          <div className="ai-form column-editor">
            <div className="card-head">
              <strong className="mono-small">Редагування колонки: {editingKey}</strong>
              <button
                className="tiny-btn"
                onClick={() => {
                  setEditingKey(null)
                  setEditDraft(null)
                }}
              >
                закрити
              </button>
            </div>
            <div className="form-row">
              <label className="field">
                <span className="field-label">Назва</span>
                <input
                  className="input"
                  value={editDraft.title}
                  onChange={(event) => setEditDraft({ ...editDraft, title: event.target.value })}
                />
              </label>
              <label className="field field--small">
                <span className="field-label">Тип</span>
                <select
                  className="select"
                  value={editDraft.dtype}
                  onChange={(event) =>
                    setEditDraft({ ...editDraft, dtype: event.target.value as FieldSpec['dtype'] })
                  }
                >
                  {DTYPES.map((dtype) => (
                    <option key={dtype} value={dtype}>
                      {dtype}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Метод</span>
                <select
                  className="select"
                  value={editDraft.method}
                  onChange={(event) =>
                    setEditDraft({
                      ...editDraft,
                      method: event.target.value as FieldSpec['method'],
                    })
                  }
                >
                  {METHODS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">PII</span>
                <select
                  className="select"
                  value={editDraft.pii}
                  onChange={(event) =>
                    setEditDraft({ ...editDraft, pii: event.target.value as FieldSpec['pii'] })
                  }
                >
                  {PII_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
            </div>

            <div className="form-row">
              <label className="field">
                <span className="field-label">Ручний CSS-селектор</span>
                <input
                  className="input mono"
                  value={editDraft.selector}
                  onChange={(event) => setEditDraft({ ...editDraft, selector: event.target.value })}
                  placeholder="Порожньо = автоматичний вибір"
                />
              </label>
              <label className="field field--small">
                <span className="field-label">Атрибут</span>
                <input
                  className="input mono"
                  value={editDraft.attr}
                  onChange={(event) => setEditDraft({ ...editDraft, attr: event.target.value })}
                  placeholder="text (за замовч.)"
                />
              </label>
            </div>

            {editDraft.method === 'llm' && (
              <label className="field">
                <span className="field-label">Промпт</span>
                <textarea
                  className="textarea"
                  rows={2}
                  value={editDraft.prompt}
                  onChange={(event) => setEditDraft({ ...editDraft, prompt: event.target.value })}
                />
              </label>
            )}

            <div className="form-row form-row--baseline">
              <label className="checkbox-inline">
                <input
                  type="checkbox"
                  checked={editDraft.required}
                  onChange={(event) => setEditDraft({ ...editDraft, required: event.target.checked })}
                />
                <span>Обовʼязкова колонка</span>
              </label>
              <label className="field">
                <span className="field-label">Аліаси (через кому)</span>
                <input
                  className="input"
                  value={editDraft.aliases}
                  onChange={(event) => setEditDraft({ ...editDraft, aliases: event.target.value })}
                />
              </label>
            </div>

            <div className="form-actions">
              <button className="btn btn--primary" onClick={saveEdit} disabled={busy}>
                Зберегти колонку
              </button>
              <button
                className="btn"
                onClick={() => {
                  setEditingKey(null)
                  setEditDraft(null)
                }}
              >
                Скасувати
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
              {spec.columns.map((column, index) => {
                const fill = quality[column.key]
                const isEditing = editingKey === column.key
                return (
                  <tr key={column.key} className={isEditing ? 'row--editing' : undefined}>
                    <td>
                      <div className="cell-title">
                        {column.title}{' '}
                        {column.required && <span className="chip chip--yes">обовʼяз.</span>}
                      </div>
                      <code className="mono-small">{column.key}</code>
                      {column.selector && (
                        <div className="selector-preview">
                          <code className="selector selector--manual">
                            {column.selector}
                            {column.attr ? ` @${column.attr}` : ''}
                          </code>
                        </div>
                      )}
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
                    <td className="td-actions td-actions--wide">
                      <div className="row-actions">
                        <button
                          className="icon-btn"
                          title="Перемістити вище"
                          onClick={() => moveColumn(index, -1)}
                          disabled={busy || index === 0}
                        >
                          ↑
                        </button>
                        <button
                          className="icon-btn"
                          title="Перемістити нижче"
                          onClick={() => moveColumn(index, 1)}
                          disabled={busy || index === spec.columns.length - 1}
                        >
                          ↓
                        </button>
                        <button
                          className="icon-btn"
                          title="Редагувати колонку"
                          onClick={() => startEdit(column)}
                          disabled={busy}
                        >
                          ✎
                        </button>
                        <button
                          className="icon-btn icon-btn--danger"
                          title="Видалити колонку"
                          onClick={() => deleteColumn(column.key)}
                          disabled={busy || spec.columns.length <= 1}
                        >
                          ✕
                        </button>
                      </div>
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
