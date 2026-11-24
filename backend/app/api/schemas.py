"""Request/response schemas for the local HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..models import (
    ApiSourceSpec,
    DeliveryChannel,
    DeliveryFilters,
    FieldSpec,
    Limits,
    MergePolicy,
    SourcePolicy,
    SubscriptionInfo,
    TaskSpec,
)
from ..pipeline.scheduler import validate_cron


class CreateTaskRequest(BaseModel):
    prompt: str = Field(min_length=5, max_length=4000)
    seeds: list[str] = Field(default_factory=list, max_length=20)
    title: str | None = Field(default=None, max_length=80)
    api_source: ApiSourceSpec | None = None


class UpdateTaskRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    spec: TaskSpec | None = None


class RunRequest(BaseModel):
    skip_calibration: bool = False
    force: bool = False
    acknowledge_high_risk: bool = False
    reason: str = Field(default="", max_length=500)


class ScheduleRequest(BaseModel):
    cron: str = Field(min_length=3, max_length=100)
    enabled: bool = True
    catch_up: bool = True
    acknowledge_high_risk: bool = False
    reason: str = Field(default="", max_length=500)

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        return validate_cron(value)


class CreateSubscriptionRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    channel: DeliveryChannel
    target: str = Field(min_length=1, max_length=2000)
    secret: str = Field(default="", max_length=500)
    filters: DeliveryFilters = Field(default_factory=DeliveryFilters)
    enabled: bool = True


class UpdateSubscriptionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, min_length=1, max_length=2000)
    secret: str | None = Field(default=None, max_length=500)
    filters: DeliveryFilters | None = None
    enabled: bool | None = None


class SubscriptionView(BaseModel):
    id: str
    task_id: str
    name: str = ""
    channel: DeliveryChannel
    target: str
    has_secret: bool = False
    filters: DeliveryFilters = Field(default_factory=DeliveryFilters)
    enabled: bool = True
    created_at: str
    updated_at: str


def subscription_view(sub: SubscriptionInfo) -> SubscriptionView:
    return SubscriptionView(
        id=sub.id,
        task_id=sub.task_id,
        name=sub.name,
        channel=sub.channel,
        target=sub.target,
        has_secret=bool(sub.secret),
        filters=sub.filters,
        enabled=sub.enabled,
        created_at=sub.created_at.isoformat(),
        updated_at=sub.updated_at.isoformat(),
    )


class EnrichRequest(BaseModel):
    column_keys: list[str] | None = None


class AddColumnRequest(BaseModel):
    field: FieldSpec


class LimitsRequest(BaseModel):
    limits: Limits


class ExportRequest(BaseModel):
    format: Literal["csv", "xlsx", "parquet", "json"] = "csv"
    mask_pii: bool = True
    limit: int | None = Field(default=None, ge=1, le=1_000_000)


class PreviewRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=200)
    mask_pii: bool = True


class SourcePolicyRequest(BaseModel):
    tos_risk: Literal["low", "medium", "high", "forbidden", "unknown"] = "unknown"
    license: str = "unknown"
    notes: str = ""
    allowed_fields: list[str] = Field(default_factory=list)
    fallback_urls: list[str] = Field(default_factory=list)
    reviewed: bool = False

    @field_validator("notes")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value[:1000]

    @field_validator("fallback_urls")
    @classmethod
    def _validate_fallbacks(cls, value: list[str]) -> list[str]:
        from ..compliance import ComplianceError, assert_logged_out

        cleaned: list[str] = []
        for raw in value:
            url = str(raw or "").strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                raise ValueError("Fallback URL має починатися з http:// або https://")
            try:
                assert_logged_out(url)
            except ComplianceError as exc:
                raise ValueError(str(exc)) from exc
            if url not in cleaned:
                cleaned.append(url)
        return cleaned


class SourcePolicyView(BaseModel):
    domain: str
    tos_risk: str
    license: str
    notes: str = ""
    allowed_fields: list[str] = Field(default_factory=list)
    fallback_urls: list[str] = Field(default_factory=list)
    reviewed_at: str | None = None


def policy_view(policy: SourcePolicy) -> SourcePolicyView:
    return SourcePolicyView(
        domain=policy.domain,
        tos_risk=policy.tos_risk,
        license=policy.license,
        notes=policy.notes,
        allowed_fields=policy.allowed_fields,
        fallback_urls=policy.fallback_urls,
        reviewed_at=policy.reviewed_at.isoformat() if policy.reviewed_at else None,
    )


class GoldCaseInput(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=1_000_000)
    content_type: Literal["auto", "html", "json"] = "auto"
    expected: list[dict[str, Any]] = Field(default_factory=list, max_length=500)


class EvalRequest(BaseModel):
    cases: list[GoldCaseInput] = Field(default_factory=list, max_length=200)
    gold_preset: str | None = Field(default=None, max_length=100)
    min_f1: float | None = Field(default=None, ge=0.0, le=1.0)


class MergeDuplicatesRequest(BaseModel):
    canonical_key: str = Field(min_length=1, max_length=200)
    merged_key: str = Field(min_length=1, max_length=200)
    policy: MergePolicy = "confidence"
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    rule: str = Field(default="manual", min_length=1, max_length=80)
    reviewer: str = Field(default="user", min_length=1, max_length=80)
    field_overrides: dict[str, Literal["left", "right"]] = Field(default_factory=dict)


class DismissDuplicateRequest(BaseModel):
    left_key: str = Field(min_length=1, max_length=200)
    right_key: str = Field(min_length=1, max_length=200)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    rule: str = Field(default="manual", min_length=1, max_length=80)
    reviewer: str = Field(default="user", min_length=1, max_length=80)

