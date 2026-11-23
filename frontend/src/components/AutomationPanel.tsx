import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, type RiskAck } from '../api'
import { fmtDateTime, fmtNumber, statusClass } from '../format'
import type {
  DeliveryChannel,
  EvalReport,
  QualityChecksReport,
  ScheduleInfo,
  SubscriptionView,
  TaskInfo,
  ToastMessage,
} from '../types'

interface Props {
  task: TaskInfo
  schedulerEnabled: boolean
  qualityChecks?: QualityChecksReport
  evalReport?: EvalReport
  onRefresh: () => Promise<void>
  onToast: (toast: ToastMessage) => void
}

const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: 'Кожні 15 хв', cron: '*/15 * * * *' },
  { label: 'Щогодини', cron: '0 * * * *' },
  { label: 'Щодня 06:00 UTC', cron: '0 6 * * *' },
  { label: 'Щотижня (Пн 06:00)', cron: '0 6 * * 1' },
]

const EVENT_TYPE_OPTIONS: { key: string; label: string }[] = [
  { key: 'new_entity', label: 'Нові записи' },
  { key: 'price_drop', label: 'Зниження ціни' },
  { key: 'field_changed', label: 'Зміна полів' },
  { key: 'removed_entity', label: 'Зниклі записи' },
]

export function AutomationPanel({
  task,
  schedulerEnabled,
  qualityChecks,
  evalReport,
  onRefresh,
  onToast,
}: Props) {
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Schedule state
  const [schedule, setSchedule] = useState<ScheduleInfo | null>(null)
  const [cron, setCron] = useState('0 * * * *')
  const [schedEnabled, setSchedEnabled] = useState(true)
  const [catchUp, setCatchUp] = useState(true)
  const [savingSchedule, setSavingSchedule] = useState(false)

  // Subscriptions state
  const [subscriptions, setSubscriptions] = useState<SubscriptionView[]>([])
  const [subChannel, setSubChannel] = useState<DeliveryChannel>('telegram')
  const [subName, setSubName] = useState('')
  const [subTarget, setSubTarget] = useState('')
  const [subSecret, setSubSecret] = useState('')
  const [subEventTypes, setSubEventTypes] = useState<string[]>(['new_entity', 'price_drop'])
  const [subKeywords, setSubKeywords] = useState('')
  const [subMinAmount, setSubMinAmount] = useState('')
  const [subMaxAmount, setSubMaxAmount] = useState('')
  const [subNotifyError, setSubNotifyError] = useState(true)
  const [subEnabled, setSubEnabled] = useState(true)
  const [creatingSub, setCreatingSub] = useState(false)
  const [busySubId, setBusySubId] = useState<string | null>(null)

  // Gold eval state
  const [goldPreset, setGoldPreset] = useState(
    task.spec.extraction === 'api_json' ? 'prozorro_gold' : 'clinics_gold',
  )
  const [minF1, setMinF1] = useState('0.8')
  const [runningEval, setRunningEval] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [schedRes, subsRes] = await Promise.all([
        api.schedule(task.id),
        api.subscriptions(task.id),
      ])
      setSchedule(schedRes.schedule)
      if (schedRes.schedule) {
        setCron(schedRes.schedule.cron)
        setSchedEnabled(schedRes.schedule.enabled)
        setCatchUp(schedRes.schedule.catch_up)
      } else {
        setCron('0 * * * *')
        setSchedEnabled(true)
        setCatchUp(true)
      }
      setSubscriptions(subsRes.items)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Не вдалося завантажити налаштування')
    } finally {
      setLoading(false)
    }
  }, [task.id])

  useEffect(() => {
    void load()
  }, [load])

  const saveSchedule = async (ack: RiskAck = {}) => {
    if (!cron.trim()) {
      onToast({ kind: 'error', text: 'Вкажіть cron-вираз (наприклад, 0 * * * *)' })
      return
    }
    setSavingSchedule(true)
    try {
      const res = await api.setSchedule(task.id, {
        cron: cron.trim(),
        enabled: schedEnabled,
        catch_up: catchUp,
        acknowledge_high_risk: ack.acknowledge_high_risk,
        reason: ack.reason,
      })
      setSchedule(res.schedule)
      onToast({ kind: 'success', text: 'Розклад запусків збережено' })
    } catch (error) {
      if (error instanceof ApiError && error.status === 428 && !ack.acknowledge_high_risk) {
        const reason = window.prompt(`${error.message}\n\nПричина автозапуску:`)?.trim()
        setSavingSchedule(false)
        if (reason) await saveSchedule({ acknowledge_high_risk: true, reason })
        return
      }
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Не вдалося зберегти розклад',
      })
    } finally {
      setSavingSchedule(false)
    }
  }

  const removeSchedule = async () => {
    setSavingSchedule(true)
    try {
      await api.deleteSchedule(task.id)
      setSchedule(null)
      setCron('0 * * * *')
      setSchedEnabled(true)
      setCatchUp(true)
      onToast({ kind: 'info', text: 'Розклад видалено' })
    } catch (error) {
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Не вдалося видалити розклад',
      })
    } finally {
      setSavingSchedule(false)
    }
  }

  const toggleEventType = (key: string) => {
    setSubEventTypes((current) =>
      current.includes(key) ? current.filter((item) => item !== key) : [...current, key],
    )
  }

  const createSubscription = async () => {
    if (!subTarget.trim()) {
      onToast({
        kind: 'error',
        text:
          subChannel === 'telegram'
            ? 'Вкажіть Telegram Chat ID або @channel'
            : 'Вкажіть HTTPS URL webhook-отримувача',
      })
      return
    }
    const minVal = subMinAmount.trim() ? Number(subMinAmount) : null
    const maxVal = subMaxAmount.trim() ? Number(subMaxAmount) : null
    if ((minVal !== null && Number.isNaN(minVal)) || (maxVal !== null && Number.isNaN(maxVal))) {
      onToast({ kind: 'error', text: 'Сума у фільтрі має бути числом' })
      return
    }
    if (minVal !== null && maxVal !== null && minVal > maxVal) {
      onToast({ kind: 'error', text: 'Мінімальна сума не може перевищувати максимальну' })
      return
    }
    const keywords = subKeywords
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)

    setCreatingSub(true)
    try {
      const res = await api.createSubscription(task.id, {
        name: subName.trim() || undefined,
        channel: subChannel,
        target: subTarget.trim(),
        secret: subChannel === 'webhook' && subSecret ? subSecret : undefined,
        filters: {
          event_types: subEventTypes,
          keywords,
          min_amount: minVal,
          max_amount: maxVal,
          notify_on_error: subNotifyError,
        },
        enabled: subEnabled,
      })
      setSubscriptions((current) => [...current, res.subscription])
      setSubName('')
      setSubTarget('')
      setSubSecret('')
      setSubKeywords('')
      setSubMinAmount('')
      setSubMaxAmount('')
      onToast({ kind: 'success', text: 'Підписку доставки створено' })
    } catch (error) {
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Не вдалося створити підписку',
      })
    } finally {
      setCreatingSub(false)
    }
  }

  const toggleSubscription = async (sub: SubscriptionView) => {
    setBusySubId(sub.id)
    try {
      const res = await api.updateSubscription(task.id, sub.id, { enabled: !sub.enabled })
      setSubscriptions((current) =>
        current.map((item) => (item.id === sub.id ? res.subscription : item)),
      )
      onToast({
        kind: 'info',
        text: res.subscription.enabled ? 'Підписку увімкнено' : 'Підписку призупинено',
      })
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка оновлення' })
    } finally {
      setBusySubId(null)
    }
  }

  const testSub = async (sub: SubscriptionView) => {
    setBusySubId(sub.id)
    try {
      const res = await api.testSubscription(task.id, sub.id)
      onToast({
        kind: res.ok ? 'success' : 'error',
        text: res.ok ? `Тест доставлено: ${res.detail}` : `Тест не пройшов: ${res.detail}`,
      })
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка тесту' })
    } finally {
      setBusySubId(null)
    }
  }

  const removeSub = async (sub: SubscriptionView) => {
    setBusySubId(sub.id)
    try {
      await api.deleteSubscription(task.id, sub.id)
      setSubscriptions((current) => current.filter((item) => item.id !== sub.id))
      onToast({ kind: 'info', text: 'Підписку видалено' })
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка видалення' })
    } finally {
      setBusySubId(null)
    }
  }

  const runEval = async () => {
    const preset = goldPreset.trim()
    if (!preset) {
      onToast({ kind: 'error', text: 'Вкажіть назву офлайн gold-пресету' })
      return
    }
    const parsedF1 = Number(minF1)
    if (Number.isNaN(parsedF1) || parsedF1 < 0 || parsedF1 > 1) {
      onToast({ kind: 'error', text: 'Поріг F1 має бути в діапазоні від 0 до 1' })
      return
    }
    setRunningEval(true)
    try {
      const res = await api.evalTask(task.id, { gold_preset: preset, min_f1: parsedF1 })
      await onRefresh()
      onToast({
        kind: res.report.passed ? 'success' : 'error',
        text: `Gold eval (${res.report.gold_name}): F1=${Math.round(res.report.f1 * 100)}% (${
          res.report.passed ? 'пройдено' : 'нижче порогу'
        })`,
      })
    } catch (error) {
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Не вдалося виконати оцінку',
      })
    } finally {
      setRunningEval(false)
    }
  }

  if (loading) {
    return (
      <section className="card">
        <div className="spinner" />
      </section>
    )
  }

  if (loadError) {
    return (
      <section className="card">
        <div className="alert alert--error">{loadError}</div>
        <div className="form-actions">
          <button className="btn" onClick={() => void load()}>
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
          <h3 className="card-title">Автоматичний розклад (UTC cron)</h3>
          {schedule ? (
            <span className={statusClass(schedule.enabled ? 'ready' : 'cancelled')}>
              {schedule.enabled ? 'увімкнено' : 'пауза'}
            </span>
          ) : (
            <span className="chip">не налаштовано</span>
          )}
        </div>

        {!schedulerEnabled && (
          <div className="alert alert--warning">
            Планувальник на сервері вимкнено (<code>DAAS_SCHEDULER_ENABLED=false</code>). Розклад
            можна налаштувати зараз, але фонові запуски стартуватимуть лише після увімкнення.
          </div>
        )}

        <p className="lede">
          Планувальник періодично запускає збір у фоні (час завжди в UTC) і пропускає запуск, якщо
          попередній ще виконується.
        </p>

        {schedule ? (
          <div className="stats-grid">
            <div className="stat">
              <span className="stat-label">Cron (UTC)</span>
              <span className="stat-value">{schedule.cron}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Наступний запуск</span>
              <span className="stat-chip-text">{fmtDateTime(schedule.next_run_at)}</span>
            </div>
            <div className="stat">
              <span className="stat-label">Останній запуск</span>
              <span className="stat-chip-text">{fmtDateTime(schedule.last_run_at)}</span>
            </div>
          </div>
        ) : (
          <div className="empty-hint">
            Розклад ще не задано — оберіть інтервал нижче та збережіть, щоб задача оновлювалась
            автоматично.
          </div>
        )}

        <div className="examples">
          {CRON_PRESETS.map((preset) => (
            <button
              key={preset.cron}
              type="button"
              className={`tiny-btn ${cron.trim() === preset.cron ? 'tiny-btn--active' : ''}`}
              onClick={() => setCron(preset.cron)}
            >
              {preset.label} · <code>{preset.cron}</code>
            </button>
          ))}
        </div>

        <div className="form-row form-row--baseline">
          <label className="field">
            <span className="field-label">Cron-вираз (5 полів UTC або @hourly/@daily/@weekly)</span>
            <input
              className="input input--mono"
              value={cron}
              onChange={(event) => setCron(event.target.value)}
              placeholder="0 * * * *"
            />
          </label>
        </div>

        <div className="form-row">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={schedEnabled}
              onChange={(event) => setSchedEnabled(event.target.checked)}
            />
            <span>Розклад увімкнено</span>
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={catchUp}
              onChange={(event) => setCatchUp(event.target.checked)}
            />
            <span>Доганяючий запуск (1 прогін після простою)</span>
          </label>
        </div>

        <div className="form-actions">
          <button
            className="btn btn--primary"
            onClick={() => void saveSchedule()}
            disabled={savingSchedule}
          >
            {savingSchedule ? 'Зберігаю…' : 'Зберегти розклад'}
          </button>
          {schedule && (
            <button className="btn btn--danger" onClick={removeSchedule} disabled={savingSchedule}>
              Видалити розклад
            </button>
          )}
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Доставка дельти та алерти</h3>
          <span className="chip">{subscriptions.length} підписок</span>
        </div>

        <p className="lede">
          Після кожного збору з дельтою релевантні зміни відправляються у Telegram або на Webhook.
          Поля <code>pii=&quot;personal&quot;</code> завжди приховуються перед доставкою.
        </p>

        {subscriptions.length === 0 ? (
          <div className="empty-hint">
            Підписок ще немає — додайте Telegram-канал або Webhook нижче, щоб отримувати нові лоти
            й зниження цін.
          </div>
        ) : (
          <div className="stack">
            {subscriptions.map((sub) => {
              const rowBusy = busySubId === sub.id
              return (
                <div key={sub.id} className="source-row">
                  <div className="source-head">
                    <div className="card-actions">
                      <span className="chip">{sub.channel}</span>
                      <strong>{sub.name || sub.target}</strong>
                    </div>
                    <div className="card-actions">
                      {sub.has_secret && <span className="chip chip--yes">HMAC підпис</span>}
                      <span className={statusClass(sub.enabled ? 'ready' : 'cancelled')}>
                        {sub.enabled ? 'активна' : 'пауза'}
                      </span>
                    </div>
                  </div>
                  <div className="mono-small">{sub.target}</div>
                  <div className="enrich-fields">
                    {sub.filters.event_types.length > 0 ? (
                      sub.filters.event_types.map((ev) => (
                        <span key={ev} className="chip">
                          {ev}
                        </span>
                      ))
                    ) : (
                      <span className="chip">усі зміни</span>
                    )}
                    {sub.filters.keywords.map((kw) => (
                      <span key={kw} className="chip chip--yes">
                        #{kw}
                      </span>
                    ))}
                    {sub.filters.min_amount !== null && (
                      <span className="chip">від {fmtNumber(sub.filters.min_amount)}</span>
                    )}
                    {sub.filters.max_amount !== null && (
                      <span className="chip">до {fmtNumber(sub.filters.max_amount)}</span>
                    )}
                    {sub.filters.notify_on_error && (
                      <span className="chip chip--warn">алерти збоїв</span>
                    )}
                  </div>
                  <div className="form-actions">
                    <button
                      className="btn btn--tiny"
                      onClick={() => void toggleSubscription(sub)}
                      disabled={rowBusy}
                    >
                      {sub.enabled ? 'Призупинити' : 'Увімкнути'}
                    </button>
                    <button
                      className="btn btn--tiny"
                      onClick={() => void testSub(sub)}
                      disabled={rowBusy}
                    >
                      Тест
                    </button>
                    <button
                      className="btn btn--tiny btn--danger"
                      onClick={() => void removeSub(sub)}
                      disabled={rowBusy}
                    >
                      Видалити
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <div className="divider" />
        <h4 className="sub-title">Нова підписка доставки</h4>

        <div className="ai-form">
          <div className="form-row">
            <label className="field field--small">
              <span className="field-label">Канал</span>
              <select
                className="select"
                value={subChannel}
                onChange={(event) => setSubChannel(event.target.value as DeliveryChannel)}
              >
                <option value="telegram">Telegram</option>
                <option value="webhook">Webhook</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">Назва (опційно)</span>
              <input
                className="input"
                value={subName}
                onChange={(event) => setSubName(event.target.value)}
                placeholder="Алерти оренди / CRM"
              />
            </label>
            <label className="field">
              <span className="field-label">
                {subChannel === 'telegram' ? 'Chat ID або @channel' : 'HTTPS Webhook URL'}
              </span>
              <input
                className="input input--mono"
                value={subTarget}
                onChange={(event) => setSubTarget(event.target.value)}
                placeholder={
                  subChannel === 'telegram'
                    ? '-1001234567890 або @my_channel'
                    : 'https://hooks.example.com/daas'
                }
              />
            </label>
          </div>

          {subChannel === 'webhook' && (
            <label className="field">
              <span className="field-label">
                HMAC-SHA256 секрет (опційно)
                <span className="hint">підписує заголовок X-Daas-Signature</span>
              </span>
              <input
                className="input input--mono"
                type="password"
                value={subSecret}
                onChange={(event) => setSubSecret(event.target.value)}
                placeholder="секретний ключ підпису"
                autoComplete="new-password"
              />
            </label>
          )}

          <div className="field">
            <span className="field-label">Типи подій для доставки</span>
            <div className="form-row">
              {EVENT_TYPE_OPTIONS.map((item) => (
                <label key={item.key} className="checkbox">
                  <input
                    type="checkbox"
                    checked={subEventTypes.includes(item.key)}
                    onChange={() => toggleEventType(item.key)}
                  />
                  <span>{item.label}</span>
                </label>
              ))}
            </div>
          </div>

          <div className="form-row">
            <label className="field">
              <span className="field-label">Ключові слова (через кому)</span>
              <input
                className="input"
                value={subKeywords}
                onChange={(event) => setSubKeywords(event.target.value)}
                placeholder="оренда, Київ, 45000000"
              />
            </label>
            <label className="field field--small">
              <span className="field-label">Мін. сума</span>
              <input
                className="input"
                type="number"
                value={subMinAmount}
                onChange={(event) => setSubMinAmount(event.target.value)}
                placeholder="100000"
              />
            </label>
            <label className="field field--small">
              <span className="field-label">Макс. сума</span>
              <input
                className="input"
                type="number"
                value={subMaxAmount}
                onChange={(event) => setSubMaxAmount(event.target.value)}
                placeholder="5000000"
              />
            </label>
          </div>

          <div className="form-row">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={subNotifyError}
                onChange={(event) => setSubNotifyError(event.target.checked)}
              />
              <span>Службові алерти (збій, 0 записів, якість)</span>
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={subEnabled}
                onChange={(event) => setSubEnabled(event.target.checked)}
              />
              <span>Активна одразу після створення</span>
            </label>
          </div>

          <div className="form-actions">
            <button
              className="btn btn--primary"
              onClick={() => void createSubscription()}
              disabled={creatingSub}
            >
              {creatingSub ? 'Додаю…' : 'Додати підписку'}
            </button>
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h3 className="card-title">Контроль якості та офлайн Gold-set Eval</h3>
          {qualityChecks && (
            <span className={statusClass(qualityChecks.passed ? 'ready' : 'failed')}>
              {qualityChecks.passed ? 'якість ОК' : `${qualityChecks.issues.length} попередж.`}
            </span>
          )}
        </div>

        {qualityChecks ? (
          <>
            <div className="stats-grid">
              <div className="stat">
                <span className="stat-label">Рядків у версії</span>
                <span className="stat-value">{fmtNumber(qualityChecks.row_count)}</span>
              </div>
              {qualityChecks.observed_count !== null && (
                <div className="stat">
                  <span className="stat-label">Спостережено</span>
                  <span className="stat-value">{fmtNumber(qualityChecks.observed_count)}</span>
                </div>
              )}
              {qualityChecks.drop_ratio !== null && (
                <div className="stat">
                  <span className="stat-label">Падіння обсягу</span>
                  <span className="stat-value">{Math.round(qualityChecks.drop_ratio * 100)}%</span>
                </div>
              )}
            </div>
            {qualityChecks.issues.length > 0 ? (
              <div className="stack">
                {qualityChecks.issues.map((issue, idx) => (
                  <div key={idx} className="alert alert--warning">
                    <strong>{issue.code}</strong>: {issue.message}
                  </div>
                ))}
              </div>
            ) : (
              <p className="lede">Усі післязбірні перевірки якості останньої версії пройдено.</p>
            )}
          </>
        ) : (
          <div className="empty-hint">
            Післязбірні перевірки з’являться після першого завершеного збору даних.
          </div>
        )}

        <div className="divider" />
        <h4 className="sub-title">Оцінка точності (Gold-set Eval)</h4>

        {evalReport && (
          <div className="stats-grid">
            <div className="stat">
              <span className="stat-label">Набір</span>
              <span className="stat-chip-text">{evalReport.gold_name}</span>
            </div>
            <div className="stat">
              <span className="stat-label">F1</span>
              <span className="stat-value">{Math.round(evalReport.f1 * 100)}%</span>
            </div>
            <div className="stat">
              <span className="stat-label">Precision / Recall</span>
              <span className="stat-value">
                {Math.round(evalReport.precision * 100)}% / {Math.round(evalReport.recall * 100)}%
              </span>
            </div>
            <div className="stat">
              <span className="stat-label">LLM fallback</span>
              <span className="stat-value">{evalReport.llm_fallback_pct.toFixed(1)}%</span>
            </div>
            <div className="stat">
              <span className="stat-label">$ / 1k записів</span>
              <span className="stat-value">${evalReport.usd_per_1k_records.toFixed(3)}</span>
            </div>
          </div>
        )}

        <div className="form-row form-row--baseline">
          <label className="field">
            <span className="field-label">Назва офлайн пресету</span>
            <input
              className="input input--mono"
              list="daas-gold-presets"
              value={goldPreset}
              onChange={(event) => setGoldPreset(event.target.value)}
              placeholder="prozorro_gold"
            />
            <datalist id="daas-gold-presets">
              <option value="prozorro_gold" />
              <option value="clinics_gold" />
            </datalist>
          </label>
          <label className="field field--small">
            <span className="field-label">Мін. F1</span>
            <input
              className="input"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={minF1}
              onChange={(event) => setMinF1(event.target.value)}
            />
          </label>
          <button className="btn" onClick={() => void runEval()} disabled={runningEval}>
            {runningEval ? 'Оцінюю…' : 'Запустити Gold eval'}
          </button>
        </div>
      </section>
    </div>
  )
}
