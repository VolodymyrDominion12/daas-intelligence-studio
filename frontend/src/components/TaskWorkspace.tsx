import { useState } from 'react'
import { api } from '../api'
import {
  RUN_LABELS,
  STATUS_LABELS,
  fmtDateTime,
  fmtNumber,
  runSummary,
  statusClass,
  timeAgo,
} from '../format'
import type { ConfigResponse, EventInfo, RunKind, TaskDetail, ToastMessage } from '../types'
import { CalibrationPanel } from './CalibrationPanel'
import { DataGrid } from './DataGrid'
import { DetailDrawer } from './DetailDrawer'
import { EventsPanel } from './EventsPanel'
import { ExportPanel } from './ExportPanel'
import { ChangesPanel, DuplicatesPanel, SourcesPanel } from './MiscPanels'
import { PlanPanel } from './PlanPanel'

type Tab = 'plan' | 'data' | 'calibration' | 'changes' | 'duplicates' | 'exports' | 'sources'

interface Props {
  detail: TaskDetail
  events: EventInfo[]
  config: ConfigResponse | null
  onRefresh: () => Promise<void>
  onToast: (toast: ToastMessage) => void
}

const TABS: { key: Tab; label: string }[] = [
  { key: 'plan', label: 'План' },
  { key: 'data', label: 'Дані' },
  { key: 'calibration', label: 'Калібрування' },
  { key: 'changes', label: 'Зміни' },
  { key: 'duplicates', label: 'Дублікати' },
  { key: 'exports', label: 'Експорт' },
  { key: 'sources', label: 'Джерела' },
]

export function TaskWorkspace({ detail, events, config, onRefresh, onToast }: Props) {
  const [tab, setTab] = useState<Tab>('plan')
  const [busy, setBusy] = useState(false)
  const [recordKey, setRecordKey] = useState<string | null>(null)

  const task = detail.task
  const spec = task.spec
  const dataset = detail.dataset.dataset
  const rows = dataset?.row_count ?? 0
  const activeRun = detail.active_run
  const lastRun = activeRun ?? task.last_run
  // Snapshot runs don't move data; keep collection numbers visible in the strip.
  const dataRun =
    activeRun ??
    detail.runs.find((run) => run.kind === 'collect' || run.kind === 'enrich') ??
    task.last_run
  const changeCount = events.filter((event) =>
    ['new_entity', 'removed_entity', 'field_changed', 'price_drop'].includes(event.type),
  ).length

  const act = async (kind: RunKind) => {
    setBusy(true)
    try {
      if (kind === 'calibrate') await api.calibrate(task.id)
      else if (kind === 'collect') await api.run(task.id)
      else if (kind === 'enrich') await api.enrich(task.id)
      else if (kind === 'snapshot') await api.snapshot(task.id)
      await onRefresh()
      onToast({ kind: 'success', text: `${RUN_LABELS[kind]}: запуск створено` })
      if (kind === 'calibrate') setTab('calibration')
      if (kind === 'collect') setTab('data')
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка запуску' })
    } finally {
      setBusy(false)
    }
  }

  const cancel = async () => {
    if (!activeRun) return
    setBusy(true)
    try {
      await api.cancelRun(activeRun.id)
      await onRefresh()
      onToast({ kind: 'info', text: 'Запуск скасовано' })
    } catch (error) {
      onToast({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка' })
    } finally {
      setBusy(false)
    }
  }

  const refreshKey =
    (dataset?.version ?? 0) * 1000 +
    (activeRun?.stats.records_new ?? 0) +
    (activeRun?.stats.records_updated ?? 0) +
    detail.runs.length

  return (
    <div className="workspace">
      <header className="workspace-head">
        <div className="workspace-title">
          <div className="eyebrow">
            {spec.entity} · {RUN_LABELS[dataRun?.kind ?? 'collect'] ?? 'задача'}
          </div>
          <h1>{task.title}</h1>
          <div className="meta-row">
            <span className={statusClass(task.status)}>{STATUS_LABELS[task.status] ?? task.status}</span>
            <span className="chip">v{dataset?.version ?? 0}</span>
            <span>{fmtNumber(rows)} рядків</span>
            <span className="muted">
              оновлено {timeAgo(detail.dataset.freshness ?? task.updated_at)}
            </span>
          </div>
        </div>

        <div className="actions">
          <button
            className="btn"
            onClick={() => act('calibrate')}
            disabled={busy || Boolean(activeRun) || spec.seeds.length === 0}
            title="Тестова вибірка та навчання CSS-схеми"
          >
            Калібрувати
          </button>
          <button
            className="btn btn--primary"
            onClick={() => act('collect')}
            disabled={busy || Boolean(activeRun) || spec.seeds.length === 0}
            title={detail.calibration ? 'Запустити масовий збір' : 'Спершу запустіть калібрування'}
          >
            Зібрати
          </button>
          <button
            className="btn"
            onClick={() => act('enrich')}
            disabled={busy || Boolean(activeRun) || rows === 0}
            title="Waterfall-збагачення та AI-колонки"
          >
            Збагатити
          </button>
          <button
            className="btn"
            onClick={() => act('snapshot')}
            disabled={busy || Boolean(activeRun) || rows === 0}
            title="Parquet-снапшот і виявлення змін"
          >
            Снапшот
          </button>
          {activeRun && (
            <button className="btn btn--danger" onClick={cancel} disabled={busy}>
              Зупинити
            </button>
          )}
        </div>
      </header>

      <div className="stats-strip">
        <div className="stat-chip">
          <span className="stat-chip-label">Сторінок</span>
          <span className="stat-chip-value">{fmtNumber(dataRun?.stats.pages_fetched ?? 0)}</span>
        </div>
        <div className="stat-chip">
          <span className="stat-chip-label">Нових</span>
          <span className="stat-chip-value">+{fmtNumber(dataRun?.stats.records_new ?? 0)}</span>
        </div>
        <div className="stat-chip">
          <span className="stat-chip-label">Оновлено</span>
          <span className="stat-chip-value">↻{fmtNumber(dataRun?.stats.records_updated ?? 0)}</span>
        </div>
        <div className="stat-chip">
          <span className="stat-chip-label">LLM</span>
          <span className="stat-chip-value">${(dataRun?.spent_usd ?? 0).toFixed(3)}</span>
        </div>
        <div className="stat-chip stat-chip--wide">
          <span className="stat-chip-label">Останній запуск</span>
          <span className="stat-chip-text">{runSummary(lastRun)}</span>
        </div>
        {lastRun?.finished_at && (
          <div className="stat-chip">
            <span className="stat-chip-label">Завершено</span>
            <span className="stat-chip-text">{fmtDateTime(lastRun.finished_at)}</span>
          </div>
        )}
      </div>

      <nav className="tabs">
        {TABS.map((item) => (
          <button
            key={item.key}
            className={`tab ${tab === item.key ? 'tab--active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            {item.label}
            {item.key === 'data' && <span className="tab-count">{fmtNumber(rows)}</span>}
            {item.key === 'changes' && changeCount > 0 && (
              <span className="tab-count">{changeCount}</span>
            )}
          </button>
        ))}
      </nav>

      <div className="workspace-body">
        <main className="panel">
          {tab === 'plan' && (
            <PlanPanel
              task={task}
              quality={detail.dataset.quality}
              enrichers={config?.enrichers ?? []}
              onRefresh={onRefresh}
              onToast={onToast}
            />
          )}
          {tab === 'data' && (
            <DataGrid
              taskId={task.id}
              columns={spec.columns}
              refreshKey={refreshKey}
              onOpenRecord={setRecordKey}
              onGoExport={() => setTab('exports')}
              onToast={onToast}
            />
          )}
          {tab === 'calibration' && (
            <CalibrationPanel
              report={detail.calibration}
              busy={busy || Boolean(activeRun)}
              canRun={spec.seeds.length > 0}
              onCalibrate={() => act('calibrate')}
            />
          )}
          {tab === 'changes' && <ChangesPanel taskId={task.id} refreshKey={refreshKey} />}
          {tab === 'duplicates' && <DuplicatesPanel taskId={task.id} refreshKey={refreshKey} />}
          {tab === 'exports' && (
            <ExportPanel taskId={task.id} rows={rows} onToast={onToast} />
          )}
          {tab === 'sources' && <SourcesPanel onToast={onToast} />}
        </main>

        <aside className="side-panel">
          <EventsPanel events={events} activeRun={activeRun} />
          <section className="card hint-card">
            <h4 className="sub-title">Порада</h4>
            <p>
              Спочатку перевірте 5 рядків у «Калібруванні». Якщо схема правильна — запускайте
              «Зібрати». Далі «Збагатити» додасть контакти й технології, а «Снапшот» зафіксує
              версію для порівняння.
            </p>
          </section>
        </aside>
      </div>

      {recordKey && (
        <DetailDrawer
          taskId={task.id}
          recordKey={recordKey}
          columns={spec.columns}
          onClose={() => setRecordKey(null)}
        />
      )}
    </div>
  )
}
