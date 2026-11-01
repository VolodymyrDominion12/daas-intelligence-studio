"""AI prompt columns ("+ AI column"): one guarded LLM call per record per column."""

from __future__ import annotations

from typing import Any

from ..llm.base import LLMRequest
from ..llm.prompts import AI_COLUMN_SYSTEM, ai_column_user
from ..models import FieldSpec, Provenance, TaskSpec
from ..utils import coerce_value, truncate
from .base import EnrichmentContext, EnrichmentResult


class AiColumns:
    async def run(
        self, payload: dict[str, Any], spec: TaskSpec, ctx: EnrichmentContext
    ) -> EnrichmentResult:
        columns = [column for column in spec.columns if column.is_ai and column.prompt]
        if not columns:
            return EnrichmentResult(skipped=True)
        if not ctx.router.available:
            return EnrichmentResult(
                skipped=True, notes=["AI columns need a configured LLM provider"]
            )

        values: dict[str, Any] = {}
        provenance: dict[str, Provenance] = {}
        notes: list[str] = []
        usd = 0.0
        calls = 0

        allowed_input = {
            key: value
            for key, value in payload.items()
            if key in spec.column_map and spec.column_map[key].pii != "personal"
        }
        for column in columns:
            request = LLMRequest(
                purpose=f"ai_column:{column.key}",
                system=AI_COLUMN_SYSTEM,
                user=ai_column_user(
                    column.prompt or column.description, _compact(allowed_input), column.dtype
                ),
                schema={"type": "object", "properties": {"value": {}, "confidence": {"type": "number"}}},
                max_tokens=500,
                prefer="quality",
            )
            response = await ctx.router.complete_json(request, budget=ctx.budget)
            calls += 1
            usd += response.usd
            if not response.ok or not isinstance(response.data, dict):
                notes.append(f"{column.key}: {response.error or 'invalid output'}")
                continue
            raw_value = response.data.get("value")
            value = coerce_value(raw_value, column.dtype)
            if value in (None, "", []):
                continue
            try:
                confidence = max(0.2, min(0.9, float(response.data.get("confidence") or 0.5)))
            except (TypeError, ValueError):
                confidence = 0.5
            values[column.key] = value
            provenance[column.key] = Provenance(
                method="llm",
                confidence=confidence,
                source_url=payload.get("_source_url"),
                note=str(response.data.get("note") or "")[:240] or None,
            )
            if response.data.get("note"):
                notes.append(f"{column.key}: {response.data['note']}")

        return EnrichmentResult(
            values=values, provenance=provenance, notes=notes[:8], usd=usd, calls=calls
        )


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str):
            compacted[key] = truncate(value, 600)
        elif isinstance(value, (dict, list)):
            compacted[key] = truncate(str(value), 600)
        else:
            compacted[key] = value
    return compacted


def ai_columns_plan(spec: TaskSpec) -> list[FieldSpec]:
    return [column for column in spec.columns if column.is_ai and column.prompt]
