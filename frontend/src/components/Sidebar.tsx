import type { ConfigResponse, TaskInfo } from '../types'
import { fmtNumber, statusClass, timeAgo } from '../format'

interface Props {
  tasks: TaskInfo[]
  selectedId: string | null
  config: ConfigResponse | null
  onSelect: (id: string) => void
  onNew: () => void
}

export function Sidebar({ tasks, selectedId, config, onSelect, onNew }: Props) {
  const caps = config?.capabilities
  const overview = config?.overview
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">D</div>
        <div>
          <div className="brand-name">DaaS Studio</div>
          <div className="brand-sub">intelligence workspace</div>
        </div>
        <span className="version">v{caps?.version ?? '—'}</span>
      </div>

      <button className="btn btn--primary btn--block" onClick={onNew}>
        <span className="plus">+</span> Нова задача
      </button>

      <div className="sidebar-section">
        <div className="sidebar-label">
          Задачі <span className="count">{tasks.length}</span>
        </div>
        <div className="task-list">
          {tasks.length === 0 && (
            <div className="empty-hint">
              Ще немає задач.
              <br />
              Опишіть, які дані потрібні, — студія збере схему.
            </div>
          )}
          {tasks.map((task) => (
            <button
              key={task.id}
              className={`task-item ${selectedId === task.id ? 'task-item--active' : ''}`}
              onClick={() => onSelect(task.id)}
            >
              <span className="task-title">{task.title}</span>
              <span className="task-meta">
                <span className={statusClass(task.status)}>{task.status}</span>
                <span>{fmtNumber(task.record_count)} рядків</span>
              </span>
              <span className="task-time">{timeAgo(task.updated_at)}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="sidebar-footer">
        {overview && (
          <div className="mini-stats">
            <div>
              <span className="mini-value">${overview.llm_spend_usd.toFixed(2)}</span>
              <span className="mini-label">$ LLM</span>
            </div>
            <div>
              <span className="mini-value">{fmtNumber(overview.cache.pages)}</span>
              <span className="mini-label">кеш</span>
            </div>
            <div>
              <span className="mini-value">{fmtNumber(overview.queued_jobs)}</span>
              <span className="mini-label">у черзі</span>
            </div>
          </div>
        )}
        <div className="engine-row">
          <span className={`dot ${caps?.llm_ready ? 'dot--ok' : 'dot--off'}`} />
          {caps?.llm_ready ? 'LLM готовий' : 'LLM офлайн'}
          <span className="sep">·</span>
          {caps?.offline ? 'offline' : (caps?.fetch_engine ?? 'http')}
        </div>
        {caps?.search_provider && (
          <div className="engine-row muted">пошук: {caps.search_provider}</div>
        )}
        <div className="engine-row muted" title={caps?.storage}>
          storage: {caps?.storage?.split('/').slice(-2).join('/') ?? '—'}
        </div>
      </div>
    </aside>
  )
}
