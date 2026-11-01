import { useEffect, useRef, useState } from 'react'
import type { EventInfo, RunInfo } from '../types'
import { LEVEL_LABELS, eventTone, fmtDateTime, fmtNumber } from '../format'

interface Props {
  events: EventInfo[]
  activeRun: RunInfo | null
}

export function EventsPanel({ events, activeRun }: Props) {
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState<'all' | 'warn'>('all')
  const bottomRef = useRef<HTMLDivElement | null>(null)
  const boxRef = useRef<HTMLDivElement | null>(null)

  const visible = events.filter((event) =>
    filter === 'all' ? true : event.level === 'warning' || event.level === 'error',
  )
  const shown = visible.slice(-250)

  useEffect(() => {
    if (paused) return
    const box = boxRef.current
    if (box && box.scrollHeight - box.scrollTop - box.clientHeight < 220) {
      bottomRef.current?.scrollIntoView({ block: 'end' })
    }
  }, [shown.length, paused])

  return (
    <div className="card log-card">
      <div className="card-head">
        <h3 className="card-title">
          Живий журнал
          {activeRun && <span className="pulse" />}
        </h3>
        <div className="card-actions">
          <button
            className={`tiny-btn ${filter === 'warn' ? 'tiny-btn--active' : ''}`}
            onClick={() => setFilter(filter === 'all' ? 'warn' : 'all')}
          >
            {filter === 'all' ? 'всі' : 'важливі'}
          </button>
          <button
            className={`tiny-btn ${paused ? 'tiny-btn--active' : ''}`}
            onClick={() => setPaused((value) => !value)}
          >
            {paused ? 'продовжити' : 'пауза'}
          </button>
        </div>
      </div>

      {activeRun && (
        <div className="run-strip">
          <div>
            <span className="run-strip-value">{fmtNumber(activeRun.stats.pages_fetched)}</span>
            <span className="run-strip-label">стор.</span>
          </div>
          <div>
            <span className="run-strip-value">+{fmtNumber(activeRun.stats.records_new)}</span>
            <span className="run-strip-label">нових</span>
          </div>
          <div>
            <span className="run-strip-value">↻{fmtNumber(activeRun.stats.records_updated)}</span>
            <span className="run-strip-label">оновлено</span>
          </div>
          <div>
            <span className="run-strip-value">${activeRun.spent_usd.toFixed(3)}</span>
            <span className="run-strip-label">
              з ${activeRun.budget_usd > 0 ? activeRun.budget_usd.toFixed(2) : '—'}
            </span>
          </div>
        </div>
      )}

      <div className="log" ref={boxRef}>
        {shown.length === 0 && (
          <div className="empty-hint">
            Події з'являться тут: калібрування, сторінки, виявлені записи та помилки.
          </div>
        )}
        {shown.map((event) => (
          <div className={eventTone(event)} key={event.id}>
            <span className="log-time">{fmtDateTime(event.created_at).split(', ')[1] ?? ''}</span>
            <span className={`log-level log-level--${event.level}`}>{LEVEL_LABELS[event.level]}</span>
            <span className="log-msg" title={event.message}>
              {event.message}
            </span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
