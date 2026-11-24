import type {
  CalibrationHistoryEntry,
  CalibrationReport,
  SchemaDiffReport,
} from '../types'
import { fmtDateTime, fmtNumber, fmtValue } from '../format'

interface Props {
  report: CalibrationReport | null
  version?: number | null
  history?: CalibrationHistoryEntry[]
  diff?: SchemaDiffReport | null
  busy: boolean
  canRun: boolean
  onCalibrate: () => void
  onRollback?: (version?: number) => Promise<void> | void
}

const STRATEGY_LABELS: Record<string, string> = {
  css: 'CSS-селектори',
  json_ld: 'JSON-LD',
  llm: 'LLM',
  none: 'не визначено',
}

const SOURCE_LABELS: Record<string, string> = {
  calibrate: 'калібрування',
  rollback: 'відкат',
  legacy: 'legacy',
}

function fmtSigned(value: number | null, suffix = ''): string {
  if (value === null || Number.isNaN(value)) return '—'
  if (value > 0) return `+${fmtNumber(value)}${suffix}`
  return `${fmtNumber(value)}${suffix}`
}

export function CalibrationPanel({
  report,
  version,
  history = [],
  diff = null,
  busy,
  canRun,
  onCalibrate,
  onRollback,
}: Props) {
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

  const canRollback = Boolean(onRollback && history.length >= 2)
  const coverageDeltaEntries = diff ? Object.entries(diff.coverage_deltas) : []

  return (
    <div className="stack">
      <section className="card">
        <div className="card-head">
          <div className="card-title">
            <h3>Результат калібрування</h3>
            {version !== undefined && version !== null && <span className="chip">v{version}</span>}
          </div>
          <div className="card-actions">
            {canRollback && (
              <button
                className="btn btn--ghost"
                onClick={() => onRollback?.()}
                disabled={busy}
                title="Повернути попередню версію схеми та оновити selector_cache"
              >
                Відкотити схему
              </button>
            )}
            <button className="btn btn--ghost" onClick={onCalibrate} disabled={busy || !canRun}>
              Перекалібрувати
            </button>
          </div>
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

      {diff && (
        <section className="card">
          <div className="card-head">
            <h3 className="card-title">Schema Diff</h3>
            {diff.has_previous ? (
              <span className="chip">
                v{diff.previous_version} → v{diff.current_version}
              </span>
            ) : (
              <span className="chip chip--muted">v{diff.current_version} · перша версія</span>
            )}
          </div>

          {!diff.has_previous ? (
            <div className="empty-hint">
              Це перша збережена версія калібрування. Після повторного калібрування тут зʼявиться
              порівняння селекторів, контейнера та покриття.
            </div>
          ) : (
            <div className="stack">
              <div className="diff-summary">
                <div className="stat">
                  <span className="stat-label">Стратегія</span>
                  <span className="stat-value">
                    {diff.strategy_before ?? '—'} → {diff.strategy_after}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Контейнер</span>
                  <span className="stat-value">
                    {diff.container_changed ? 'змінився' : 'без змін'}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat-label">Дельта записів</span>
                  <span className="stat-value">{fmtSigned(diff.records_delta)}</span>
                </div>
                <div className="stat">
                  <span className="stat-label">Дельта score</span>
                  <span className="stat-value">{fmtSigned(diff.score_delta)}</span>
                </div>
              </div>

              {diff.container_changed && (
                <div className="schema-box">
                  <span className="muted">container</span>
                  <code className="diff-before">{diff.container_before ?? '—'}</code>
                  <span>→</span>
                  <code className="diff-after">{diff.container_after ?? '—'}</code>
                </div>
              )}

              {!diff.has_changes && (
                <div className="empty-hint">
                  Схема та покриття не змінилися порівняно з v{diff.previous_version}.
                </div>
              )}

              {diff.fields_added.length > 0 && (
                <div>
                  <h4 className="sub-title">Додані поля ({diff.fields_added.length})</h4>
                  <div className="enrich-fields">
                    {diff.fields_added.map((field) => (
                      <span className="chip chip--yes" key={field.key}>
                        + {field.key}: {field.selector}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {diff.fields_removed.length > 0 && (
                <div>
                  <h4 className="sub-title">Видалені поля ({diff.fields_removed.length})</h4>
                  <div className="enrich-fields">
                    {diff.fields_removed.map((field) => (
                      <span className="chip chip--warn" key={field.key}>
                        − {field.key}: {field.selector}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {diff.fields_changed.length > 0 && (
                <div className="table-wrap">
                  <table className="table table--compact">
                    <thead>
                      <tr>
                        <th>Поле</th>
                        <th>Було</th>
                        <th>Стало</th>
                        <th>Покриття</th>
                      </tr>
                    </thead>
                    <tbody>
                      {diff.fields_changed.map((item) => (
                        <tr key={item.key}>
                          <td>
                            <code className="mono-small">{item.key}</code>
                          </td>
                          <td>
                            <code className="selector diff-before">
                              {item.selector_before ?? '—'}
                              {item.attr_before ? ` @${item.attr_before}` : ''}
                            </code>
                          </td>
                          <td>
                            <code className="selector diff-after">
                              {item.selector_after ?? '—'}
                              {item.attr_after ? ` @${item.attr_after}` : ''}
                            </code>
                          </td>
                          <td>
                            {item.coverage_before ?? '—'}% → {item.coverage_after ?? '—'}%
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {coverageDeltaEntries.length > 0 && (
                <div>
                  <h4 className="sub-title">Зміна покриття</h4>
                  <div className="enrich-fields">
                    {coverageDeltaEntries.map(([key, delta]) => (
                      <span
                        className={`chip ${delta >= 0 ? 'chip--yes' : 'chip--warn'}`}
                        key={key}
                      >
                        {key}: {fmtSigned(delta, '%')}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      )}

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

      {history.length > 0 && (
        <section className="card">
          <div className="card-head">
            <h3 className="card-title">Історія версій калібрування</h3>
            <span className="chip">{history.length}</span>
          </div>
          <div className="table-wrap">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Версія</th>
                  <th>Джерело</th>
                  <th>Стратегія</th>
                  <th>Контейнер</th>
                  <th>Записів</th>
                  <th>Коли</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {history.map((entry, index) => {
                  const isCurrent = index === 0 || entry.version === version
                  return (
                    <tr key={entry.version}>
                      <td>
                        <code className="mono-small">v{entry.version}</code>{' '}
                        {isCurrent && <span className="chip chip--yes">активна</span>}
                      </td>
                      <td>
                        <span className="chip">{SOURCE_LABELS[entry.source] ?? entry.source}</span>
                      </td>
                      <td>{STRATEGY_LABELS[entry.strategy] ?? entry.strategy}</td>
                      <td>
                        {entry.container ? (
                          <code className="selector">{entry.container}</code>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      <td>{fmtNumber(entry.records_found)}</td>
                      <td className="muted">{fmtDateTime(entry.created_at)}</td>
                      <td className="td-actions">
                        {!isCurrent && onRollback && (
                          <button
                            className="tiny-btn"
                            disabled={busy}
                            onClick={() => onRollback(entry.version)}
                          >
                            відкотити
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
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
