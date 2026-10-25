import { useEffect, useMemo, useState } from 'react'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { api } from '../api'
import { fmtDateTime, fmtNumber, fmtValue } from '../format'
import type { FieldSpec, RecordRow, ToastMessage } from '../types'

interface Props {
  taskId: string
  columns: FieldSpec[]
  refreshKey: number
  onOpenRecord: (recordKey: string) => void
  onGoExport: () => void
  onToast: (toast: ToastMessage) => void
}

const PAGE_SIZE = 50

export function DataGrid({ taskId, columns, refreshKey, onOpenRecord, onGoExport, onToast }: Props) {
  const [q, setQ] = useState('')
  const [filterField, setFilterField] = useState('')
  const [filterValue, setFilterValue] = useState('')
  const [sort, setSort] = useState<string | null>(null)
  const [dir, setDir] = useState<'asc' | 'desc'>('asc')
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState<RecordRow[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)

  const helper = useMemo(() => createColumnHelper<RecordRow>(), [])

  const tableColumns = useMemo(() => {
    const definitions = [
      helper.display({
        id: '__index',
        header: '#',
        cell: ({ row }) => <span className="muted">{offset + row.index + 1}</span>,
      }),
      ...columns.map((column) =>
        helper.accessor((row) => row.payload[column.key], {
          id: column.key,
          header: column.title,
          cell: (info) => fmtValue(info.getValue(), column.dtype),
        }),
      ),
      helper.accessor('confidence', {
        id: 'confidence',
        header: 'Conf',
        cell: (info) => {
          const value = info.getValue()
          return (
            <span
              className={`conf ${value >= 0.85 ? 'conf--high' : value >= 0.6 ? 'conf--mid' : 'conf--low'}`}
            >
              {Math.round(value * 100)}%
            </span>
          )
        },
      }),
      helper.accessor('observed_at', {
        id: 'observed_at',
        header: 'Оновлено',
        cell: (info) => <span className="muted">{fmtDateTime(info.getValue())}</span>,
      }),
    ]
    return definitions
  }, [columns, helper, offset])

  const table = useReactTable({
    data: rows,
    columns: tableColumns,
    getCoreRowModel: getCoreRowModel(),
    manualPagination: true,
    manualSorting: true,
    pageCount: Math.max(1, Math.ceil(total / PAGE_SIZE)),
  })

  useEffect(() => {
    let cancelled = false
    const timer = setTimeout(async () => {
      setLoading(true)
      try {
        const response = await api.records(taskId, {
          q: q.trim() || undefined,
          sort: sort ?? undefined,
          dir,
          limit: PAGE_SIZE,
          offset,
          filters: filterField && filterValue ? { [filterField]: filterValue } : {},
        })
        if (cancelled) return
        setRows(response.items)
        setTotal(response.total)
      } catch (error) {
        if (!cancelled) {
          onToast({
            kind: 'error',
            text: error instanceof Error ? error.message : 'Не вдалося завантажити записи',
          })
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }, 250)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [taskId, q, sort, dir, offset, filterField, filterValue, refreshKey, onToast])

  const toggleSort = (key: string) => {
    if (!columns.some((column) => column.key === key)) return
    if (sort === key) {
      setDir(dir === 'asc' ? 'desc' : 'asc')
    } else {
      setSort(key)
      setDir('asc')
    }
    setOffset(0)
  }

  const page = Math.floor(offset / PAGE_SIZE) + 1
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + PAGE_SIZE, total)

  return (
    <section className="card data-card">
      <div className="grid-toolbar">
        <input
          className="input input--search"
          placeholder="Пошук по даних…"
          value={q}
          onChange={(event) => {
            setQ(event.target.value)
            setOffset(0)
          }}
        />
        <select
          className="select select--auto"
          value={filterField}
          onChange={(event) => {
            setFilterField(event.target.value)
            setOffset(0)
          }}
        >
          <option value="">Фільтр…</option>
          {columns.map((column) => (
            <option key={column.key} value={column.key}>
              {column.title}
            </option>
          ))}
        </select>
        <input
          className="input input--filter"
          placeholder="значення"
          value={filterValue}
          disabled={!filterField}
          onChange={(event) => {
            setFilterValue(event.target.value)
            setOffset(0)
          }}
        />
        <div className="spacer" />
        <span className="muted count-label">
          {fmtNumber(total)} рядків {loading && <span className="spinner" />}
        </span>
        <button className="btn btn--ghost" onClick={onGoExport} disabled={total === 0}>
          Експорт
        </button>
      </div>

      <div className="table-wrap table-wrap--grid">
        <table className="table table--grid">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const sortable = columns.some((column) => column.key === header.column.id)
                  return (
                    <th
                      key={header.id}
                      className={sortable ? 'th-sortable' : undefined}
                      onClick={() => sortable && toggleSort(header.column.id)}
                    >
                      <span>
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {sort === header.column.id && (
                          <span className="sort-arrow">{dir === 'asc' ? '↑' : '↓'}</span>
                        )}
                      </span>
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                onClick={() => onOpenRecord(row.original.record_key)}
                className="grid-row"
                title="Відкрити походження даних"
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr>
                <td colSpan={tableColumns.length}>
                  <div className="empty-hint">
                    Даних ще немає. Запустіть калібрування, потім «Зібрати».
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="pagination">
        <span className="muted">
          {from}–{to} з {fmtNumber(total)}
        </span>
        <div className="pager">
          <button
            className="btn btn--tiny"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← Назад
          </button>
          <span className="muted">
            {page} / {pages}
          </span>
          <button
            className="btn btn--tiny"
            disabled={to >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Далі →
          </button>
        </div>
      </div>
    </section>
  )
}
