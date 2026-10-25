import { useEffect, useState } from 'react'

interface Props {
  open: boolean
  busy: boolean
  onClose: () => void
  onCreate: (payload: { prompt: string; seeds: string[]; title?: string }) => Promise<void>
}

const EXAMPLES = [
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

export function NewTask({ open, busy, onClose, onCreate }: Props) {
  const [prompt, setPrompt] = useState('')
  const [seeds, setSeeds] = useState('')
  const [title, setTitle] = useState('')
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
    try {
      await onCreate({ prompt: prompt.trim(), seeds: seedList, title: title.trim() || undefined })
      setPrompt('')
      setSeeds('')
      setTitle('')
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
                onClick={() => setPrompt(example.prompt)}
                type="button"
              >
                {example.label}
              </button>
            ))}
          </div>

          <label className="field">
            <span className="field-label">
              Стартові URL <span className="hint">по одному в рядку · каталог, рубрика або сторінка компанії</span>
            </span>
            <textarea
              className="textarea textarea--mono"
              rows={3}
              value={seeds}
              onChange={(event) => setSeeds(event.target.value)}
              placeholder={'https://example.com/catalog\nhttps://example.com/clinics'}
            />
          </label>

          <label className="field">
            <span className="field-label">Назва задачі (необов'язково)</span>
            <input
              className="input"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Клініки Києва з низьким рейтингом"
            />
          </label>

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
