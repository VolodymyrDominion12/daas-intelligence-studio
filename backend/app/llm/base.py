"""LLM provider interfaces.

One protocol, several adapters: any OpenAI-compatible endpoint (Ollama, LM Studio,
vLLM, OpenRouter, Gemini-compat proxy, OpenAI itself) plus a deterministic mock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

Preference = Literal["local", "quality", "any"]


@dataclass(slots=True)
class LLMRequest:
    purpose: str
    system: str
    user: str
    schema: dict[str, Any] | None = None
    max_tokens: int = 2000
    temperature: float = 0.0
    prefer: Preference = "any"
    images: list[str] = field(default_factory=list)

    def cache_key(self) -> str:
        from ..utils import hash_text

        return hash_text(
            "|".join(
                [
                    self.purpose,
                    self.system,
                    self.user,
                    repr(self.schema),
                    str(self.max_tokens),
                    str(self.temperature),
                ]
            )
        )


class LLMResponse(BaseModel):
    provider: str
    model: str
    text: str = ""
    data: Any = None
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    cached: bool = False
    error: str | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and (self.data is not None or self.text)


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str
    is_local: bool

    @property
    def available(self) -> bool: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def aclose(self) -> None: ...


class LLMBudget:
    """Per-run spend guard (docs/02 §3 / G4)."""

    def __init__(self, max_usd: float, spent_usd: float = 0.0) -> None:
        self.max_usd = max(0.0, max_usd)
        self.spent_usd = max(0.0, spent_usd)
        self.calls = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_usd - self.spent_usd)

    def can_afford(self, estimate_usd: float) -> bool:
        # Local/free providers report 0.0 and are always allowed.
        if estimate_usd <= 0:
            return True
        return self.remaining >= estimate_usd

    def charge(self, usd: float) -> None:
        self.spent_usd = round(self.spent_usd + max(0.0, usd), 6)
        self.calls += 1


class LLMUnavailable(RuntimeError):
    pass


class LLMBudgetExceeded(RuntimeError):
    pass


class ExtractionItem(BaseModel):
    """One record produced by an LLM extraction/planning call."""

    values: dict[str, Any] = Field(default_factory=dict)
    confidence: dict[str, float] = Field(default_factory=dict)
    notes: str | None = None
