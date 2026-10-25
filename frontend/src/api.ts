import type {
  CalibrationReport,
  ConfigResponse,
  DatasetPayload,
  DuplicatesResponse,
  EventInfo,
  ExportItem,
  ExportResult,
  FieldSpec,
  Limits,
  RecordDetail,
  RecordsResponse,
  RunInfo,
  SourcePolicyView,
  TaskDetail,
  TaskInfo,
  TaskSpec,
} from './types'

declare const __DAAS_TOKEN__: string

const metaToken =
  document.querySelector('meta[name="daas-token"]')?.getAttribute('content') ?? ''
export const TOKEN =
  metaToken && metaToken !== '__DAAS_TOKEN__'
    ? metaToken
    : typeof __DAAS_TOKEN__ === 'string'
      ? __DAAS_TOKEN__
      : ''

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (TOKEN) headers.set('Authorization', `Bearer ${TOKEN}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api${path}`, { ...init, headers })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json()
      if (typeof payload?.detail === 'string') detail = payload.detail
      else if (Array.isArray(payload?.detail)) detail = payload.detail.map((e: { msg?: string }) => e.msg).join('; ')
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function withToken(url: string): string {
  if (!TOKEN) return url
  return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(TOKEN)}`
}

export function eventStreamUrl(taskId: string, after = 0): string {
  return withToken(`/api/tasks/${taskId}/events/stream?after=${after}`)
}

export const api = {
  config: () => request<ConfigResponse>('/config'),
  tasks: () => request<{ items: TaskInfo[] }>('/tasks'),
  task: (id: string) => request<TaskDetail>(`/tasks/${id}`),
  createTask: (payload: { prompt: string; seeds: string[]; title?: string }) =>
    request<{ task: TaskInfo; spec: TaskSpec }>('/tasks', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateTask: (id: string, payload: { title?: string; spec?: TaskSpec }) =>
    request<{ task: TaskInfo }>(`/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  updateLimits: (id: string, limits: Limits) =>
    request<{ task: TaskInfo }>(`/tasks/${id}/limits`, {
      method: 'PATCH',
      body: JSON.stringify({ limits }),
    }),
  deleteTask: (id: string) => request<{ deleted: boolean }>(`/tasks/${id}`, { method: 'DELETE' }),
  addColumn: (id: string, field: Partial<FieldSpec>) =>
    request<{ task: TaskInfo }>(`/tasks/${id}/columns`, {
      method: 'POST',
      body: JSON.stringify({ field }),
    }),
  deleteColumn: (id: string, key: string) =>
    request<{ task: TaskInfo }>(`/tasks/${id}/columns/${key}`, { method: 'DELETE' }),
  calibrate: (id: string) =>
    request<{ run: RunInfo }>(`/tasks/${id}/calibrate`, { method: 'POST' }),
  run: (id: string, options: { skip_calibration?: boolean; force?: boolean } = {}) =>
    request<{ run: RunInfo }>(`/tasks/${id}/run`, {
      method: 'POST',
      body: JSON.stringify({ skip_calibration: false, force: false, ...options }),
    }),
  enrich: (id: string, columnKeys: string[] | null = null) =>
    request<{ run: RunInfo }>(`/tasks/${id}/enrich`, {
      method: 'POST',
      body: JSON.stringify({ column_keys: columnKeys }),
    }),
  snapshot: (id: string) => request<{ run: RunInfo }>(`/tasks/${id}/snapshot`, { method: 'POST' }),
  cancelRun: (runId: string) =>
    request<{ run: RunInfo }>(`/runs/${runId}/cancel`, { method: 'POST' }),
  calibration: (id: string) =>
    request<{ calibration: CalibrationReport | null }>(`/tasks/${id}/calibration`),
  runs: (id: string) => request<{ items: RunInfo[] }>(`/tasks/${id}/runs`),
  events: (id: string, after = 0) =>
    request<{ items: EventInfo[] }>(`/tasks/${id}/events?after=${after}&limit=500`),
  changes: (id: string) =>
    request<{ items: EventInfo[] }>(`/tasks/${id}/changes?limit=200`),
  dataset: (id: string) => request<DatasetPayload>(`/tasks/${id}/dataset`),
  records: (
    id: string,
    params: {
      q?: string
      sort?: string
      dir?: 'asc' | 'desc'
      limit?: number
      offset?: number
      filters?: Record<string, string>
    },
  ) => {
    const search = new URLSearchParams()
    if (params.q) search.set('q', params.q)
    if (params.sort) search.set('sort', params.sort)
    if (params.dir) search.set('dir', params.dir)
    search.set('limit', String(params.limit ?? 50))
    search.set('offset', String(params.offset ?? 0))
    for (const [key, value] of Object.entries(params.filters ?? {})) {
      if (value) search.set(`f_${key}`, value)
    }
    return request<RecordsResponse>(`/tasks/${id}/records?${search.toString()}`)
  },
  record: (id: string, recordKey: string) =>
    request<RecordDetail>(`/tasks/${id}/records/${encodeURIComponent(recordKey)}`),
  duplicates: (id: string, threshold = 88) =>
    request<DuplicatesResponse>(`/tasks/${id}/duplicates?threshold=${threshold}`),
  export: (id: string, payload: { format: string; mask_pii: boolean; limit?: number | null }) =>
    request<ExportResult>(`/tasks/${id}/exports`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  exports: () => request<{ items: ExportItem[] }>('/exports'),
  sources: () => request<{ items: SourcePolicyView[] }>('/sources'),
  updateSource: (
    domain: string,
    payload: {
      tos_risk: string
      license: string
      notes: string
      allowed_fields?: string[]
      reviewed?: boolean
    },
  ) =>
    request<{ item: SourcePolicyView }>(`/sources/${domain}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
}
