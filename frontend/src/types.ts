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
export type Method = 'json_ld' | 'css' | 'regex' | 'llm' | 'enrichment' | 'manual' | 'merge' | 'api_json'
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

export interface ApiSourceSpec {
  items_path: string
  id_path: string
  next_page_path: string | null
  cursor_path: string | null
  cursor_param: string
  detail_url_template: string | null
  record_url_template: string | null
  field_map: Record<string, string>
  incremental: boolean
  headers: Record<string, string>
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
  fallback_urls?: string[]
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
  extraction: 'auto' | 'json_ld' | 'css' | 'llm' | 'api_json'
  api_source?: ApiSourceSpec | null
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
  records_removed?: number
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

export interface SchemaFieldDiff {
  key: string
  selector_before: string | null
  selector_after: string | null
  attr_before: string | null
  attr_after: string | null
  dtype_before: DType | null
  dtype_after: DType | null
  confidence_before: number | null
  confidence_after: number | null
  coverage_before: number | null
  coverage_after: number | null
}

export interface SchemaDiffReport {
  current_version: number
  previous_version: number | null
  has_previous: boolean
  has_changes: boolean
  strategy_before: string | null
  strategy_after: string
  container_before: string | null
  container_after: string | null
  container_changed: boolean
  item_scope_before: boolean | null
  item_scope_after: boolean | null
  score_before: number | null
  score_after: number | null
  score_delta: number | null
  records_before: number | null
  records_after: number
  records_delta: number | null
  fields_added: CssField[]
  fields_removed: CssField[]
  fields_changed: SchemaFieldDiff[]
  coverage_deltas: Record<string, number>
}

export interface CalibrationHistoryEntry {
  version: number
  run_id: string | null
  source: string
  strategy: string
  container: string | null
  item_scope: boolean | null
  score: number
  records_found: number
  pages_ok: number
  pages_tried: number
  fields_count: number
  created_at: string
}

export interface CalibrationStateResponse {
  calibration: CalibrationReport | null
  version: number | null
  history: CalibrationHistoryEntry[]
  diff: SchemaDiffReport | null
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
  calibration_version?: number | null
  calibration_history?: CalibrationHistoryEntry[]
  calibration_diff?: SchemaDiffReport | null
  dataset: DatasetPayload
  runs: RunInfo[]
  active_run: RunInfo | null
  quarantine_count?: number
  spend_usd?: number
}

export interface QuarantineItem {
  id: string
  run_id: string | null
  source_url: string
  reason: string
  payload: Record<string, unknown>
  created_at: string | null
}

export interface QuarantineResponse {
  items: QuarantineItem[]
  total: number
  limit: number
  offset: number
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

export type MergePolicy =
  | 'confidence'
  | 'freshness'
  | 'completeness'
  | 'prefer_left'
  | 'prefer_right'

export type EntityLinkStatus = 'merged' | 'dismissed' | 'unmerged'

export interface EntityLinkInfo {
  id: string
  task_id: string
  canonical_key: string
  merged_key: string
  status: EntityLinkStatus
  score: number
  rule: string
  policy: MergePolicy
  reviewer: string
  canonical_before_rev: number | null
  merged_before_rev: number | null
  created_at: string
  updated_at: string
  unmerged_at: string | null
}

export interface DuplicateCandidate {
  left_key: string
  right_key: string
  left_name: string
  right_name: string
  score: number
  left_quality?: number | null
  right_quality?: number | null
  left_payload?: Record<string, unknown>
  right_payload?: Record<string, unknown>
}

export interface DuplicatesResponse {
  items: DuplicateCandidate[]
  links: EntityLinkInfo[]
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

export interface JsRendererStatus {
  mode: string
  active_engine: string
  sidecar_configured: boolean
  sidecar_url: string | null
  local_playwright: boolean
  escalations: number
  rendered_ok: number
  fallbacks: number
  errors: number
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
  scheduler_enabled?: boolean
  proxies?: {
    pool_size: number
    healthy_count: number
    strategy: string
  }
  circuit_breaker?: {
    threshold: number
    cooldown_s: number
    open_domains: number
  }
  js_renderer?: JsRendererStatus
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
  fallback_urls: string[]
  reviewed_at: string | null
}

export interface ProxyEndpointView {
  id: string
  url: string
  scheme: string
  host: string
  port: number | null
  health_score: number
  in_cooldown: boolean
  cooldown_remaining_s: number
  requests: number
  successes: number
  blocks: number
  failures: number
  bytes_transferred: number
  cost_usd: number
}

export interface DomainCircuitView {
  domain: string
  state: 'closed' | 'open' | 'half_open'
  consecutive_blocks: number
  total_blocks: number
  total_successes: number
  total_requests: number
  last_reason: string | null
  cooldown_remaining_s: number
}

export interface ResilienceStatus {
  strategy: string
  pool_size: number
  healthy_count: number
  total_bytes: number
  total_cost_usd: number
  proxies: ProxyEndpointView[]
  circuits: DomainCircuitView[]
  js_renderer?: JsRendererStatus
}

export interface ScheduleInfo {
  task_id: string
  cron: string
  enabled: boolean
  catch_up: boolean
  acknowledge_high_risk: boolean
  reason: string
  next_run_at: string | null
  last_run_at: string | null
  last_run_id: string | null
  created_at: string
  updated_at: string
}

export type DeliveryChannel = 'webhook' | 'telegram'

export interface DeliveryFilters {
  event_types: string[]
  keywords: string[]
  min_amount: number | null
  max_amount: number | null
  notify_on_error: boolean
}

export interface SubscriptionView {
  id: string
  task_id: string
  name: string
  channel: DeliveryChannel
  target: string
  has_secret: boolean
  filters: DeliveryFilters
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface QualityCheckIssue {
  code: 'row_count_drop' | 'required_field_empty'
  severity: 'warning' | 'error'
  field: string | null
  message: string
  metric: number
  threshold: number
}

export interface QualityChecksReport {
  passed: boolean
  row_count: number
  observed_count: number | null
  previous_row_count: number | null
  drop_ratio: number | null
  issues: QualityCheckIssue[]
}

export interface EvalFieldMetrics {
  precision: number
  recall: number
  f1: number
  coverage: number
  tp: number
  fp: number
  fn: number
}

export interface EvalReport {
  gold_name: string
  passed: boolean
  min_f1: number
  precision: number
  recall: number
  f1: number
  coverage: number
  cases: number
  records_expected: number
  records_extracted: number
  llm_fallback_pct: number
  usd_per_1k_records: number
  dataset_version: number | null
  evaluated_at: string
  fields: Record<string, EvalFieldMetrics>
}

export interface GoldCaseInput {
  url: string
  content: string
  content_type?: 'auto' | 'html' | 'json'
  expected: Record<string, unknown>[]
}
