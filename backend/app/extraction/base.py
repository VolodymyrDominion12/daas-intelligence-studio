"""Shared outcome type for every extraction strategy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExtractionOutcome:
    items: list[dict[str, Any]] = field(default_factory=list)
    strategy: str = "none"
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    llm_cost_usd: float = 0.0
    llm_calls: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.items)
