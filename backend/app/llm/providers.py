"""Concrete LLM adapters (OpenAI-compatible + deterministic mock)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..utils import redact_pii
from .base import LLMProvider, LLMRequest, LLMResponse, LLMUnavailable
from .guard import sanitize_for_llm
from .parsing import parse_json_loose

# USD per 1M tokens (input, output). Local models are free by definition.
PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-sonnet-4": (3.00, 15.00),
    "deepseek-chat": (0.27, 1.10),
}


def estimate_cost(model: str, tokens_in: int, tokens_out: int, is_local: bool = False) -> float:
    if is_local:
        return 0.0
    key = next((name for name in PRICING if name in model.lower()), None)
    if not key:
        return 0.0
    price_in, price_out = PRICING[key]
    return round(tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out, 6)


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class OpenAICompatProvider:
    """Works with OpenAI, Ollama (``/v1``), LM Studio, vLLM, OpenRouter, compat proxies."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        base_url: str,
        api_key: str | None = None,
        is_local: bool = False,
        timeout_s: float = 120.0,
        json_mode: bool = True,
    ) -> None:
        self.name = name
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.is_local = is_local
        self.timeout_s = timeout_s
        self.json_mode = json_mode
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self.base_url and (self.is_local or self.api_key))

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        client = self._get_client()
        return await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        user_content = sanitize_for_llm(
            request.user, limit=60_000, redact=not self.is_local
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": user_content},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.schema and self.json_mode:
            body["response_format"] = {"type": "json_object"}

        try:
            response = await self._post(body)
            if response.status_code == 400 and "response_format" in body:
                body.pop("response_format", None)
                response = await self._post(body)
            if response.status_code >= 400:
                detail = response.text[:300]
                return LLMResponse(
                    provider=self.name,
                    model=self.model,
                    error=f"HTTP {response.status_code}: {detail}",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            payload = response.json()
        except httpx.HTTPError as exc:
            return LLMResponse(
                provider=self.name,
                model=self.model,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except ValueError as exc:
            return LLMResponse(provider=self.name, model=self.model, error=f"bad json: {exc}")

        text = _extract_content(payload)
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or approx_tokens(request.system + user_content))
        tokens_out = int(usage.get("completion_tokens") or approx_tokens(text))
        data: Any = None
        error: str | None = None
        if request.schema:
            try:
                data = parse_json_loose(text)
            except ValueError as exc:
                error = f"invalid structured output: {exc}"
        else:
            data = text

        return LLMResponse(
            provider=self.name,
            model=payload.get("model") or self.model,
            text=text,
            data=data,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            usd=estimate_cost(self.model, tokens_in, tokens_out, self.is_local),
            error=error,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class MockProvider:
    """Offline smoke-test provider. Never used unless explicitly selected."""

    name = "mock"
    is_local = True

    def __init__(self, model: str = "mock-1") -> None:
        self.model = model

    @property
    def available(self) -> bool:
        return True

    async def complete(self, request: LLMRequest) -> LLMResponse:
        data: Any
        if request.schema and "items" in str(request.schema):
            data = {"items": []}
        elif request.schema and "value" in str(request.schema):
            data = {"value": None, "confidence": 0.0, "note": "mock provider"}
        else:
            data = {"ok": True, "purpose": request.purpose}
        return LLMResponse(provider=self.name, model=self.model, data=data, text="{}")


def build_providers(settings) -> list[LLMProvider]:
    mode = settings.llm_provider
    if mode == "disabled":
        return []
    if mode == "mock":
        return [MockProvider()]

    providers: list[LLMProvider] = []
    if settings.llm_local_base_url:
        providers.append(
            OpenAICompatProvider(
                name="local",
                model=settings.llm_local_model or "local",
                base_url=settings.llm_local_base_url,
                is_local=True,
                timeout_s=settings.llm_timeout_s,
            )
        )
    if settings.llm_base_url and settings.llm_api_key:
        providers.append(
            OpenAICompatProvider(
                name="primary",
                model=settings.llm_model,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                timeout_s=settings.llm_timeout_s,
            )
        )
    if settings.llm_fallback_base_url and settings.llm_fallback_api_key:
        providers.append(
            OpenAICompatProvider(
                name="fallback",
                model=settings.llm_fallback_model or settings.llm_model,
                base_url=settings.llm_fallback_base_url,
                api_key=settings.llm_fallback_api_key,
                timeout_s=settings.llm_timeout_s,
            )
        )
    return providers


def require_provider(router) -> None:
    if not router.available:
        raise LLMUnavailable("no LLM provider is configured")


def redact_for_cloud(text: str) -> str:
    return redact_pii(text)


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""
