import { useEffect, useState } from 'react'
import type { ApiSourceSpec } from '../types'

interface Props {
  open: boolean
  busy: boolean
  onClose: () => void
  onCreate: (payload: {
    prompt: string
    seeds: string[]
    title?: string
    api_source?: ApiSourceSpec | null
  }) => Promise<void>
}

interface ExampleItem {
  label: string
  prompt: string
  seeds?: string
  title?: string
}

const EXAMPLES: ExampleItem[] = [
  {
    label: 'Prozorro Переможці (B2B)',
    prompt:
      'Знайди переможців тендерів Prozorro через API від 1 млн грн та проведи аудит їх сайтів, контактів і техстеку',
    seeds: 'https://public.api.openprocurement.org/api/2.5/tenders',
    title: 'Prozorro: переможці тендерів (B2B)',
  },
  {
    label: 'Prozorro API (тендери)',
    prompt:
      'Збери відкриті тендери Prozorro через API: ID, предмет закупівлі, замовник, ЄДРПОУ, CPV, регіон, сума, статус, дедлайн',
    seeds: 'https://public.api.openprocurement.org/api/2.5/tenders',
    title: 'Prozorro: відкриті тендери',
  },
  {
    label: 'Prozorro.Продажі API',
    prompt:
      'Збери аукціони Prozorro.Продажі через API: ID лота, назва, організатор, ЄДРПОУ, регіон, стартова ціна, статус, дедлайн',
    seeds: 'https://public.api.openprocurement.org/api/0/auctions',
    title: 'Prozorro.Продажі: аукціони',
  },
  {
    label: 'Локальний бізнес',
    prompt:
      'Збери стоматологічні клініки Києва з рейтингом нижче 4.2: назва, адреса, телефон, сайт, email, чи є онлайн-запис',
  },
  {
    label: 'B2B компанії',
    prompt:
      'Знайди будівельні компанії Київської області: назва, сайт, місто, телефон, email, опис діяльності, технології сайту',
  },
  {
    label: 'E-commerce',
    prompt:
      'Збери товари категорії «офісні крісла»: назва, артикул, ціна, наявність, рейтинг, кількість відгуків, фото',
  },
  {
    label: 'Вакансії як сигнал',
    prompt:
      'Збери компанії, які зараз шукають маркетолога або РОПа: компанія, посада, зарплата, місто, дата публікації',
  },
]

const DEFAULT_FIELD_MAP = `{
  "title": "title",
  "name": "procuringEntity.name",
  "amount": "value.amount|minimalStep.amount"
}`

export function NewTask({ open, busy, onClose, onCreate }: Props) {
  const [prompt, setPrompt] = useState('')
  const [seeds, setSeeds] = useState('')
  const [title, setTitle] = useState('')
  const [useCustomApi, setUseCustomApi] = useState(false)
  const [itemsPath, setItemsPath] = useState('data')
  const [idPath, setIdPath] = useState('id')
  const [cursorPath, setCursorPath] = useState('next_page.offset')
  const [cursorParam, setCursorParam] = useState('offset')
  const [nextPagePath, setNextPagePath] = useState('next_page.uri')
  const [detailUrlTemplate, setDetailUrlTemplate] = useState('')
  const [recordUrlTemplate, setRecordUrlTemplate] = useState('')
  const [incremental, setIncremental] = useState(true)
  const [fieldMapText, setFieldMapText] = useState(DEFAULT_FIELD_MAP)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  const applyExample = (example: ExampleItem) => {
    setPrompt(example.prompt)
    if (example.seeds) setSeeds(example.seeds)
    if (example.title) setTitle(example.title)
  }

  const submit = async () => {
    setError(null)
    if (prompt.trim().length < 5) {
      setError('Опишіть задачу хоча б одним реченням')
      return
    }
    const seedList = seeds
      .split(/[\n,]+/)
      .map((value) => value.trim())
      .filter(Boolean)

    let apiSource: ApiSourceSpec | null = null
    if (useCustomApi) {
      let parsedMap: Record<string, string> = {}
      const trimmedMap = fieldMapText.trim()
      if (trimmedMap) {
        try {
          const raw: unknown = JSON.parse(trimmedMap)
          if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
            setError('Мапінг полів має бути JSON-об’єктом (ключ → JSON-шлях)')
            return
          }
          for (const [k, v] of Object.entries(raw)) {
            if (typeof v !== 'string') {
              setError(`Шлях для поля «${k}» у field_map має бути рядком`)
              return
            }
            parsedMap[k] = v
          }
        } catch {
          setError('Некоректний JSON у полі «Мапінг полів (field_map)»')
          return
        }
      }
      if (!itemsPath.trim() || !idPath.trim()) {
        setError('Для JSON API вкажіть шлях до масиву записів та шлях до ID')
        return
      }
      apiSource = {
        items_path: itemsPath.trim(),
        id_path: idPath.trim(),
        next_page_path: nextPagePath.trim() || null,
        cursor_path: cursorPath.trim() || null,
        cursor_param: cursorParam.trim() || 'offset',
        detail_url_template: detailUrlTemplate.trim() || null,
        record_url_template: recordUrlTemplate.trim() || null,
        field_map: parsedMap,
        incremental,
        headers: {},
      }
    }

    try {
      await onCreate({
        prompt: prompt.trim(),
        seeds: seedList,
        title: title.trim() || undefined,
        api_source: apiSource,
      })
      setPrompt('')
      setSeeds('')
      setTitle('')
      setUseCustomApi(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не вдалося створити задачу')
    }
  }

  return (
    <div className="overlay" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal-head">
          <div>
            <div className="eyebrow">Task Studio</div>
            <h2>Що потрібно зібрати?</h2>
          </div>
          <button className="icon-btn" onClick={onClose} aria-label="Закрити">
            ✕
          </button>
        </div>

        <div className="modal-body">
          <label className="field">
            <span className="field-label">Бізнес-питання природною мовою</span>
            <textarea
              className="textarea"
              rows={4}
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="Наприклад: збери забудовників котеджних містечок Київської області — сайти, контакти, ціни за м² та відкриті вакансії"
              autoFocus
            />
          </label>

          <div className="examples">
            {EXAMPLES.map((example) => (
              <button
                key={example.label}
                className="example"
                onClick={() => applyExample(example)}
                type="button"
              >
                {example.label}
              </button>
            ))}
          </div>

          <label className="field">
            <span className="field-label">
              Стартові URL <span className="hint">по одному в рядку · каталог, рубрика, сторінка або Open Data API</span>
            </span>
            <textarea
              className="textarea textarea--mono"
              rows={3}
              value={seeds}
              onChange={(event) => setSeeds(event.target.value)}
              placeholder={'https://example.com/catalog\nhttps://public.api.openprocurement.org/api/2.5/tenders'}
            />
          </label>

          <label className="field">
            <span className="field-label">Назва задачі (необов&apos;язково)</span>
            <input
              className="input"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Клініки Києва з низьким рейтингом"
            />
          </label>

          <details className="api-details">
            <summary>Налаштувати відкритий JSON API (api_source) — для кастомних API</summary>
            <div className="api-details-body">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={useCustomApi}
                  onChange={(event) => setUseCustomApi(event.target.checked)}
                />
                <span>Передати власну конфігурацію JSON API (замість авто-профілю)</span>
              </label>

              {useCustomApi && (
                <div className="stack">
                  <div className="form-row">
                    <label className="field">
                      <span className="field-label">Шлях до списку (items_path)</span>
                      <input
                        className="input input--mono"
                        value={itemsPath}
                        onChange={(event) => setItemsPath(event.target.value)}
                        placeholder="data"
                      />
                    </label>
                    <label className="field">
                      <span className="field-label">Шлях до ID (id_path)</span>
                      <input
                        className="input input--mono"
                        value={idPath}
                        onChange={(event) => setIdPath(event.target.value)}
                        placeholder="id"
                      />
                    </label>
                  </div>

                  <div className="form-row">
                    <label className="field">
                      <span className="field-label">Шлях курсора (cursor_path)</span>
                      <input
                        className="input input--mono"
                        value={cursorPath}
                        onChange={(event) => setCursorPath(event.target.value)}
                        placeholder="next_page.offset"
                      />
                    </label>
                    <label className="field field--small">
                      <span className="field-label">Query-параметр</span>
                      <input
                        className="input input--mono"
                        value={cursorParam}
                        onChange={(event) => setCursorParam(event.target.value)}
                        placeholder="offset"
                      />
                    </label>
                    <label className="field">
                      <span className="field-label">next_page_path</span>
                      <input
                        className="input input--mono"
                        value={nextPagePath}
                        onChange={(event) => setNextPagePath(event.target.value)}
                        placeholder="next_page.uri"
                      />
                    </label>
                  </div>

                  <div className="form-row">
                    <label className="field">
                      <span className="field-label">Шаблон detail URL</span>
                      <input
                        className="input input--mono"
                        value={detailUrlTemplate}
                        onChange={(event) => setDetailUrlTemplate(event.target.value)}
                        placeholder="https://api.example.com/items/{id}"
                      />
                    </label>
                    <label className="field">
                      <span className="field-label">Шаблон публічного посилання</span>
                      <input
                        className="input input--mono"
                        value={recordUrlTemplate}
                        onChange={(event) => setRecordUrlTemplate(event.target.value)}
                        placeholder="https://example.com/item/{id}"
                      />
                    </label>
                  </div>

                  <label className="field">
                    <span className="field-label">
                      Мапінг полів (field_map JSON)
                      <span className="hint">колонка → шлях із резервом через |</span>
                    </span>
                    <textarea
                      className="textarea textarea--mono"
                      rows={4}
                      value={fieldMapText}
                      onChange={(event) => setFieldMapText(event.target.value)}
                    />
                  </label>

                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={incremental}
                      onChange={(event) => setIncremental(event.target.checked)}
                    />
                    <span>Інкрементальне довантаження (зберігати курсор між прогонами)</span>
                  </label>
                </div>
              )}
            </div>
          </details>

          {error && <div className="alert alert--error">{error}</div>}

          <div className="modal-actions">
            <button className="btn" onClick={onClose} disabled={busy}>
              Скасувати
            </button>
            <button className="btn btn--primary" onClick={submit} disabled={busy}>
              {busy ? 'Генерую схему…' : 'Створити задачу'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
