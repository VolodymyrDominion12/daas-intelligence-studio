"""LLM extraction fallback (only when deterministic strategies fail)."""

from __future__ import annotations

from typing import Any

from ..llm.base import LLMBudget, LLMRequest
from ..llm.guard import detect_injection, wrap_untrusted
from ..llm.prompts import EXTRACTION_SYSTEM, extraction_user
from ..llm.router import LLMRouter
from ..models import TaskSpec
from ..utils import coerce_value, truncate
from .base import ExtractionOutcome


class LLMExtractor:
    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def extract(
        self,
        *,
        markdown: str,
        text: str,
        spec: TaskSpec,
        budget: LLMBudget | None = None,
        max_items: int = 25,
    ) -> ExtractionOutcome:
        if not self.router.available:
            return ExtractionOutcome(
                strategy="llm", confidence=0.0, warnings=["LLM provider is not configured"]
            )
        schema = {
            column.key: {
                "dtype": column.dtype,
                "description": column.description or column.title,
                "required": column.required,
            }
            for column in spec.columns
            if not column.is_ai
        }
        findings = detect_injection(markdown or text)
        warnings = [f"possible prompt injection markers: {', '.join(findings)}"] if findings else []

        content = markdown or text
        content = truncate(content, 48_000)
        request = LLMRequest(
            purpose="extraction",
            system=EXTRACTION_SYSTEM.replace("{max_items}", str(max_items)),
            user=extraction_user(schema, wrap_untrusted(content)),
            schema={"type": "object", "properties": {"items": {"type": "array"}}},
            max_tokens=min(4000, 600 + 220 * len(schema)),
            prefer="local",
        )
        response = await self.router.complete_json(request, budget=budget)
        if not response.ok or not isinstance(response.data, dict):
            return ExtractionOutcome(
                strategy="llm",
                confidence=0.0,
                warnings=[*warnings, f"llm extraction failed: {response.error or 'invalid output'}"],
                llm_cost_usd=response.usd,
                llm_calls=1,
            )

        raw_items = response.data.get("items")
        if not isinstance(raw_items, list):
            return ExtractionOutcome(
                strategy="llm", confidence=0.0, warnings=[*warnings, "llm returned no items"],
                llm_cost_usd=response.usd, llm_calls=1,
            )

        allowed = set(schema)
        items: list[dict[str, Any]] = []
        confidences: list[dict[str, float]] = []
        for raw in raw_items[:max_items]:
            if not isinstance(raw, dict):
                continue
            values_raw = raw.get("values") if isinstance(raw.get("values"), dict) else raw
            values: dict[str, Any] = {}
            confidence_map: dict[str, float] = {}
            for key, value in values_raw.items():
                if key not in allowed:
                    continue
                column = spec.column_map[key]
                coerced = coerce_value(value, column.dtype)
                if coerced in (None, "", []):
                    continue
                values[key] = coerced
                try:
                    confidence_map[key] = max(0.0, min(1.0, float((raw.get("confidence") or {}).get(key, 0.55))))
                except (TypeError, ValueError, AttributeError):
                    confidence_map[key] = 0.55
            if values:
                items.append(values)
                confidences.append(confidence_map)

        mean = (
            sum(sum(map.values()) / max(1, len(map)) for map in confidences) / len(confidences)
            if confidences
            else 0.0
        )
        return ExtractionOutcome(
            items=items,
            strategy="llm",
            confidence=round(mean, 3),
            warnings=warnings,
            llm_cost_usd=response.usd,
            llm_calls=1,
        )
