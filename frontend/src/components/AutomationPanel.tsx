import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, type RiskAck } from '../api'
import { fmtDateTime, fmtNumber, statusClass } from '../format'
import type {
  DeliveryChannel,
  DeliveryMode,
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

const DIGEST_CRON_PRESETS: { label: string; cron: string }[] = [
  { label: '06:00 UTC (09:00 Київ)', cron: '0 6 * * *' },
  { label: '15:00 UTC (18:00 Київ)', cron: '0 15 * * *' },
  { label: 'Будні 06:00 UTC', cron: '0 6 * * 1-5' },
]

const EVENT_TYPE_OPTIONS: { key: string; label: string }[] = [
  { key: 'new_entity', label: 'Нові записи' },
  { key: 'price_drop', label: 'Зниження ціни' },
  { key: 'field_changed', label: 'Зміна полів' },
  { key: 'removed_entity', label: 'Зниклі записи' },
]

function splitCsv(raw: string): string[] {
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function parseFieldFiltersInput(raw: string): Record<string, string[]> {
  const result: Record<string, string[]> = {}
  for (const part of splitCsv(raw)) {
    const eqIndex = part.indexOf('=')
    if (eqIndex <= 0) {
      throw new Error(`Невірний формат фільтра поля «${part}»: очікується ключ=значення1|значення2`)
    }
    const key = part.slice(0, eqIndex).trim()
    const values = part
      .slice(eqIndex + 1)
      .split('|')
      .map((item) => item.trim())
      .filter(Boolean)
    if (!key || values.length === 0) {
      throw new Error(`Порожнє значення у фільтрі поля «${part}»`)
    }
    result[key] = [...(result[key] ?? []), ...values]
  }
  return result
}

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
  const [subExcludeKeywords, setSubExcludeKeywords] = useState('')
  const [subCpvPrefixes, setSubCpvPrefixes] = useState('')
  const [subRegions, setSubRegions] = useState('')
  const [subFieldFilters, setSubFieldFilters] = useState('')
  const [subMinAmount, setSubMinAmount] = useState('')
  const [subMaxAmount, setSubMaxAmount] = useState('')
  const [subDeliveryMode, setSubDeliveryMode] = useState<DeliveryMode>('instant')
  const [subDigestCron, setSubDigestCron] = useState('0 6 * * *')
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
    let parsedFieldFilters: Record<string, string[]> = {}
    try {
      parsedFieldFilters = parseFieldFiltersInput(subFieldFilters)
    } catch (error) {
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Невірний формат фільтрів полів',
      })
      return
    }

    const keywords = splitCsv(subKeywords)
    const excludeKeywords = splitCsv(subExcludeKeywords)
    const cpvPrefixes = splitCsv(subCpvPrefixes)
    const regions = splitCsv(subRegions)

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
          exclude_keywords: excludeKeywords,
          cpv_prefixes: cpvPrefixes,
          regions,
          field_filters: parsedFieldFilters,
          min_amount: minVal,
          max_amount: maxVal,
          notify_on_error: subNotifyError,
          delivery_mode: subDeliveryMode,
          digest_cron: subDigestCron.trim() || '0 6 * * *',
        },
        enabled: subEnabled,
      })
      setSubscriptions((current) => [...current, res.subscription])
      setSubName('')
      setSubTarget('')
      setSubSecret('')
      setSubKeywords('')
      setSubExcludeKeywords('')
      setSubCpvPrefixes('')
      setSubRegions('')
      setSubFieldFilters('')
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

  const flushSub = async (sub: SubscriptionView) => {
    setBusySubId(sub.id)
    try {
      const res = await api.flushSubscription(task.id, sub.id)
      const subsRes = await api.subscriptions(task.id)
      setSubscriptions(subsRes.items)
      onToast({
        kind: res.ok ? 'success' : 'error',
        text: res.ok ? res.detail : `Не вдалося відправити дайджест: ${res.detail}`,
      })
    } catch (error) {
      onToast({
        kind: 'error',
        text: error instanceof Error ? error.message : 'Помилка відправки дайджесту',
      })
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
              const isDigest = sub.filters.delivery_mode === 'digest_daily'
              return (
                <div key={sub.id} className="source-row">
                  <div className="source-head">
                    <div className="card-actions">
                      <span className="chip">{sub.channel}</span>
                      <span className="chip">
                        {isDigest ? `Дайджест · ${sub.filters.digest_cron}` : 'Миттєво'}
                      </span>
                      {sub.pending_digest_count > 0 && (
                        <span className="chip chip--warn">
                          У черзі: {sub.pending_digest_count}
                        </span>
                      )}
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
                  {(isDigest || sub.last_digest_at) && (
                    <div className="mono-small">
                      {sub.next_digest_at && <>Наступний: {fmtDateTime(sub.next_digest_at)}</>}
                      {sub.next_digest_at && sub.last_digest_at && ' · '}
                      {sub.last_digest_at && <>Останній: {fmtDateTime(sub.last_digest_at)}</>}
                    </div>
                  )}
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
                      <span key={`kw-${kw}`} className="chip chip--yes">
                        #{kw}
                      </span>
                    ))}
                    {sub.filters.exclude_keywords.map((kw) => (
                      <span key={`ex-${kw}`} className="chip chip--warn">
                        без: {kw}
                      </span>
                    ))}
                    {sub.filters.cpv_prefixes.map((cpv) => (
                      <span key={`cpv-${cpv}`} className="chip">
                        CPV {cpv}*
                      </span>
                    ))}
                    {sub.filters.regions.map((reg) => (
                      <span key={`reg-${reg}`} className="chip">
                        📍 {reg}
                      </span>
                    ))}
                    {Object.entries(sub.filters.field_filters).map(([fkey, fvals]) => (
                      <span key={`fld-${fkey}`} className="chip">
                        {fkey}={fvals.join('|')}
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
                    {sub.pending_digest_count > 0 && (
                      <button
                        className="btn btn--tiny btn--primary"
                        onClick={() => void flushSub(sub)}
                        disabled={rowBusy}
                      >
                        Надіслати зараз ({sub.pending_digest_count})
                      </button>
                    )}
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
            <label className="field field--small">
              <span className="field-label">Режим</span>
              <select
                className="select"
                value={subDeliveryMode}
                onChange={(event) => setSubDeliveryMode(event.target.value as DeliveryMode)}
              >
                <option value="instant">Миттєво</option>
                <option value="digest_daily">Щоденний дайджест</option>
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

          {subDeliveryMode === 'digest_daily' && (
            <div className="field">
              <span className="field-label">
                Розклад дайджесту (UTC cron)
                <span className="hint">накопичує зміни в чергу й надсилає одним пакетом</span>
              </span>
              <div className="examples">
                {DIGEST_CRON_PRESETS.map((preset) => (
                  <button
                    key={preset.cron}
                    type="button"
                    className={`tiny-btn ${subDigestCron.trim() === preset.cron ? 'tiny-btn--active' : ''}`}
                    onClick={() => setSubDigestCron(preset.cron)}
                  >
                    {preset.label} · <code>{preset.cron}</code>
                  </button>
                ))}
              </div>
              <input
                className="input input--mono"
                value={subDigestCron}
                onChange={(event) => setSubDigestCron(event.target.value)}
                placeholder="0 6 * * *"
              />
            </div>
          )}

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
                placeholder="оренда, ремонт"
              />
            </label>
            <label className="field">
              <span className="field-label">Стоп-слова (виключити)</span>
              <input
                className="input"
                value={subExcludeKeywords}
                onChange={(event) => setSubExcludeKeywords(event.target.value)}
                placeholder="субвенція, ліфт"
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
            <label className="field">
              <span className="field-label">CPV-префікси (через кому)</span>
              <input
                className="input input--mono"
                value={subCpvPrefixes}
                onChange={(event) => setSubCpvPrefixes(event.target.value)}
                placeholder="45, 3019, 3310"
              />
            </label>
            <label className="field">
              <span className="field-label">Регіони / міста (через кому)</span>
              <input
                className="input"
                value={subRegions}
                onChange={(event) => setSubRegions(event.target.value)}
                placeholder="Київ, Львів"
              />
            </label>
            <label className="field">
              <span className="field-label">Фільтри полів (key=val1|val2)</span>
              <input
                className="input input--mono"
                value={subFieldFilters}
                onChange={(event) => setSubFieldFilters(event.target.value)}
                placeholder="status=active*, currency=UAH"
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
