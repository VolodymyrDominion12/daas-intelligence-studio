"""Provider cascade with per-run budget, disk cache and a spend ledger."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db.sqlite_store import OperationalStore
from ..utils import hash_text, iso
from .base import LLMBudget, LLMRequest, LLMResponse
from .providers import OpenAICompatProvider, approx_tokens, build_providers, estimate_cost

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self, settings: Settings, store: OperationalStore | None = None) -> None:
        self.settings = settings
        self.store = store
        self.providers = build_providers(settings)
        self.task_id: str | None = None
        self.run_id: str | None = None
        self.budget = LLMBudget(settings.llm_budget_usd)
        self.cache_dir: Path = settings.paths.root / "llm_cache"
        if settings.llm_cache:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, LLMResponse] = {}
        self._health: dict[str, tuple[bool, float]] = {}
        self._last_probe = 0.0

    # -- lifecycle --------------------------------------------------------
    def _healthy(self, provider_name: str, *, max_age: float = 120.0) -> bool:
        entry = self._health.get(provider_name)
        if entry is None:
            return True
        ok, checked_at = entry
        if time.monotonic() - checked_at > max_age:
            return True
        return ok

    @property
    def available(self) -> bool:
        return any(provider.available and self._healthy(provider.name) for provider in self.providers)

    async def probe(self, *, timeout: float = 2.0) -> None:
        """Cheap reachability check so the UI does not promise a model that is down."""
        for provider in self.providers:
            if not provider.available:
                self._health[provider.name] = (False, time.monotonic())
                continue
            if isinstance(provider, OpenAICompatProvider):
                headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
                try:
                    response = await provider._get_client().get(
                        f"{provider.base_url}/models", headers=headers, timeout=timeout
                    )
                    ok = response.status_code < 400 or response.status_code == 404
                except Exception:  # noqa: BLE001 - unreachable is the point of the probe
                    ok = False
                self._health[provider.name] = (ok, time.monotonic())
            else:
                self._health[provider.name] = (True, time.monotonic())
        self._last_probe = time.monotonic()

    async def probe_if_stale(self, *, max_age: float = 30.0) -> None:
        if time.monotonic() - self._last_probe > max_age:
            await self.probe()

    @property
    def description(self) -> list[dict[str, Any]]:
        return [
            {
                "name": provider.name,
                "model": provider.model,
                "local": provider.is_local,
                "available": provider.available and self._healthy(provider.name),
                "healthy": self._healthy(provider.name),
            }
            for provider in self.providers
        ]

    def bind(self, *, task_id: str | None, run_id: str | None, budget_usd: float | None = None) -> None:
        self.task_id = task_id
        self.run_id = run_id
        spent = self.store.run_spend(run_id) if (self.store and run_id) else 0.0
        self.budget = LLMBudget(
            self.settings.llm_budget_usd if budget_usd is None else budget_usd, spent
        )

    async def aclose(self) -> None:
        for provider in self.providers:
            try:
                await provider.aclose()
            except Exception:  # pragma: no cover - best effort
                logger.debug("failed to close llm provider %s", provider.name, exc_info=True)

    # -- main entrypoint --------------------------------------------------
    async def complete_json(
        self, request: LLMRequest, *, budget: LLMBudget | None = None
    ) -> LLMResponse:
        budget = budget or self.budget
        ordered = self._ordered(request)
        if not ordered:
            return LLMResponse(
                provider="none", model="none", error="no_llm_provider_configured"
            )

        last_error = "no provider attempted"
        for provider in ordered:
            cached = self._cache_get(request, provider.name)
            if cached is not None:
                return cached

            estimate_in = approx_tokens(request.system + request.user)
            estimate_usd = 0.0
            if isinstance(provider, OpenAICompatProvider):
                estimate_usd = estimate_cost(
                    provider.model, estimate_in, request.max_tokens, provider.is_local
                )
            if not budget.can_afford(estimate_usd):
                last_error = f"budget exhausted (${budget.spent_usd:.3f}/${budget.max_usd:.2f})"
                logger.info("skipping provider %s: %s", provider.name, last_error)
                continue

            response = await provider.complete(request)
            budget.charge(response.usd)
            if self.store is not None:
                self.store.record_llm_call(
                    purpose=request.purpose,
                    provider=response.provider,
                    model=response.model,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                    usd=response.usd,
                    cached=False,
                    task_id=self.task_id,
                    run_id=self.run_id,
                )
            if response.ok:
                self._cache_put(request, provider.name, response)
                return response
            last_error = response.error or "unknown provider error"
            if provider.is_local or "ConnectError" in last_error or "timed out" in last_error:
                self._health[provider.name] = (False, time.monotonic())
            logger.warning("llm provider %s failed: %s", provider.name, last_error)

        return LLMResponse(provider="none", model="none", error=last_error)

    def _ordered(self, request: LLMRequest):
        usable = [provider for provider in self.providers if provider.available]
        if request.prefer == "local":
            return sorted(usable, key=lambda p: (not p.is_local, p.name))
        if request.prefer == "quality":
            cloud = [p for p in usable if not p.is_local]
            local = [p for p in usable if p.is_local]
            return cloud + local
        return usable

    # -- cache ------------------------------------------------------------
    def _cache_path(self, request: LLMRequest, provider_name: str) -> Path:
        key = hash_text(request.cache_key() + "|" + provider_name)
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_get(self, request: LLMRequest, provider_name: str) -> LLMResponse | None:
        if not self.settings.llm_cache:
            return None
        key = hash_text(request.cache_key() + "|" + provider_name)
        if key in self._memory:
            response = self._memory[key].model_copy(deep=True)
            response.cached = True
            return response
        path = self._cache_path(request, provider_name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
            response = LLMResponse(**data, cached=True)
        except (json.JSONDecodeError, OSError, TypeError):
            return None
        self._memory[key] = response
        return response

    def _cache_put(self, request: LLMRequest, provider_name: str, response: LLMResponse) -> None:
        if not self.settings.llm_cache:
            return
        key = hash_text(request.cache_key() + "|" + provider_name)
        self._memory[key] = response.model_copy(deep=True)
        path = self._cache_path(request, provider_name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = response.model_dump(mode="json") | {"_cached_at": iso()}
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:  # pragma: no cover - disk issues should not break runs
            logger.warning("cannot write llm cache entry", exc_info=True)
