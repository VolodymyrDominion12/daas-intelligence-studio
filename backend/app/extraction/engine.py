"""Extraction engine: deterministic first, LLM only as a guarded fallback."""

from __future__ import annotations

import logging

from ..config import Settings
from ..llm.base import LLMBudget
from ..llm.router import LLMRouter
from ..models import CssSchema, ExtractedRecord, PageContent, Provenance, TaskSpec
from ..utils import coerce_value, normalize_domain, record_key_for
from .base import ExtractionOutcome
from .css import extract_css
from .jsonld import extract_jsonld
from .learner import fill_from_text
from .llm_extractor import LLMExtractor

logger = logging.getLogger(__name__)


class ExtractionEngine:
    def __init__(self, settings: Settings, router: LLMRouter) -> None:
        self.settings = settings
        self.router = router
        self.llm = LLMExtractor(router)

    async def extract(
        self,
        page: PageContent,
        spec: TaskSpec,
        *,
        css_schema: CssSchema | None = None,
        allow_llm: bool = True,
        budget: LLMBudget | None = None,
    ) -> ExtractionOutcome:
        warnings: list[str] = []
        cost = 0.0
        calls = 0

        jsonld_outcome = extract_jsonld(page.html, spec, source_url=page.final_url or page.url)
        css_outcome = (
            extract_css(page.html, css_schema, spec, base_url=page.final_url or page.url)
            if css_schema and css_schema.container
            else None
        )
        warnings.extend(jsonld_outcome.warnings)
        if css_outcome:
            warnings.extend(css_outcome.warnings)

        if css_outcome is not None and len(css_outcome.items) > 1:
            base = css_outcome
            supplement = jsonld_outcome if len(jsonld_outcome.items) == 1 else None
        elif jsonld_outcome.items:
            base = jsonld_outcome
            supplement = css_outcome if css_outcome and len(css_outcome.items) == 1 else None
        elif css_outcome is not None and css_outcome.items:
            base = css_outcome
            supplement = jsonld_outcome if len(jsonld_outcome.items) == 1 else None
        else:
            base = ExtractionOutcome(strategy="none", confidence=0.0)
            supplement = jsonld_outcome if jsonld_outcome.items else None

        items = [dict(item) for item in base.items]
        if supplement and supplement.items:
            primary = supplement.items[0]
            for item in items:
                if _same_entity(item, primary):
                    for key, value in primary.items():
                        item.setdefault(key, value)

        if not items and allow_llm:
            llm_outcome = await self.llm.extract(
                markdown=page.markdown,
                text=page.text,
                spec=spec,
                budget=budget,
            )
            warnings.extend(llm_outcome.warnings)
            cost += llm_outcome.llm_cost_usd
            calls += llm_outcome.llm_calls
            if llm_outcome.items:
                base = llm_outcome
                items = [dict(item) for item in llm_outcome.items]

        filled: list[dict] = []
        for item in items:
            self._fill_missing(item, spec, page)
            if self._is_usable(item, spec):
                filled.append(item)

        if not filled and not jsonld_outcome.items and not (css_outcome and css_outcome.items) and allow_llm:
            # Deterministic strategies found nothing at all: try the LLM even if items existed but were filtered.
            llm_outcome = await self.llm.extract(
                markdown=page.markdown, text=page.text, spec=spec, budget=budget
            )
            warnings.extend(llm_outcome.warnings)
            cost += llm_outcome.llm_cost_usd
            calls += llm_outcome.llm_calls
            for item in llm_outcome.items:
                self._fill_missing(item, spec, page)
                if self._is_usable(item, spec):
                    filled.append(item)
            if filled:
                base = llm_outcome

        return ExtractionOutcome(
            items=filled,
            strategy=base.strategy if filled else "none",
            confidence=base.confidence if filled else 0.0,
            warnings=warnings,
            llm_cost_usd=cost,
            llm_calls=calls,
        )

    def to_records(
        self, outcome: ExtractionOutcome, page: PageContent, spec: TaskSpec
    ) -> list[ExtractedRecord]:
        records: list[ExtractedRecord] = []
        for item in outcome.items:
            payload = {key: value for key, value in item.items() if value not in (None, "", [])}
            if not payload:
                continue
            key = record_key_for(payload, fallback=page.url)
            provenance: dict[str, Provenance] = {}
            for field_key in payload:
                column = spec.column_map.get(field_key)
                confidence = outcome.confidence
                if column is not None and column.method == "llm":
                    confidence = min(confidence, 0.65)
                provenance[field_key] = Provenance(
                    method=outcome.strategy if outcome.strategy in {"json_ld", "css", "llm"} else "css",
                    confidence=max(0.25, min(0.98, confidence)),
                    source_url=page.final_url or page.url,
                    verified=outcome.strategy in {"json_ld", "css"},
                )
            records.append(
                ExtractedRecord(
                    record_key=key,
                    payload=payload,
                    provenance=provenance,
                    source_url=page.final_url or page.url,
                    content_hash=page.content_hash,
                )
            )
        return records

    # -- helpers ----------------------------------------------------------
    def _fill_missing(self, item: dict, spec: TaskSpec, page: PageContent) -> None:
        text = page.text or ""
        for column in spec.columns:
            if column.is_ai or item.get(column.key) not in (None, "", []):
                continue
            if column.dtype in ("email", "phone") or (
                column.dtype in ("number", "integer") and "price" in column.key
            ):
                value = fill_from_text(text, column)
                if value not in (None, "", []):
                    item[column.key] = value
        if "website" in spec.column_map and not item.get("website"):
            domain = normalize_domain(page.final_url or page.url)
            if domain:
                item["website"] = f"https://{domain}"
        if "name" in spec.column_map and not item.get("name"):
            title = (page.text or "")[:120]
            if title:
                item["name"] = title.split("\n")[0].strip()[:120]

    def _is_usable(self, item: dict, spec: TaskSpec) -> bool:
        values = {key: value for key, value in item.items() if value not in (None, "", [])}
        if len(values) < 2:
            return False
        identity_keys = {"name", "title", "company", "website", "phone", "email", "address"}
        if not identity_keys & values.keys():
            return False
        # Validate dtypes once more (LLM output may ignore instructions).
        for key in list(values):
            column = spec.column_map.get(key)
            if column is None:
                values.pop(key)
                continue
            coerced = coerce_value(values[key], column.dtype)
            if coerced in (None, "", []):
                item.pop(key, None)
        return len(values) >= 2


def _same_entity(left: dict, right: dict) -> bool:
    left_domain = normalize_domain(str(left.get("website") or ""))
    right_domain = normalize_domain(str(right.get("website") or ""))
    if left_domain and left_domain == right_domain:
        return True
    left_name = str(left.get("name") or "").strip().casefold()
    right_name = str(right.get("name") or "").strip().casefold()
    return bool(left_name) and left_name == right_name
