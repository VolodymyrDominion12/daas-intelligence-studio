"""Request/response schemas for the local HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..models import FieldSpec, Limits, SourcePolicy, TaskSpec


class CreateTaskRequest(BaseModel):
    prompt: str = Field(min_length=5, max_length=4000)
    seeds: list[str] = Field(default_factory=list, max_length=20)
    title: str | None = Field(default=None, max_length=80)


class UpdateTaskRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    spec: TaskSpec | None = None


class RunRequest(BaseModel):
    skip_calibration: bool = False
    force: bool = False


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
    reviewed: bool = False

    @field_validator("notes")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value[:1000]


class SourcePolicyView(BaseModel):
    domain: str
    tos_risk: str
    license: str
    notes: str = ""
    allowed_fields: list[str] = Field(default_factory=list)
    reviewed_at: str | None = None


def policy_view(policy: SourcePolicy) -> SourcePolicyView:
    return SourcePolicyView(
        domain=policy.domain,
        tos_risk=policy.tos_risk,
        license=policy.license,
        notes=policy.notes,
        allowed_fields=policy.allowed_fields,
        reviewed_at=policy.reviewed_at.isoformat() if policy.reviewed_at else None,
    )
