import type { CalibrationReport } from '../types'
import { fmtNumber, fmtValue } from '../format'

interface Props {
  report: CalibrationReport | null
  busy: boolean
  canRun: boolean
  onCalibrate: () => void
}

const STRATEGY_LABELS: Record<string, string> = {
  css: 'CSS-селектори',
  json_ld: 'JSON-LD',
  llm: 'LLM',
  none: 'не визначено',
}

export function CalibrationPanel({ report, busy, canRun, onCalibrate }: Props) {
  if (!report) {
    return (
      <section className="card empty-card">
        <div className="empty-icon">◎</div>
        <h3>Калібрування ще не запущене</h3>
        <p>
          Студія завантажить 2-4 сторінки, знайде повторювані картки й згенерує детерміновану
          схему екстракції. Це головна економія: LLM платить один раз за схему, а не за кожну
          сторінку.
        </p>
        <button className="btn btn--primary" onClick={onCalibrate} disabled={busy || !canRun}>
          Запустити калібрування
        </button>
        {!canRun && <div className="alert alert--warning">Спершу додайте стартові URL.</div>}
      </section>
    )
  }

  const avgCoverage =
    Object.values(report.coverage).length > 0
      ? Object.values(report.coverage).reduce((a, b) => a + b, 0) / Object.values(report.coverage).length
      : 0

  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Результат калібрування</h3>
          <button className="btn btn--ghost" onClick={onCalibrate} disabled={busy || !canRun}>
            Перекалібрувати
          </button>
        </div>
        <div className="stats-grid">
          <div className="stat">
            <span className="stat-label">Стратегія</span>
            <span className="stat-value">{STRATEGY_LABELS[report.strategy] ?? report.strategy}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Записів у прев'ю</span>
            <span className="stat-value">{fmtNumber(report.records_found)}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Сторінок</span>
            <span className="stat-value">
              {report.pages_ok}/{report.pages_tried}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Середня заповненість</span>
            <span className="stat-value">{fmtNumber(Math.round(avgCoverage))}%</span>
          </div>
          {report.estimated_llm_cost_usd > 0 && (
            <div className="stat">
              <span className="stat-label">LLM-витрати</span>
              <span className="stat-value">${report.estimated_llm_cost_usd.toFixed(4)}</span>
            </div>
          )}
        </div>

        {report.warnings.length > 0 && (
          <div className="alert alert--warning">
            <ul className="plain-list">
              {report.warnings.slice(0, 6).map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </div>
        )}

        {report.strategy === 'llm' && (
          <div className="alert alert--warning">
            Дані отримано LLM-екстракцією. Для великих обсягів додайте CSS-селектори: це у 10-50
            разів дешевше.
          </div>
        )}
      </section>

      {report.schema && (
        <section className="card">
          <div className="card-head">
            <h3 className="card-title">Вивчена схема</h3>
            <span className="chip">score {report.schema.score}</span>
          </div>
          <div className="schema-box">
            <span className="muted">container</span>
            <code>{report.schema.container}</code>
            <span className="chip chip--muted">
              {report.schema.item_scope ? 'список' : 'сторінка деталі'}
            </span>
          </div>
          <div className="table-wrap">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Поле</th>
                  <th>Селектор</th>
                  <th>Атрибут</th>
                  <th>Впевненість</th>
                  <th>Покриття</th>
                </tr>
              </thead>
              <tbody>
                {report.schema.fields.map((field) => (
                  <tr key={field.key}>
                    <td>
                      <code className="mono-small">{field.key}</code>
                    </td>
                    <td>
                      <code className="selector">{field.selector}</code>
                    </td>
                    <td>{field.attr ?? <span className="muted">text</span>}</td>
                    <td>{Math.round(field.confidence * 100)}%</td>
                    <td>{report.schema?.coverage[field.key] ?? 0}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {report.schema.notes.length > 0 && (
            <div className="notes">{report.schema.notes.join(' · ')}</div>
          )}
        </section>
      )}

      <section className="card">
        <h3 className="card-title">Тестова вибірка</h3>
        {report.samples.length === 0 && <div className="empty-hint">Немає зразків.</div>}
        <div className="samples">
          {report.samples.map((sample) => (
            <div className="sample" key={sample.url}>
              <div className="sample-head">
                <a className="link" href={sample.url} target="_blank" rel="noreferrer">
                  {sample.url}
                </a>
                <span className="chip">{sample.records.length} записів</span>
              </div>
              {sample.error && <div className="alert alert--warning">{sample.error}</div>}
              <div className="preview-grid">
                {sample.records.slice(0, 2).map((record, index) => (
                  <div className="preview-card" key={index}>
                    {Object.entries(record)
                      .slice(0, 8)
                      .map(([key, value]) => (
                        <div className="preview-row" key={key}>
                          <span className="preview-key">{key}</span>
                          <span className="preview-value">{fmtValue(value)}</span>
                        </div>
                      ))}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
