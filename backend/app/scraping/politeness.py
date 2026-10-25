"""Politeness primitives: robots.txt, per-domain rate limiting, retry classification."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..utils import normalize_domain, utcnow

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
BLOCKED_STATUS = {401, 403, 407, 451}


class RobotoDisallowedError(RuntimeError):
    def __init__(self, url: str, reason: str = "disallowed by robots.txt") -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url


@dataclass(slots=True)
class RobotsDecision:
    allowed: bool
    reason: str
    crawl_delay: float | None = None


class RobotsGate:
    """Caches robots.txt per host and answers ``can_fetch`` for our user agent.

    RFC 9309 semantics: 404/410 => allow all, 5xx/unreachable => treat as disallow.
    """

    def __init__(self, settings, *, ttl_seconds: int = 6 * 3600) -> None:
        self.settings = settings
        self.ttl = timedelta(seconds=ttl_seconds)
        self._cache: dict[str, tuple[datetime, RobotFileParser | None, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = threading.Lock()
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=min(10.0, self.settings.request_timeout_s),
                headers={"User-Agent": self.settings.user_agent},
            )
        return self._client

    def _domain_lock(self, domain: str) -> asyncio.Lock:
        with self._lock:
            return self._locks.setdefault(domain, asyncio.Lock())

    async def check(self, url: str) -> RobotsDecision:
        if not self.settings.respect_robots:
            return RobotsDecision(True, "robots disabled by configuration")
        parts = urlsplit(url)
        domain = parts.netloc.lower()
        if not domain:
            return RobotsDecision(False, "invalid url")
        cached = self._cache.get(domain)
        if cached and utcnow() - cached[0] < self.ttl:
            return self._decision(cached[1], cached[2], url)

        async with self._domain_lock(domain):
            cached = self._cache.get(domain)
            if cached and utcnow() - cached[0] < self.ttl:
                return self._decision(cached[1], cached[2], url)
            parser, reason = await self._load(f"{parts.scheme}://{parts.netloc}")
            self._cache[domain] = (utcnow(), parser, reason)
            return self._decision(parser, reason, url)

    def _decision(self, parser: RobotFileParser | None, reason: str, url: str) -> RobotsDecision:
        if parser is None:
            return RobotsDecision(False, reason)
        allowed = parser.can_fetch(self.settings.user_agent, url)
        delay = None
        try:
            delay = parser.crawl_delay(self.settings.user_agent)
        except Exception:  # pragma: no cover - stdlib quirk
            delay = None
        return RobotsDecision(allowed, "allowed" if allowed else "disallowed by robots.txt",
                              float(delay) if delay else None)

    async def _load(self, origin: str) -> tuple[RobotFileParser | None, str]:
        url = origin + "/robots.txt"
        try:
            response = await self._get_client().get(url)
        except httpx.HTTPError as exc:
            logger.warning("robots.txt unreachable for %s: %s", origin, exc)
            return None, f"robots.txt unreachable ({type(exc).__name__}) — treated as disallow"
        if response.status_code in (404, 410):
            parser = RobotFileParser()
            parser.parse([])
            return parser, "robots.txt missing — allow"
        if response.status_code >= 500:
            return None, f"robots.txt server error {response.status_code} — treated as disallow"
        if response.status_code >= 400:
            parser = RobotFileParser()
            parser.parse([])
            return parser, f"robots.txt status {response.status_code} — allow"
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser, "robots.txt loaded"

    async def crawl_delay(self, url: str, default: float) -> float:
        decision = await self.check(url)
        if decision.crawl_delay:
            return max(default, decision.crawl_delay)
        return default


class DomainRateLimiter:
    """One in-flight request per domain plus a minimum interval between requests."""

    def __init__(self, *, max_concurrency: int, per_domain_concurrency: int, default_rps: float) -> None:
        self.global_sem = asyncio.Semaphore(max_concurrency)
        self.per_domain = per_domain_concurrency
        self.default_rps = default_rps
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._last_request: dict[str, float] = {}
        self._rps: dict[str, float] = {}
        self._guard = threading.Lock()

    def set_rps(self, domain: str, rps: float) -> None:
        self._rps[domain] = max(0.05, rps)

    def _sem(self, domain: str) -> asyncio.Semaphore:
        with self._guard:
            return self._semaphores.setdefault(domain, asyncio.Semaphore(self.per_domain))

    def _lock(self, domain: str) -> asyncio.Lock:
        with self._guard:
            return self._locks.setdefault(domain, asyncio.Lock())

    async def wait_turn(self, url: str) -> None:
        domain = normalize_domain(url) or "unknown"
        rps = self._rps.get(domain, self.default_rps)
        interval = 1.0 / rps if rps > 0 else 0.0
        async with self._lock(domain):
            last = self._last_request.get(domain, 0.0)
            wait = last + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[domain] = time.monotonic()

    def domain_slot(self, url: str) -> asyncio.Semaphore:
        return self._sem(normalize_domain(url) or "unknown")


def classify_status(status: int) -> str:
    if status in BLOCKED_STATUS:
        return "blocked"
    if status in RETRYABLE_STATUS:
        return "transient"
    if status >= 500:
        return "transient"
    if status >= 400:
        return "permanent"
    return "ok"


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(raw)
        if target.tzinfo is None:
            target = target.replace(tzinfo=utcnow().tzinfo)
        return max(0.0, (target - utcnow()).total_seconds())
    except (TypeError, ValueError):
        return None
