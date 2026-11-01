import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, eventStreamUrl } from './api'
import { NewTask } from './components/NewTask'
import { Sidebar } from './components/Sidebar'
import { TaskWorkspace } from './components/TaskWorkspace'
import type {
  ConfigResponse,
  EventInfo,
  TaskDetail,
  TaskInfo,
  ToastMessage,
} from './types'

function mergeEvents(previous: EventInfo[], incoming: EventInfo[]): EventInfo[] {
  const map = new Map<number, EventInfo>()
  for (const event of previous) map.set(event.id, event)
  for (const event of incoming) map.set(event.id, event)
  return [...map.values()].sort((a, b) => a.id - b.id).slice(-500)
}

export function App() {
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [tasks, setTasks] = useState<TaskInfo[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [events, setEvents] = useState<EventInfo[]>([])
  const [showNew, setShowNew] = useState(false)
  const [creating, setCreating] = useState(false)
  const [booting, setBooting] = useState(true)
  const [fatal, setFatal] = useState<string | null>(null)
  const [toast, setToast] = useState<(ToastMessage & { id: number }) | null>(null)

  const notify = useCallback((message: ToastMessage) => {
    setToast({ ...message, id: Date.now() })
  }, [])

  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(null), 6500)
    return () => clearTimeout(timer)
  }, [toast])

  const bootstrap = useCallback(async () => {
    try {
      const [configResponse, taskList] = await Promise.all([api.config(), api.tasks()])
      setConfig(configResponse)
      setTasks(taskList.items)
      setSelectedId((current) => current ?? taskList.items[0]?.id ?? null)
      setFatal(null)
    } catch (error) {
      const message =
        error instanceof ApiError && error.status === 401
          ? 'API відхилив токен. Відкрийте сторінку через `daas serve` або перезапустіть dev-сервер, щоб підхопити storage/runtime.json.'
          : error instanceof Error
            ? error.message
            : 'Невідома помилка'
      setFatal(message)
    } finally {
      setBooting(false)
    }
  }, [])

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  const refreshAll = useCallback(async () => {
    const taskList = await api.tasks()
    setTasks(taskList.items)
    if (selectedId) {
      setDetail(await api.task(selectedId))
    }
  }, [selectedId])

  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let alive = true
    setDetail(null)
    api
      .task(selectedId)
      .then((response) => {
        if (alive) setDetail(response)
      })
      .catch((error: unknown) => {
        if (alive) notify({ kind: 'error', text: error instanceof Error ? error.message : 'Помилка' })
      })
    return () => {
      alive = false
    }
  }, [selectedId, notify])

  // Light polling keeps the workspace in sync with background runs.
  useEffect(() => {
    if (!selectedId) return
    const timer = setInterval(() => {
      refreshAll().catch(() => undefined)
    }, 2600)
    return () => clearInterval(timer)
  }, [selectedId, refreshAll])

  // Live event stream (falls back to the polling refresh above if SSE is unavailable).
  useEffect(() => {
    if (!selectedId) {
      setEvents([])
      return
    }
    setEvents([])
    let alive = true
    api
      .events(selectedId)
      .then((response) => {
        if (alive) setEvents((current) => mergeEvents(current, response.items))
      })
      .catch(() => undefined)

    let source: EventSource | null = null
    try {
      source = new EventSource(eventStreamUrl(selectedId))
      source.onmessage = (event) => {
        try {
          const parsed = JSON.parse(event.data) as EventInfo
          setEvents((current) => mergeEvents(current, [parsed]))
        } catch {
          /* keep-alive or malformed frame */
        }
      }
    } catch {
      source = null
    }
    return () => {
      alive = false
      source?.close()
    }
  }, [selectedId])

  const createTask = async (payload: { prompt: string; seeds: string[]; title?: string }) => {
    setCreating(true)
    try {
      const response = await api.createTask(payload)
      setShowNew(false)
      const taskList = await api.tasks()
      setTasks(taskList.items)
      setSelectedId(response.task.id)
      notify({
        kind: 'success',
        text: 'Схему згенеровано. Перевірте колонки й запустіть калібрування.',
      })
    } finally {
      setCreating(false)
    }
  }

  if (booting) {
    return (
      <div className="boot">
        <div className="boot-mark">D</div>
        <div className="spinner" />
        <p className="muted">Запускаю локальну студію…</p>
      </div>
    )
  }

  if (fatal) {
    return (
      <div className="boot">
        <div className="boot-mark boot-mark--error">!</div>
        <h1>Немає зв'язку з API</h1>
        <p className="muted">{fatal}</p>
        <div className="boot-hints">
          <code>cd backend && uv run daas serve</code>
          <code>storage/runtime.json → api_token</code>
        </div>
        <button className="btn btn--primary" onClick={() => void bootstrap()}>
          Спробувати ще раз
        </button>
      </div>
    )
  }

  return (
    <div className="app">
      <Sidebar
        tasks={tasks}
        selectedId={selectedId}
        config={config}
        onSelect={setSelectedId}
        onNew={() => setShowNew(true)}
      />

      <main className="main">
        {detail ? (
          <TaskWorkspace
            key={detail.task.id}
            detail={detail}
            events={events}
            config={config}
            onRefresh={refreshAll}
            onToast={notify}
          />
        ) : (
          <Welcome hasTasks={tasks.length > 0} config={config} onNew={() => setShowNew(true)} />
        )}
      </main>

      <NewTask
        open={showNew}
        busy={creating}
        onClose={() => setShowNew(false)}
        onCreate={createTask}
      />

      {toast && (
        <div className={`toast toast--${toast.kind}`} role="status" onClick={() => setToast(null)}>
          {toast.text}
        </div>
      )}
    </div>
  )
}

function Welcome({
  hasTasks,
  config,
  onNew,
}: {
  hasTasks: boolean
  config: ConfigResponse | null
  onNew: () => void
}) {
  const caps = config?.capabilities
  return (
    <div className="welcome">
      <div className="welcome-inner">
        <div className="eyebrow">Local-first · AI data studio</div>
        <h1>
          Від бізнес-питання
          <br />
          до дата-продукту
        </h1>
        <p className="welcome-lede">
          Опишіть, які дані потрібні. Студія згенерує схему, навчиться парсити джерело
          детермінованими селекторами, збагатить записи й зафіксує історію змін — усе локально,
          з контролем витрат на LLM.
        </p>
        <div className="welcome-actions">
          <button className="btn btn--primary btn--lg" onClick={onNew}>
            Створити першу задачу
          </button>
          {hasTasks && <span className="muted">або оберіть задачу зліва</span>}
        </div>

        <div className="steps">
          <div className="step">
            <span className="step-num">01</span>
            <h3>План і схема</h3>
            <p>Промпт → колонки з типами, PII-мітками та джерелами.</p>
          </div>
          <div className="step">
            <span className="step-num">02</span>
            <h3>Калібрування</h3>
            <p>2-4 сторінки → CSS-схема. LLM платить один раз, не за кожну сторінку.</p>
          </div>
          <div className="step">
            <span className="step-num">03</span>
            <h3>Збір і збагачення</h3>
            <p>Черга з lease, robots.txt, контакти, технології, AI-колонки.</p>
          </div>
          <div className="step">
            <span className="step-num">04</span>
            <h3>Історія та експорт</h3>
            <p>Parquet-снапшоти, дельта цін, XLSX/CSV з маніфестом і ліцензією.</p>
          </div>
        </div>

        <div className="cap-row">
          <span className={`cap ${caps?.llm_ready ? 'cap--ok' : 'cap--warn'}`}>
            {caps?.llm_ready ? 'LLM підключено' : 'LLM офлайн · локальні шаблони'}
          </span>
          <span className="cap">robots.txt: {caps?.respect_robots ? 'так' : 'вимкнено'}</span>
          <span className="cap">{caps?.enrichers.length ?? 0} збагачувачів</span>
          <span className="cap">fetch: {caps?.fetch_engine ?? 'http'}</span>
          {caps?.offline && <span className="cap cap--warn">offline режим</span>}
        </div>
      </div>
    </div>
  )
}
