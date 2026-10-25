export interface ToastMessage {
  kind: 'success' | 'error' | 'info'
  text: string
}

export type DType =
  | 'string'
  | 'number'
  | 'integer'
  | 'boolean'
  | 'url'
  | 'email'
  | 'phone'
  | 'datetime'
  | 'json'
  | 'array'

export type Pii = 'none' | 'business_contact' | 'personal'
export type Method = 'json_ld' | 'css' | 'regex' | 'llm' | 'enrichment' | 'manual' | 'merge'
export type RunKind = 'calibrate' | 'collect' | 'enrich' | 'snapshot' | 'prepare'
export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'partial'

export interface FieldSpec {
  key: string
  title: string
  dtype: DType
  description: string
  required: boolean
  pii: Pii
  method: Method
  aliases: string[]
  selector: string | null
  attr: string | null
  prompt: string | null
  enrichment: string | null
}

export interface EnrichmentSpec {
  enricher: string
  fields: string[]
  config: Record<string, unknown>
  enabled: boolean
}

export interface Limits {
  max_pages: number
  max_depth: number
  budget_usd: number
  per_domain_rps: number | null
  sample_size: number
}

export interface SourcePolicy {
  domain: string
  tos_risk: 'low' | 'medium' | 'high' | 'forbidden' | 'unknown'
  robots_allowed: boolean | null
  license: string
  allowed_fields: string[]
  notes: string
  reviewed_at: string | null
}

export interface TaskSpec {
  title: string
  prompt: string
  entity: string
  locale: string
  columns: FieldSpec[]
  seeds: string[]
  extraction: 'auto' | 'json_ld' | 'css' | 'llm'
  enrichment: EnrichmentSpec[]
  limits: Limits
  rationale: string
  assumptions: string[]
  source_policy: Record<string, SourcePolicy>
  warning: string | null
}

export interface RunStats {
  urls_total: number
  urls_done: number
  pages_fetched: number
  pages_cached: number
  pages_failed: number
  robots_blocked: number
  records_new: number
  records_updated: number
  records_quarantined: number
  enrichments_run: number
  llm_calls: number
  llm_usd: number
  events: number
  started_at: string | null
  finished_at: string | null
}

export interface RunInfo {
  id: string
  task_id: string
  kind: RunKind
  status: RunStatus
  stats: RunStats
  error: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  budget_usd: number
  spent_usd: number
}

export interface TaskInfo {
  id: string
  title: string
  prompt: string
  spec: TaskSpec
  status: 'draft' | 'ready' | 'running' | 'done' | 'failed'
  created_at: string
  updated_at: string
  dataset_id: string | null
  record_count: number
  last_run: RunInfo | null
}

export interface EventInfo {
  id: number
  task_id: string
  run_id: string | null
  type: string
  level: 'debug' | 'info' | 'warning' | 'error'
  message: string
  data: Record<string, unknown>
  created_at: string
}

export interface CssField {
  key: string
  selector: string
  attr: string | null
  dtype: DType
  confidence: number
  aliases: string[]
}

export interface CssSchema {
  container: string
  item_scope: boolean
  fields: CssField[]
  sample_url: string | null
  coverage: Record<string, number>
  score: number
  method: 'json_ld' | 'css' | 'llm'
  item_signature: string | null
  notes: string[]
}

export interface CalibrationSample {
  url: string
  records: Record<string, unknown>[]
  error: string | null
}

export interface CalibrationReport {
  strategy: 'json_ld' | 'css' | 'llm' | 'none'
  schema: CssSchema | null
  samples: CalibrationSample[]
  coverage: Record<string, number>
  records_found: number
  pages_tried: number
  pages_ok: number
  warnings: string[]
  estimated_llm_cost_usd: number
}

export interface DatasetInfo {
  id: string
  task_id: string
  name: string
  version: number
  row_count: number
  columns: FieldSpec[]
  created_at: string
  updated_at: string
  freshness: string | null
  quality: Record<string, number>
  manifest: Record<string, unknown>
}

export interface DatasetVersion {
  version: number
  run_id: string | null
  created_at: string
  row_count: number
  manifest: Record<string, unknown>
}

export interface DatasetPayload {
  dataset: DatasetInfo | null
  quality: Record<string, number>
  versions: DatasetVersion[]
  freshness: string | null
}

export interface TaskDetail {
  task: TaskInfo
  calibration: CalibrationReport | null
  dataset: DatasetPayload
  runs: RunInfo[]
  active_run: RunInfo | null
}

export interface ProvenanceInfo {
  method: Method
  confidence: number
  source_url: string | null
  observed_at: string
  verified: boolean
  note: string | null
}

export interface RecordRow {
  record_key: string
  payload: Record<string, unknown>
  provenance: Record<string, ProvenanceInfo>
  source_url: string | null
  observed_at: string | null
  confidence: number
}

export interface RecordsResponse {
  items: RecordRow[]
  total: number
  limit: number
  offset: number
  columns: FieldSpec[]
}

export interface DuplicateCandidate {
  left_key: string
  right_key: string
  left_name: string
  right_name: string
  score: number
}

export interface DuplicatesResponse {
  items: DuplicateCandidate[]
  scanned: number
  note: string
}

export interface EnricherInfo {
  key: string
  title: string
  produces: string[]
  requires: string[]
}

export interface LLMProviderInfo {
  name: string
  model: string
  local: boolean
  available: boolean
  healthy: boolean
}

export interface Capabilities {
  version: string
  offline: boolean
  auth_required: boolean
  fetch_engine: string
  respect_robots: boolean
  llm_ready: boolean
  llm_providers: LLMProviderInfo[]
  llm_models: { primary: string; local: string | null }
  search_provider: string | null
  enrichers: EnricherInfo[]
  storage: string
  budget_usd_default: number
  max_pages_default: number
}

export interface Overview {
  tasks: number
  running_runs: number
  queued_jobs: number
  failed_jobs: number
  llm_spend_usd: number
  cache: { pages: number; compressed: string; path: string }
}

export interface ConfigResponse {
  capabilities: Capabilities
  overview: Overview
  enrichers: EnricherInfo[]
}

export interface ExportManifest {
  dataset_id?: string
  rows: number
  columns: { key: string; title: string; dtype: string; pii: string; method: string }[]
  masked_pii: boolean
  license: string
  quality?: Record<string, number>
  freshness?: string | null
  [key: string]: unknown
}

export interface ExportResult {
  file: string
  download_url: string
  manifest: ExportManifest
  rows: number
  bytes: number
  masked_pii: boolean
}

export interface ExportItem {
  file: string
  bytes: number
  modified: number
  download_url: string
}

export interface DashboardResponse {
  overview: Overview
  tasks: {
    id: string
    title: string
    status: string
    updated_at: string
    rows: number
    version: number
    last_run: RunInfo | null
  }[]
  capabilities: Capabilities
}

export interface RecordDetail {
  record: RecordRow
  history: {
    dataset_version: number
    revision: number
    payload: Record<string, unknown>
    provenance: Record<string, ProvenanceInfo>
    source_url: string | null
    observed_at: string
  }[]
}

export interface SourcePolicyView {
  domain: string
  tos_risk: string
  license: string
  notes: string
  allowed_fields: string[]
  reviewed_at: string | null
}
