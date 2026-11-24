"""Fetching: HTTP by default, optional Playwright; robots + rate limits + SSRF guard."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import Settings
from ..models import PageContent
from ..storage.raw_cache import RawCache
from ..utils import (
    UnsafeUrlError,
    assert_public_url,
    canonical_url,
    hash_text,
    normalize_domain,
    utcnow,
)
from .page import html_to_markdown, html_to_text
from .politeness import (
    DomainRateLimiter,
    RobotsGate,
    classify_status,
    retry_after_seconds,
)
from .resilience import (
    CircuitStateName,
    DomainCircuitBreaker,
    NoopProxyProvider,
    ProxyEndpoint,
    ProxyProvider,
    detect_ban_marker,
    redact_proxy_url,
)

logger = logging.getLogger(__name__)

FETCH_MAX_BYTES = 6 * 1024 * 1024
REDIRECT_LIMIT = 4
ErrorKind = Literal[
    "transient",
    "blocked",
    "permanent",
    "robots",
    "unsafe",
    "disabled",
    "empty",
    "policy",
    "circuit_open",
]


@dataclass(slots=True)
class FetchResult:
    page: PageContent | None = None
    kind: Literal["ok"] | ErrorKind = "ok"
    error: str | None = None
    status: int | None = None

    @property
    def ok(self) -> bool:
        return self.page is not None and self.kind == "ok"


class Fetcher(Protocol):
    name: str

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult: ...

    async def aclose(self) -> None: ...


class HttpFetcher:
    name = "http"

    def __init__(
        self,
        settings: Settings,
        *,
        cache: RawCache | None = None,
        robots: RobotsGate | None = None,
        limiter: DomainRateLimiter | None = None,
        proxy_provider: ProxyProvider | None = None,
        circuit_breaker: DomainCircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        client_factory: Callable[[str | None], httpx.AsyncClient] | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        # Set by compliance.PolicyGuardFetcher: returns a reason if a redirect hop is not allowed.
        self.redirect_guard: Callable[[str], str | None] | None = None
        self.robots = robots or RobotsGate(settings)
        self.limiter = limiter or DomainRateLimiter(
            max_concurrency=settings.max_concurrency,
            per_domain_concurrency=settings.per_domain_concurrency,
            default_rps=settings.per_domain_rps,
        )
        self.proxy_provider: ProxyProvider = proxy_provider or NoopProxyProvider()
        self.circuit_breaker = circuit_breaker
        self._transport = transport
        self._client_factory = client_factory
        self._proxy_clients: dict[str, httpx.AsyncClient] = {}
        self._client = self._new_client(proxy_url=None)

    def _new_client(self, *, proxy_url: str | None) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(proxy_url)
        kwargs: dict = {
            "timeout": httpx.Timeout(self.settings.request_timeout_s, connect=10.0),
            "headers": {
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
        }
        if proxy_url is not None:
            kwargs["proxy"] = proxy_url
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _client_for(self, proxy: ProxyEndpoint | None) -> httpx.AsyncClient:
        if proxy is None:
            return self._client
        client = self._proxy_clients.get(proxy.id)
        if client is None:
            client = self._new_client(proxy_url=proxy.url)
            self._proxy_clients[proxy.id] = client
        return client

    async def aclose(self) -> None:
        seen_ids: set[int] = set()
        for client in [self._client, *self._proxy_clients.values()]:
            if id(client) not in seen_ids:
                seen_ids.add(id(client))
                await client.aclose()
        await self.robots.aclose()

    def _finalize_circuit(
        self, domain: str, cb_state: CircuitStateName | None, result: FetchResult
    ) -> FetchResult:
        if self.circuit_breaker is None:
            return result
        if result.ok:
            self.circuit_breaker.record_success(domain)
        elif result.kind == "blocked":
            self.circuit_breaker.record_block(domain, result.error or "HTTP blocked")
        elif cb_state == "half_open":
            self.circuit_breaker.release_probe(domain)
        return result

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        try:
            assert_public_url(url, allow_private=self.settings.allow_private_network)
        except UnsafeUrlError as exc:
            return FetchResult(kind="unsafe", error=str(exc))

        if self.cache is not None and not force_refresh:
            cached = self.cache.get(url, max_age_seconds=self.settings.cache_ttl_hours * 3600)
            if cached is not None:
                return FetchResult(page=cached, status=cached.status_code)

        domain = normalize_domain(url) or urlsplit(url).hostname or "unknown"
        cb_state: CircuitStateName | None = None
        if self.circuit_breaker is not None:
            allowed, cb_state, wait_s = self.circuit_breaker.allow(domain)
            if not allowed:
                wait_display = max(1, int(round(wait_s)))
                return FetchResult(
                    kind="circuit_open",
                    error=f"Circuit breaker відкритий для {domain} (ще {wait_display}с)",
                )

        if self.settings.respect_robots:
            decision = await self.robots.check(url)
            if not decision.allowed:
                return self._finalize_circuit(
                    domain, cb_state, FetchResult(kind="robots", error=decision.reason)
                )
            if decision.crawl_delay:
                self.limiter.set_rps(
                    urlsplit(url).hostname or "unknown", 1.0 / max(decision.crawl_delay, 0.1)
                )

        last_error: str | None = None
        last_blocked: FetchResult | None = None
        for attempt in range(self.settings.max_retries + 1):
            proxy: ProxyEndpoint | None = None
            if self.proxy_provider.pool_size > 0:
                proxy = self.proxy_provider.acquire(domain)
                if proxy is None:
                    if last_blocked is not None:
                        return self._finalize_circuit(domain, cb_state, last_blocked)
                    return self._finalize_circuit(
                        domain,
                        cb_state,
                        FetchResult(kind="transient", error="Усі проксі тимчасово в cooldown"),
                    )

            try:
                result = await self._attempt(
                    url, allow_json=allow_json, domain=domain, proxy=proxy
                )
            except httpx.HTTPError as exc:
                if proxy is not None:
                    self.proxy_provider.report_outcome(proxy.id, domain, "error")
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.settings.max_retries:
                    await asyncio.sleep(min(8.0, 1.5 * (2**attempt)))
                    continue
                return self._finalize_circuit(
                    domain, cb_state, FetchResult(kind="transient", error=last_error)
                )

            if result.kind == "blocked":
                last_blocked = result
                if (
                    self.proxy_provider.pool_size > 0
                    and self.proxy_provider.available_count(domain) > 0
                    and attempt < self.settings.max_retries
                ):
                    logger.info("rotating proxy for %s after block (%s)", url, result.error)
                    continue
                return self._finalize_circuit(domain, cb_state, result)

            if result.kind != "transient" or attempt == self.settings.max_retries:
                return self._finalize_circuit(domain, cb_state, result)

            delay = result.page.headers.get("retry-after") if result.page else None
            sleep_for = (
                float(delay)
                if delay and str(delay).replace(".", "", 1).isdigit()
                else 1.5 * (2**attempt)
            )
            logger.info("retrying %s after %.1fs (%s)", url, sleep_for, result.error)
            await asyncio.sleep(min(15.0, sleep_for))

        return self._finalize_circuit(
            domain, cb_state, last_blocked or FetchResult(kind="transient", error=last_error)
        )

    async def _attempt(
        self,
        url: str,
        *,
        allow_json: bool = False,
        domain: str = "",
        proxy: ProxyEndpoint | None = None,
    ) -> FetchResult:
        current = url
        target_domain = domain or normalize_domain(url) or urlsplit(url).hostname or "unknown"
        req_headers = {"Accept": "application/json, */*;q=0.8"} if allow_json else None
        client = self._client_for(proxy)
        total_bytes = 0

        async with self.limiter.global_sem, self.limiter.domain_slot(url):
            for _hop in range(REDIRECT_LIMIT + 1):
                await self.limiter.wait_turn(current)
                started = time.monotonic()
                try:
                    response = await client.get(current, headers=req_headers)
                except httpx.HTTPError as exc:
                    if proxy is not None:
                        self.proxy_provider.report_outcome(
                            proxy.id, target_domain, "error", bytes_transferred=total_bytes
                        )
                    return FetchResult(kind="transient", error=f"{type(exc).__name__}: {exc}")
                elapsed_ms = int((time.monotonic() - started) * 1000)
                total_bytes += len(response.content)

                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        if proxy is not None:
                            self.proxy_provider.report_outcome(
                                proxy.id, target_domain, "ok", bytes_transferred=total_bytes
                            )
                        return FetchResult(
                            kind="permanent",
                            status=response.status_code,
                            error="redirect without location",
                        )
                    current = urljoin(current, location)
                    try:
                        assert_public_url(
                            current, allow_private=self.settings.allow_private_network
                        )
                    except UnsafeUrlError as exc:
                        if proxy is not None:
                            self.proxy_provider.report_outcome(
                                proxy.id, target_domain, "ok", bytes_transferred=total_bytes
                            )
                        return FetchResult(kind="unsafe", error=str(exc))
                    if self.redirect_guard is not None:
                        blocked = self.redirect_guard(current)
                        if blocked:
                            if proxy is not None:
                                self.proxy_provider.report_outcome(
                                    proxy.id, target_domain, "ok", bytes_transferred=total_bytes
                                )
                            return FetchResult(
                                kind="policy",
                                status=response.status_code,
                                error=f"redirect: {blocked}",
                            )
                    continue

                status_kind = classify_status(response.status_code)
                if status_kind == "blocked":
                    if proxy is not None:
                        self.proxy_provider.report_outcome(
                            proxy.id, target_domain, "blocked", bytes_transferred=total_bytes
                        )
                    return FetchResult(
                        kind="blocked",
                        status=response.status_code,
                        error=f"HTTP {response.status_code} (anti-bot or paywall?)",
                    )
                if status_kind == "transient":
                    if proxy is not None:
                        self.proxy_provider.report_outcome(
                            proxy.id, target_domain, "transient", bytes_transferred=total_bytes
                        )
                    page = PageContent(
                        url=url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        headers={"retry-after": retry_after_seconds(response) or ""},
                    )
                    return FetchResult(
                        page=page,
                        kind="transient",
                        status=response.status_code,
                        error=f"HTTP {response.status_code}",
                    )
                if status_kind == "permanent":
                    if proxy is not None:
                        self.proxy_provider.report_outcome(
                            proxy.id, target_domain, "ok", bytes_transferred=total_bytes
                        )
                    return FetchResult(
                        kind="permanent",
                        status=response.status_code,
                        error=f"HTTP {response.status_code}",
                    )

                content_type = response.headers.get("content-type", "")
                lowered_ct = content_type.lower()
                is_html_xml = "html" in lowered_ct or "xml" in lowered_ct
                is_json = "application/json" in lowered_ct or "+json" in lowered_ct
                if not (is_html_xml or (allow_json and is_json)):
                    if proxy is not None:
                        self.proxy_provider.report_outcome(
                            proxy.id, target_domain, "ok", bytes_transferred=total_bytes
                        )
                    return FetchResult(
                        kind="permanent",
                        status=response.status_code,
                        error=f"unsupported content-type: {content_type or 'unknown'}",
                    )

                raw = response.content[:FETCH_MAX_BYTES]
                body = _decode(raw, response.headers.get("content-type", ""))
                if not body.strip():
                    if proxy is not None:
                        self.proxy_provider.report_outcome(
                            proxy.id, target_domain, "transient", bytes_transferred=total_bytes
                        )
                    return FetchResult(
                        kind="empty", status=response.status_code, error="empty response body"
                    )

                if is_html_xml:
                    ban_reason = detect_ban_marker(body, response.status_code)
                    if ban_reason is not None:
                        if proxy is not None:
                            self.proxy_provider.report_outcome(
                                proxy.id, target_domain, "blocked", bytes_transferred=total_bytes
                            )
                        proxy_note = f" via {redact_proxy_url(proxy.url)}" if proxy else ""
                        return FetchResult(
                            kind="blocked",
                            status=response.status_code,
                            error=f"HTTP {response.status_code}: {ban_reason}{proxy_note}",
                        )

                if is_json and not is_html_xml:
                    page = PageContent(
                        url=url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        html=body,
                        text=body,
                        markdown="",
                        content_hash=hash_text(body),
                        fetched_at=utcnow(),
                        engine=self.name,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        elapsed_ms=elapsed_ms,
                    )
                else:
                    page = self._build_page(
                        url=url,
                        final_url=str(response.url),
                        status=response.status_code,
                        html=body,
                        headers=dict(response.headers),
                        elapsed_ms=elapsed_ms,
                    )
                if proxy is not None:
                    self.proxy_provider.report_outcome(
                        proxy.id, target_domain, "ok", bytes_transferred=total_bytes
                    )
                if self.cache is not None:
                    self.cache.put(page)
                return FetchResult(page=page, status=response.status_code)

        if proxy is not None:
            self.proxy_provider.report_outcome(
                proxy.id, target_domain, "error", bytes_transferred=total_bytes
            )
        return FetchResult(kind="permanent", error="too many redirects")

    def _build_page(
        self,
        *,
        url: str,
        final_url: str,
        status: int,
        html: str,
        headers: dict[str, str],
        elapsed_ms: int = 0,
    ) -> PageContent:
        return PageContent(
            url=url,
            final_url=final_url,
            status_code=status,
            html=html,
            text=html_to_text(html),
            markdown=html_to_markdown(html, final_url),
            content_hash=hash_text(html),
            fetched_at=utcnow(),
            engine=self.name,
            headers={key.lower(): value for key, value in headers.items()},
            elapsed_ms=elapsed_ms,
        )


class PlaywrightFetcher(HttpFetcher):
    """Optional JS-rendering fetcher (``pip install playwright && playwright install chromium``)."""

    name = "playwright"

    def __init__(self, settings: Settings, **kwargs) -> None:
        super().__init__(settings, **kwargs)
        try:
            import playwright.async_api  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Playwright is not installed. Install with: pip install playwright && playwright install chromium"
            ) from exc
        self._browser = None

    async def _attempt(
        self,
        url: str,
        *,
        allow_json: bool = False,
        domain: str = "",
        proxy: ProxyEndpoint | None = None,
    ) -> FetchResult:  # pragma: no cover - needs a browser
        from playwright.async_api import async_playwright

        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        page = await self._browser.new_page(user_agent=self.settings.user_agent)
        try:
            response = await page.goto(
                url, timeout=self.settings.request_timeout_s * 1000, wait_until="domcontentloaded"
            )
            await page.wait_for_timeout(700)
            html = await page.content()
            built = self._build_page(
                url=url,
                final_url=page.url,
                status=response.status if response else 200,
                html=html,
                headers=dict(response.headers) if response else {},
            )
            if self.cache is not None:
                self.cache.put(built)
            return FetchResult(page=built, status=built.status_code)
        except Exception as exc:  # pragma: no cover
            return FetchResult(kind="transient", error=f"{type(exc).__name__}: {exc}")
        finally:
            await page.close()

    async def aclose(self) -> None:  # pragma: no cover
        if self._browser is not None:
            await self._browser.close()
        await super().aclose()


def build_fetcher(
    settings: Settings,
    *,
    cache: RawCache | None = None,
    robots: RobotsGate | None = None,
    limiter: DomainRateLimiter | None = None,
    proxy_provider: ProxyProvider | None = None,
    circuit_breaker: DomainCircuitBreaker | None = None,
) -> Fetcher:
    if settings.fetch_engine == "playwright":
        try:
            return PlaywrightFetcher(
                settings,
                cache=cache,
                robots=robots,
                limiter=limiter,
                proxy_provider=proxy_provider,
                circuit_breaker=circuit_breaker,
            )
        except RuntimeError as exc:
            logger.warning("%s — falling back to plain HTTP", exc)
    return HttpFetcher(
        settings,
        cache=cache,
        robots=robots,
        limiter=limiter,
        proxy_provider=proxy_provider,
        circuit_breaker=circuit_breaker,
    )


def _decode(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=")[-1].split(";")[0].strip().strip('"') or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def normalize_target(url: str) -> str:
    return canonical_url(url)
