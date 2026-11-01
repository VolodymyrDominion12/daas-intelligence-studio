"""Enrichment contracts: declarative column agents compatible with waterfalls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings
from ..db.sqlite_store import OperationalStore
from ..llm.base import LLMBudget
from ..llm.router import LLMRouter
from ..models import Provenance, TaskSpec
from ..scraping.fetcher import Fetcher


@dataclass
class EnrichmentContext:
    settings: Settings
    fetcher: Fetcher
    router: LLMRouter
    store: OperationalStore | None = None
    task_id: str | None = None
    run_id: str | None = None
    budget: LLMBudget | None = None
    search: Any = None

    @property
    def offline(self) -> bool:
        return self.settings.offline


@dataclass
class EnrichmentResult:
    values: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Provenance] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    usd: float = 0.0
    calls: int = 0
    skipped: bool = False


class Enricher(Protocol):
    key: str
    title: str
    produces: list[str]
    requires: list[str]

    def applies(self, payload: dict[str, Any]) -> bool: ...

    async def run(
        self, payload: dict[str, Any], ctx: EnrichmentContext
    ) -> EnrichmentResult: ...


def merge_payload(
    payload: dict[str, Any],
    result: EnrichmentResult,
    spec: TaskSpec,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Merge enrichment values into a record without clobbering scraped data."""
    merged = dict(payload)
    by_key = spec.column_map
    for key, value in result.values.items():
        if value in (None, "", []):
            continue
        columns = [key]
        if key == "email":
            columns = [key, "emails"]
        if key == "phone":
            columns = [key, "phones"]
        for column_key in columns:
            if column_key not in by_key:
                continue
            existing = merged.get(column_key)
            if existing in (None, "", []) or overwrite:
                merged[column_key] = value
    return merged


def provenance_for(
    keys: list[str], result: EnrichmentResult, *, source_url: str | None = None, confidence: float = 0.7
) -> dict[str, Provenance]:
    return {
        key: result.provenance.get(
            key,
            Provenance(method="enrichment", confidence=confidence, source_url=source_url),
        )
        for key in keys
    }
