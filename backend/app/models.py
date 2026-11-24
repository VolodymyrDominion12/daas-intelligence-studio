"""Domain models shared by API, pipeline, extraction and storage layers."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .utils import canonical_url, utcnow

DType = Literal[
    "string", "number", "integer", "boolean", "url", "email", "phone", "datetime", "json", "array"
]
PiiClass = Literal["none", "business_contact", "personal"]
Method = Literal["json_ld", "css", "regex", "llm", "enrichment", "manual", "merge", "api_json"]
RunKind = Literal["calibrate", "collect", "enrich", "snapshot", "prepare"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "partial"]
TaskStatus = Literal["draft", "ready", "running", "done", "failed"]

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class Provenance(BaseModel):
    """Where a single field value came from and how much we trust it."""

    method: Method = "css"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_url: str | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    verified: bool = True
    note: str | None = None


def validate_css_selector(selector: str | None) -> str | None:
    if selector is None:
        return None
    cleaned = selector.strip()
    if not cleaned:
        return None
    from selectolax.lexbor import LexborHTMLParser

    try:
        LexborHTMLParser("<div></div>").css(cleaned)
    except Exception as exc:  # noqa: BLE001 - selectolax raises runtime/value errors on bad syntax
        raise ValueError(f"Невалідний CSS-селектор «{cleaned}»: {exc}") from exc
    return cleaned


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    dtype: DType = "string"
    description: str = ""
    required: bool = False
    pii: PiiClass = "none"
    method: Method = "css"
    aliases: list[str] = Field(default_factory=list)
    selector: str | None = None
    attr: str | None = None
    prompt: str | None = None
    enrichment: str | None = None

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if not _KEY_RE.match(normalized):
            raise ValueError("key must be snake_case starting with a letter (2-41 chars)")
        return normalized

    @field_validator("selector")
    @classmethod
    def _valid_selector(cls, value: str | None) -> str | None:
        return validate_css_selector(value)

    @field_validator("attr")
    @classmethod
    def _clean_attr(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @property
    def is_ai(self) -> bool:
        return self.method == "llm" or bool(self.prompt)


class EnrichmentSpec(BaseModel):
    enricher: str
    fields: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class Limits(BaseModel):
    max_pages: int = Field(default=50, ge=1, le=10_000)
    max_depth: int = Field(default=1, ge=0, le=3)
    budget_usd: float = Field(default=0.75, ge=0.0)
    per_domain_rps: float | None = Field(default=None, ge=0.1)
    sample_size: int = Field(default=4, ge=1, le=20)


class SourcePolicy(BaseModel):
    """Governance record per domain (docs/02 §2.1)."""

    domain: str
    tos_risk: Literal["low", "medium", "high", "forbidden", "unknown"] = "unknown"
    robots_allowed: bool | None = None
    license: str = "unknown"
    allowed_fields: list[str] = Field(default_factory=list)
    fallback_urls: list[str] = Field(default_factory=list)
    notes: str = ""
    reviewed_at: datetime | None = None

    @field_validator("fallback_urls")
    @classmethod
    def _validate_fallback_urls(cls, value: list[str]) -> list[str]:
        from .compliance import ComplianceError, assert_logged_out

        cleaned: list[str] = []
        for raw in value:
            url = str(raw or "").strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                raise ValueError("fallback URL must start with http:// or https://")
            try:
                assert_logged_out(url)
            except ComplianceError as exc:
                raise ValueError(str(exc)) from exc
            if url not in cleaned:
                cleaned.append(url)
        return cleaned


class ApiSourceSpec(BaseModel):
    """Declarative configuration for a public REST JSON API source (D-02)."""

    model_config = ConfigDict(extra="forbid")

    items_path: str = "data"
    id_path: str = "id"
    field_map: dict[str, str]
    next_page_path: str | None = "next_page.uri"
    cursor_path: str | None = "next_page.offset"
    cursor_param: str = "offset"
    detail_url_template: str | None = None
    record_url_template: str | None = None
    incremental: bool = True

    @field_validator("field_map")
    @classmethod
    def _non_empty_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {
            str(key).strip(): str(path).strip()
            for key, path in value.items()
            if str(key).strip() and str(path).strip()
        }
        if not cleaned:
            raise ValueError("api_source.field_map must contain at least one field mapping")
        return cleaned

    @field_validator("detail_url_template", "record_url_template")
    @classmethod
    def _validate_url_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("URL template must start with http:// or https://")
        from .compliance import assert_logged_out

        sample = cleaned.replace("{id}", "sample-id")
        assert_logged_out(sample)
        return cleaned


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    prompt: str
    entity: str = "company"
    locale: str = "uk"
    columns: list[FieldSpec]
    seeds: list[str] = Field(default_factory=list)
    extraction: Literal["auto", "json_ld", "css", "llm", "api_json"] = "auto"
    api_source: ApiSourceSpec | None = None
    enrichment: list[EnrichmentSpec] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    rationale: str = ""
    assumptions: list[str] = Field(default_factory=list)
    source_policy: dict[str, SourcePolicy] = Field(default_factory=dict)
    warning: str | None = None

    @model_validator(mode="after")
    def _validate_spec(self) -> TaskSpec:
        drop_query = self.api_source is None
        cleaned: list[str] = []
        for seed in self.seeds:
            seed = (seed or "").strip()
            if not seed:
                continue
            if not seed.startswith(("http://", "https://")):
                seed = "https://" + seed.lstrip("/")
            canonical = canonical_url(seed, drop_query=drop_query)
            if canonical not in cleaned:
                cleaned.append(canonical)
        self.seeds = cleaned

        keys = [column.key for column in self.columns]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate column keys in task spec")
        if not self.columns:
            raise ValueError("task spec needs at least one column")
        if self.api_source is not None:
            mapped = set(self.api_source.field_map) & set(keys)
            if not mapped:
                raise ValueError("api_source.field_map keys must match at least one task column")
        return self

    @property
    def column_map(self) -> dict[str, FieldSpec]:
        return {column.key: column for column in self.columns}

    @property
    def pii_columns(self) -> list[str]:
        return [column.key for column in self.columns if column.pii == "personal"]

    def source_columns(self) -> list[FieldSpec]:
        return [column for column in self.columns if not column.is_ai]


class CssField(BaseModel):
    key: str
    selector: str
    attr: str | None = None
    dtype: DType = "string"
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    aliases: list[str] = Field(default_factory=list)


class CssSchema(BaseModel):
    """Deterministic extraction plan learned during calibration (ADR-06)."""

    container: str
    item_scope: bool = True
    fields: list[CssField] = Field(default_factory=list)
    sample_url: str | None = None
    coverage: dict[str, float] = Field(default_factory=dict)
    score: float = 0.0
    method: Literal["json_ld", "css", "llm"] = "css"
    item_signature: str | None = None
    notes: list[str] = Field(default_factory=list)


class ExtractedRecord(BaseModel):
    record_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Provenance] = Field(default_factory=dict)
    source_url: str
    content_hash: str = ""

    def mean_confidence(self) -> float:
        if not self.provenance:
            return 0.0
        return sum(item.confidence for item in self.provenance.values()) / len(self.provenance)


class PageContent(BaseModel):
    url: str
    final_url: str
    status_code: int = 200
    html: str = ""
    text: str = ""
    markdown: str = ""
    content_hash: str = ""
    fetched_at: datetime = Field(default_factory=utcnow)
    engine: str = "http"
    headers: dict[str, str] = Field(default_factory=dict)
    elapsed_ms: int = 0
    from_cache: bool = False


class CalibrationSample(BaseModel):
    url: str
    records: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class CalibrationReport(BaseModel):
    strategy: Literal["json_ld", "css", "llm", "none"] = "none"
    schema_: CssSchema | None = Field(default=None, alias="schema")
    samples: list[CalibrationSample] = Field(default_factory=list)
    coverage: dict[str, float] = Field(default_factory=dict)
    records_found: int = 0
    pages_tried: int = 0
    pages_ok: int = 0
    warnings: list[str] = Field(default_factory=list)
    search_provider: str | None = None
    discovered_urls: list[str] = Field(default_factory=list)
    estimated_llm_cost_usd: float = 0.0

    model_config = ConfigDict(populate_by_name=True)


class SchemaFieldDiff(BaseModel):
    key: str
    selector_before: str | None = None
    selector_after: str | None = None
    attr_before: str | None = None
    attr_after: str | None = None
    dtype_before: DType | None = None
    dtype_after: DType | None = None
    confidence_before: float | None = None
    confidence_after: float | None = None
    coverage_before: float | None = None
    coverage_after: float | None = None


class SchemaDiffReport(BaseModel):
    current_version: int
    previous_version: int | None = None
    has_previous: bool = False
    has_changes: bool = False
    strategy_before: str | None = None
    strategy_after: str = "none"
    container_before: str | None = None
    container_after: str | None = None
    container_changed: bool = False
    item_scope_before: bool | None = None
    item_scope_after: bool | None = None
    score_before: float | None = None
    score_after: float | None = None
    score_delta: float | None = None
    records_before: int | None = None
    records_after: int = 0
    records_delta: int | None = None
    fields_added: list[CssField] = Field(default_factory=list)
    fields_removed: list[CssField] = Field(default_factory=list)
    fields_changed: list[SchemaFieldDiff] = Field(default_factory=list)
    coverage_deltas: dict[str, float] = Field(default_factory=dict)


class CalibrationHistoryEntry(BaseModel):
    version: int
    run_id: str | None = None
    source: str = "calibrate"
    strategy: str = "none"
    container: str | None = None
    item_scope: bool | None = None
    score: float = 0.0
    records_found: int = 0
    pages_ok: int = 0
    pages_tried: int = 0
    fields_count: int = 0
    created_at: datetime


class RunStats(BaseModel):
    urls_total: int = 0
    urls_done: int = 0
    pages_fetched: int = 0
    pages_cached: int = 0
    pages_failed: int = 0
    robots_blocked: int = 0
    records_new: int = 0
    records_updated: int = 0
    records_quarantined: int = 0
    records_removed: int = 0
    enrichments_run: int = 0
    llm_calls: int = 0
    llm_usd: float = 0.0
    events: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def merge(self, other: RunStats) -> RunStats:
        data = self.model_dump()
        for key, value in other.model_dump().items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                data[key] = (data.get(key) or 0) + value
            elif value is not None:
                data[key] = value
        return RunStats(**data)


class RunInfo(BaseModel):
    id: str
    task_id: str
    kind: RunKind
    status: RunStatus = "queued"
    stats: RunStats = Field(default_factory=RunStats)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    budget_usd: float = 0.0
    spent_usd: float = 0.0


class TaskInfo(BaseModel):
    id: str
    title: str
    prompt: str
    spec: TaskSpec
    status: TaskStatus = "draft"
    created_at: datetime
    updated_at: datetime
    dataset_id: str | None = None
    record_count: int = 0
    last_run: RunInfo | None = None


class EventInfo(BaseModel):
    id: int
    task_id: str
    run_id: str | None = None
    type: str
    level: Literal["debug", "info", "warning", "error"] = "info"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class DatasetInfo(BaseModel):
    id: str
    task_id: str
    name: str
    version: int = 0
    row_count: int = 0
    columns: list[FieldSpec] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    freshness: datetime | None = None
    quality: dict[str, float] = Field(default_factory=dict)
    manifest: dict[str, Any] = Field(default_factory=dict)


class ChangeEvent(BaseModel):
    type: Literal[
        "new_entity", "removed_entity", "field_changed", "price_drop", "new_job", "out_of_stock"
    ]
    record_key: str
    field: str | None = None
    old: Any = None
    new: Any = None
    delta_pct: float | None = None
    source_url: str | None = None


MergePolicy = Literal["confidence", "freshness", "completeness", "prefer_left", "prefer_right"]
EntityLinkStatus = Literal["merged", "dismissed", "unmerged"]


class DuplicateCandidate(BaseModel):
    left_key: str
    right_key: str
    left_name: str
    right_name: str
    score: float
    left_quality: float | None = None
    right_quality: float | None = None
    left_payload: dict[str, Any] = Field(default_factory=dict)
    right_payload: dict[str, Any] = Field(default_factory=dict)


class EntityLinkInfo(BaseModel):
    """Persistent entity resolution decision (merged / dismissed / unmerged)."""

    id: str
    task_id: str
    canonical_key: str
    merged_key: str
    status: EntityLinkStatus
    score: float = 0.0
    rule: str = "manual"
    policy: MergePolicy = "confidence"
    reviewer: str = "user"
    canonical_before_rev: int | None = None
    merged_before_rev: int | None = None
    created_at: datetime
    updated_at: datetime
    unmerged_at: datetime | None = None


class LLMCallRecord(BaseModel):
    purpose: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    cached: bool = False


class ScheduleInfo(BaseModel):
    """Per-task cron schedule persisted in operational SQLite storage."""

    task_id: str
    cron: str
    enabled: bool = True
    catch_up: bool = True
    acknowledge_high_risk: bool = False
    reason: str = ""
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_id: str | None = None
    created_at: datetime
    updated_at: datetime


DeliveryChannel = Literal["webhook", "telegram"]
DeliveryMode = Literal["instant", "digest_daily"]


class DeliveryFilters(BaseModel):
    """Deterministic filter criteria and delivery cadence for outbound delta notifications."""

    event_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    cpv_prefixes: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    field_filters: dict[str, list[str]] = Field(default_factory=dict)
    min_amount: float | None = None
    max_amount: float | None = None
    notify_on_error: bool = False
    delivery_mode: DeliveryMode = "instant"
    digest_cron: str = "0 6 * * *"

    @field_validator(
        "event_types",
        "keywords",
        "exclude_keywords",
        "cpv_prefixes",
        "regions",
        mode="before",
    )
    @classmethod
    def _clean_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("field_filters", mode="before")
    @classmethod
    def _clean_field_filters(cls, value: Any) -> dict[str, list[str]]:
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("field_filters має бути словником key -> list[str]")
        cleaned: dict[str, list[str]] = {}
        for raw_key, raw_vals in value.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            vals_iter = [raw_vals] if isinstance(raw_vals, str) else list(raw_vals or [])
            vals = [str(v or "").strip() for v in vals_iter if str(v or "").strip()]
            if vals:
                cleaned[key] = vals
        return cleaned

    @field_validator("digest_cron", mode="before")
    @classmethod
    def _validate_digest_cron(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "0 6 * * *"
        from .pipeline.scheduler import validate_cron

        return validate_cron(text)


class SubscriptionInfo(BaseModel):
    """Per-task outbound delivery subscription (webhook or Telegram chat)."""

    id: str
    task_id: str
    name: str = ""
    channel: DeliveryChannel
    target: str
    secret: str = ""
    filters: DeliveryFilters = Field(default_factory=DeliveryFilters)
    enabled: bool = True
    next_digest_at: datetime | None = None
    last_digest_at: datetime | None = None
    pending_digest_count: int = 0
    created_at: datetime
    updated_at: datetime


class QualityCheckIssue(BaseModel):
    """Single automated post-run quality invariant violation (P1-01)."""

    code: Literal["row_count_drop", "required_field_empty"]
    severity: Literal["warning", "error"] = "warning"
    field: str | None = None
    message: str
    metric: float
    threshold: float


class QualityChecksReport(BaseModel):
    """Automated post-run data quality report stored in dataset_versions.manifest."""

    passed: bool = True
    row_count: int = 0
    observed_count: int | None = None
    previous_row_count: int | None = None
    drop_ratio: float | None = None
    issues: list[QualityCheckIssue] = Field(default_factory=list)


class EvalFieldMetrics(BaseModel):
    """Precision/recall/F1/coverage computed for a single field over a gold dataset."""

    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    coverage: float = 0.0
    tp: int = 0
    fp: int = 0
    fn: int = 0


class EvalReport(BaseModel):
    """Offline gold-set extraction evaluation report (P1-01)."""

    gold_name: str = "inline"
    passed: bool = False
    min_f1: float = 0.8
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    coverage: float = 0.0
    cases: int = 0
    records_expected: int = 0
    records_extracted: int = 0
    llm_fallback_pct: float = 0.0
    usd_per_1k_records: float = 0.0
    dataset_version: int | None = None
    evaluated_at: datetime = Field(default_factory=utcnow)
    fields: dict[str, EvalFieldMetrics] = Field(default_factory=dict)
