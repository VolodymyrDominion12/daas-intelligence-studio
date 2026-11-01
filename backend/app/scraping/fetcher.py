"""Fetching: HTTP by default, optional Playwright; robots + rate limits + SSRF guard."""

from __future__ import annotations

import asyncio
import logging
import time
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
    utcnow,
)
from .page import html_to_markdown, html_to_text
from .politeness import (
    DomainRateLimiter,
    RobotsGate,
    classify_status,
    retry_after_seconds,
)

logger = logging.getLogger(__name__)

FETCH_MAX_BYTES = 6 * 1024 * 1024
REDIRECT_LIMIT = 4
ErrorKind = Literal["transient", "blocked", "permanent", "robots", "unsafe", "disabled", "empty"]


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

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult: ...

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
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.robots = robots or RobotsGate(settings)
        self.limiter = limiter or DomainRateLimiter(
            max_concurrency=settings.max_concurrency,
            per_domain_concurrency=settings.per_domain_concurrency,
            default_rps=settings.per_domain_rps,
        )
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s, connect=10.0),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()
        await self.robots.aclose()

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        try:
            assert_public_url(url, allow_private=self.settings.allow_private_network)
        except UnsafeUrlError as exc:
            return FetchResult(kind="unsafe", error=str(exc))

        if self.cache is not None and not force_refresh:
            cached = self.cache.get(url, max_age_seconds=self.settings.cache_ttl_hours * 3600)
            if cached is not None:
                return FetchResult(page=cached, status=cached.status_code)

        if self.settings.respect_robots:
            decision = await self.robots.check(url)
            if not decision.allowed:
                return FetchResult(kind="robots", error=decision.reason)
            if decision.crawl_delay:
                self.limiter.set_rps(
                    urlsplit(url).hostname or "unknown", 1.0 / max(decision.crawl_delay, 0.1)
                )

        last_error: str | None = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                result = await self._attempt(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.settings.max_retries:
                    await asyncio.sleep(min(8.0, 1.5 * (2**attempt)))
                    continue
                return FetchResult(kind="transient", error=last_error)

            if result.kind != "transient" or attempt == self.settings.max_retries:
                return result
            delay = result.page.headers.get("retry-after") if result.page else None
            sleep_for = float(delay) if delay and str(delay).replace(".", "", 1).isdigit() else 1.5 * (2**attempt)
            logger.info("retrying %s after %.1fs (%s)", url, sleep_for, result.error)
            await asyncio.sleep(min(15.0, sleep_for))
        return FetchResult(kind="transient", error=last_error)

    async def _attempt(self, url: str) -> FetchResult:
        current = url
        async with self.limiter.global_sem, self.limiter.domain_slot(url):
            for _hop in range(REDIRECT_LIMIT + 1):
                await self.limiter.wait_turn(current)
                started = time.monotonic()
                try:
                    response = await self._client.get(current)
                except httpx.HTTPError as exc:
                    return FetchResult(kind="transient", error=f"{type(exc).__name__}: {exc}")
                elapsed_ms = int((time.monotonic() - started) * 1000)

                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        return FetchResult(kind="permanent", status=response.status_code,
                                           error="redirect without location")
                    current = urljoin(current, location)
                    try:
                        assert_public_url(
                            current, allow_private=self.settings.allow_private_network
                        )
                    except UnsafeUrlError as exc:
                        return FetchResult(kind="unsafe", error=str(exc))
                    continue

                status_kind = classify_status(response.status_code)
                if status_kind == "blocked":
                    return FetchResult(kind="blocked", status=response.status_code,
                                       error=f"HTTP {response.status_code} (anti-bot or paywall?)")
                if status_kind == "transient":
                    page = PageContent(
                        url=url,
                        final_url=str(response.url),
                        status_code=response.status_code,
                        headers={"retry-after": retry_after_seconds(response) or ""},
                    )
                    return FetchResult(
                        page=page, kind="transient", status=response.status_code,
                        error=f"HTTP {response.status_code}",
                    )
                if status_kind == "permanent":
                    return FetchResult(kind="permanent", status=response.status_code,
                                       error=f"HTTP {response.status_code}")

                content_type = response.headers.get("content-type", "")
                if "html" not in content_type and "xml" not in content_type:
                    return FetchResult(
                        kind="permanent",
                        status=response.status_code,
                        error=f"unsupported content-type: {content_type or 'unknown'}",
                    )

                raw = response.content[:FETCH_MAX_BYTES]
                html = _decode(raw, response.headers.get("content-type", ""))
                if not html.strip():
                    return FetchResult(kind="empty", status=response.status_code,
                                       error="empty response body")
                page = self._build_page(
                    url=url,
                    final_url=str(response.url),
                    status=response.status_code,
                    html=html,
                    headers=dict(response.headers),
                    elapsed_ms=elapsed_ms,
                )
                if self.cache is not None:
                    self.cache.put(page)
                return FetchResult(page=page, status=response.status_code)
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

    async def _attempt(self, url: str) -> FetchResult:  # pragma: no cover - needs a browser
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
) -> Fetcher:
    if settings.fetch_engine == "playwright":
        try:
            return PlaywrightFetcher(settings, cache=cache, robots=robots, limiter=limiter)
        except RuntimeError as exc:
            logger.warning("%s — falling back to plain HTTP", exc)
    return HttpFetcher(settings, cache=cache, robots=robots, limiter=limiter)


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
