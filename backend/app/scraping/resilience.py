"""Crawler resilience primitives: proxy pool (sticky/health/cost), ban detection, domain circuit breaker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

from ..utils import normalize_domain

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings

ProxyStrategy = Literal["sticky_domain", "round_robin", "health_first"]
ProxyOutcome = Literal["ok", "blocked", "transient", "error"]
CircuitStateName = Literal["closed", "open", "half_open"]

_STRONG_BAN_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform",
    "<title>just a moment...</title>",
    "attention required! | cloudflare",
    "captcha-delivery.com",
    "px-captcha",
    "datadome",
    "unusual traffic from your computer",
    "please verify you are a human",
    "verify you are human",
)
_WIDGET_BAN_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "turnstile.cloudflare.com",
)


def redact_proxy_url(raw_url: str) -> str:
    """Strip any username/password from a proxy URL for logs, API responses, and UI."""
    try:
        parts = urlsplit(raw_url.strip())
    except ValueError:
        return "[invalid-proxy]"
    host = parts.hostname or ""
    if not host:
        return "[invalid-proxy]"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme or "http", netloc, "", "", ""))


def detect_ban_marker(body: str, status_code: int = 200) -> str | None:
    """Heuristically detect anti-bot/CAPTCHA challenge pages without false positives on real sites."""
    if not body:
        return None
    if len(body) > 65_536 and status_code not in {401, 403, 407, 429, 451, 503}:
        return None
    lowered = body.lower()
    for marker in _STRONG_BAN_MARKERS:
        if marker in lowered:
            return f"anti-bot challenge ({marker})"
    if len(body) < 16_000:
        for marker in _WIDGET_BAN_MARKERS:
            if marker in lowered:
                return f"captcha gate ({marker})"
    return None


@dataclass(slots=True)
class ProxyEndpoint:
    id: str
    url: str
    redacted_url: str
    scheme: str
    host: str
    port: int | None
    health_score: float = 1.0
    requests: int = 0
    successes: int = 0
    blocks: int = 0
    failures: int = 0
    consecutive_blocks: int = 0
    bytes_transferred: int = 0
    cost_usd: float = 0.0
    cooldown_until: float | None = None


class ProxyProvider(Protocol):
    strategy: ProxyStrategy
    pool_size: int

    def acquire(self, domain: str) -> ProxyEndpoint | None: ...

    def report_outcome(
        self,
        proxy_id: str,
        domain: str,
        outcome: ProxyOutcome,
        *,
        bytes_transferred: int = 0,
    ) -> None: ...

    def available_count(self, domain: str = "") -> int: ...

    def snapshot(self) -> dict[str, Any]: ...


class NoopProxyProvider:
    strategy: ProxyStrategy = "sticky_domain"
    pool_size: int = 0

    def acquire(self, domain: str) -> ProxyEndpoint | None:
        return None

    def report_outcome(
        self,
        proxy_id: str,
        domain: str,
        outcome: ProxyOutcome,
        *,
        bytes_transferred: int = 0,
    ) -> None:
        return None

    def available_count(self, domain: str = "") -> int:
        return 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "pool_size": 0,
            "healthy_count": 0,
            "total_bytes": 0,
            "total_cost_usd": 0.0,
            "proxies": [],
        }


class PoolProxyProvider:
    """In-memory proxy pool with sticky-domain pinning, health scoring, cooldowns, and byte cost accounting."""

    def __init__(
        self,
        urls: list[str],
        *,
        strategy: ProxyStrategy = "sticky_domain",
        sticky_ttl_s: float = 600.0,
        cooldown_s: float = 60.0,
        cost_per_gb_usd: float = 0.0,
        ban_cooldown_after: int = 2,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.strategy: ProxyStrategy = strategy
        self.sticky_ttl_s = max(1.0, sticky_ttl_s)
        self.cooldown_s = max(1.0, cooldown_s)
        self.cost_per_gb_usd = max(0.0, cost_per_gb_usd)
        self.ban_cooldown_after = max(1, ban_cooldown_after)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._cursor = 0
        # domain -> (proxy_id, expires_at)
        self._sticky: dict[str, tuple[str, float]] = {}
        self._endpoints: list[ProxyEndpoint] = []
        self._by_id: dict[str, ProxyEndpoint] = {}

        seen_urls: set[str] = set()
        for idx, raw in enumerate(urls, start=1):
            cleaned = (raw or "").strip()
            if not cleaned or cleaned in seen_urls:
                continue
            parts = urlsplit(cleaned)
            if parts.scheme not in {"http", "https", "socks5", "socks5h"} or not parts.hostname:
                continue
            seen_urls.add(cleaned)
            proxy_id = f"proxy_{idx}"
            ep = ProxyEndpoint(
                id=proxy_id,
                url=cleaned,
                redacted_url=redact_proxy_url(cleaned),
                scheme=parts.scheme,
                host=parts.hostname,
                port=parts.port,
            )
            self._endpoints.append(ep)
            self._by_id[proxy_id] = ep

    @property
    def pool_size(self) -> int:
        return len(self._endpoints)

    def _eligible_locked(self, now: float) -> list[ProxyEndpoint]:
        eligible: list[ProxyEndpoint] = []
        for ep in self._endpoints:
            if ep.cooldown_until is not None and ep.cooldown_until <= now:
                ep.cooldown_until = None
                ep.consecutive_blocks = 0
                ep.health_score = max(ep.health_score, 0.5)
            if ep.cooldown_until is None:
                eligible.append(ep)
        return eligible

    def available_count(self, domain: str = "") -> int:
        now = self._clock()
        with self._lock:
            return len(self._eligible_locked(now))

    def acquire(self, domain: str) -> ProxyEndpoint | None:
        norm = normalize_domain(domain) or domain.strip().lower() or "default"
        now = self._clock()
        with self._lock:
            eligible = self._eligible_locked(now)
            if not eligible:
                return None
            eligible_ids = {ep.id: ep for ep in eligible}

            if self.strategy == "sticky_domain":
                sticky = self._sticky.get(norm)
                if sticky is not None:
                    pid, expires_at = sticky
                    ep = eligible_ids.get(pid)
                    if ep is not None and expires_at > now and ep.health_score >= 0.3:
                        return ep
                    self._sticky.pop(norm, None)
                chosen = self._pick_round_or_health_locked(eligible)
                self._sticky[norm] = (chosen.id, now + self.sticky_ttl_s)
                return chosen

            if self.strategy == "health_first":
                return max(eligible, key=lambda ep: (ep.health_score, -ep.requests))

            return self._pick_round_robin_locked(eligible)

    def _pick_round_robin_locked(self, eligible: list[ProxyEndpoint]) -> ProxyEndpoint:
        chosen = eligible[self._cursor % len(eligible)]
        self._cursor += 1
        return chosen

    def _pick_round_or_health_locked(self, eligible: list[ProxyEndpoint]) -> ProxyEndpoint:
        best_score = max(ep.health_score for ep in eligible)
        top = [ep for ep in eligible if ep.health_score >= best_score - 0.15]
        return self._pick_round_robin_locked(top)

    def report_outcome(
        self,
        proxy_id: str,
        domain: str,
        outcome: ProxyOutcome,
        *,
        bytes_transferred: int = 0,
    ) -> None:
        norm = normalize_domain(domain) or domain.strip().lower() or "default"
        now = self._clock()
        with self._lock:
            ep = self._by_id.get(proxy_id)
            if ep is None:
                return
            ep.requests += 1
            safe_bytes = max(0, int(bytes_transferred))
            ep.bytes_transferred += safe_bytes
            if safe_bytes > 0 and self.cost_per_gb_usd > 0.0:
                ep.cost_usd += (safe_bytes / float(1024**3)) * self.cost_per_gb_usd

            if outcome == "ok":
                ep.successes += 1
                ep.consecutive_blocks = 0
                ep.health_score = round(min(1.0, ep.health_score + 0.1), 4)
            elif outcome == "blocked":
                ep.blocks += 1
                ep.failures += 1
                ep.consecutive_blocks += 1
                ep.health_score = round(max(0.05, ep.health_score - 0.35), 4)
                sticky = self._sticky.get(norm)
                if sticky is not None and sticky[0] == proxy_id:
                    self._sticky.pop(norm, None)
                if ep.consecutive_blocks >= self.ban_cooldown_after:
                    ep.cooldown_until = now + self.cooldown_s
            else:
                ep.failures += 1
                ep.health_score = round(max(0.05, ep.health_score - 0.15), 4)
                if outcome == "error":
                    ep.consecutive_blocks += 1
                    sticky = self._sticky.get(norm)
                    if sticky is not None and sticky[0] == proxy_id:
                        self._sticky.pop(norm, None)
                    if ep.consecutive_blocks >= self.ban_cooldown_after:
                        ep.cooldown_until = now + self.cooldown_s

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            proxies = []
            healthy_count = 0
            total_bytes = 0
            total_cost = 0.0
            for ep in self._endpoints:
                in_cooldown = ep.cooldown_until is not None and ep.cooldown_until > now
                if ep.cooldown_until is not None and ep.cooldown_until <= now:
                    ep.cooldown_until = None
                    ep.consecutive_blocks = 0
                if not in_cooldown and ep.health_score >= 0.3:
                    healthy_count += 1
                total_bytes += ep.bytes_transferred
                total_cost += ep.cost_usd
                proxies.append(
                    {
                        "id": ep.id,
                        "url": ep.redacted_url,
                        "scheme": ep.scheme,
                        "host": ep.host,
                        "port": ep.port,
                        "health_score": round(ep.health_score, 3),
                        "in_cooldown": in_cooldown,
                        "cooldown_remaining_s": round(
                            max(0.0, (ep.cooldown_until or 0.0) - now), 1
                        )
                        if in_cooldown
                        else 0.0,
                        "requests": ep.requests,
                        "successes": ep.successes,
                        "blocks": ep.blocks,
                        "failures": ep.failures,
                        "bytes_transferred": ep.bytes_transferred,
                        "cost_usd": round(ep.cost_usd, 6),
                    }
                )
            return {
                "strategy": self.strategy,
                "pool_size": len(self._endpoints),
                "healthy_count": healthy_count,
                "total_bytes": total_bytes,
                "total_cost_usd": round(total_cost, 6),
                "proxies": proxies,
            }


def build_proxy_provider(settings: Settings) -> ProxyProvider:
    raw = str(getattr(settings, "proxy_urls", "") or "")
    urls = [
        item.strip()
        for chunk in raw.replace("\n", ",").replace(";", ",").split(",")
        for item in [chunk.strip()]
        if item
    ]
    if not urls:
        return NoopProxyProvider()
    return PoolProxyProvider(
        urls,
        strategy=getattr(settings, "proxy_strategy", "sticky_domain"),
        sticky_ttl_s=float(getattr(settings, "proxy_sticky_ttl_s", 600.0)),
        cooldown_s=float(getattr(settings, "proxy_cooldown_s", 60.0)),
        cost_per_gb_usd=float(getattr(settings, "proxy_cost_per_gb_usd", 0.0)),
    )


class DomainCircuitStatus(BaseModel):
    domain: str
    state: CircuitStateName = "closed"
    consecutive_blocks: int = 0
    total_blocks: int = 0
    total_successes: int = 0
    total_requests: int = 0
    last_reason: str | None = None
    cooldown_remaining_s: float = 0.0


@dataclass(slots=True)
class _CircuitEntry:
    domain: str
    state: CircuitStateName = "closed"
    consecutive_blocks: int = 0
    total_blocks: int = 0
    total_successes: int = 0
    total_requests: int = 0
    last_reason: str | None = None
    opened_at: float | None = None
    cooldown_until: float | None = None
    probe_in_flight: bool = False


class DomainCircuitBreaker:
    """Per-domain circuit breaker: closes on success, opens after N consecutive blocks."""

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_s: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_s = max(1.0, float(cooldown_s))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, _CircuitEntry] = {}
        self._total_rejected = 0

    @property
    def total_rejected(self) -> int:
        with self._lock:
            return self._total_rejected

    @staticmethod
    def _norm(domain: str) -> str:
        return normalize_domain(domain) or domain.strip().lower() or "unknown"

    def _get_locked(self, norm: str) -> _CircuitEntry:
        entry = self._entries.get(norm)
        if entry is None:
            entry = _CircuitEntry(domain=norm)
            self._entries[norm] = entry
        return entry

    def _to_status_locked(self, entry: _CircuitEntry, now: float) -> DomainCircuitStatus:
        remaining = 0.0
        if entry.state == "open" and entry.cooldown_until is not None:
            remaining = max(0.0, entry.cooldown_until - now)
        return DomainCircuitStatus(
            domain=entry.domain,
            state=entry.state,
            consecutive_blocks=entry.consecutive_blocks,
            total_blocks=entry.total_blocks,
            total_successes=entry.total_successes,
            total_requests=entry.total_requests,
            last_reason=entry.last_reason,
            cooldown_remaining_s=round(remaining, 1),
        )

    def allow(self, domain: str) -> tuple[bool, CircuitStateName, float]:
        norm = self._norm(domain)
        now = self._clock()
        with self._lock:
            entry = self._get_locked(norm)
            if entry.state == "closed":
                entry.total_requests += 1
                return True, "closed", 0.0
            if entry.state == "open":
                if entry.cooldown_until is not None and now < entry.cooldown_until:
                    self._total_rejected += 1
                    return False, "open", max(0.0, entry.cooldown_until - now)
                entry.state = "half_open"
                entry.probe_in_flight = True
                entry.total_requests += 1
                return True, "half_open", 0.0
            # half_open
            if entry.probe_in_flight:
                self._total_rejected += 1
                return False, "half_open", 1.0
            entry.probe_in_flight = True
            entry.total_requests += 1
            return True, "half_open", 0.0

    def release_probe(self, domain: str) -> None:
        norm = self._norm(domain)
        with self._lock:
            entry = self._entries.get(norm)
            if entry is not None:
                entry.probe_in_flight = False

    def record_block(self, domain: str, reason: str = "") -> tuple[bool, DomainCircuitStatus]:
        norm = self._norm(domain)
        now = self._clock()
        with self._lock:
            entry = self._get_locked(norm)
            prev_state = entry.state
            entry.consecutive_blocks += 1
            entry.total_blocks += 1
            entry.probe_in_flight = False
            if reason:
                entry.last_reason = reason[:240]
            just_opened = False
            if prev_state == "half_open" or entry.consecutive_blocks >= self.threshold:
                entry.state = "open"
                entry.opened_at = now
                entry.cooldown_until = now + self.cooldown_s
                just_opened = prev_state != "open"
            return just_opened, self._to_status_locked(entry, now)

    def record_success(self, domain: str) -> tuple[bool, DomainCircuitStatus]:
        norm = self._norm(domain)
        now = self._clock()
        with self._lock:
            entry = self._get_locked(norm)
            was_open = entry.state != "closed"
            entry.state = "closed"
            entry.consecutive_blocks = 0
            entry.total_successes += 1
            entry.opened_at = None
            entry.cooldown_until = None
            entry.probe_in_flight = False
            return was_open, self._to_status_locked(entry, now)

    def reset(self, domain: str) -> DomainCircuitStatus:
        norm = self._norm(domain)
        now = self._clock()
        with self._lock:
            entry = self._get_locked(norm)
            entry.state = "closed"
            entry.consecutive_blocks = 0
            entry.opened_at = None
            entry.cooldown_until = None
            entry.probe_in_flight = False
            return self._to_status_locked(entry, now)

    def status(self, domain: str) -> DomainCircuitStatus:
        norm = self._norm(domain)
        now = self._clock()
        with self._lock:
            entry = self._get_locked(norm)
            return self._to_status_locked(entry, now)

    def open_count(self) -> int:
        now = self._clock()
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if entry.state == "open"
                and (entry.cooldown_until is None or entry.cooldown_until > now)
            )

    def snapshot(self) -> list[DomainCircuitStatus]:
        now = self._clock()
        with self._lock:
            return [
                self._to_status_locked(entry, now)
                for entry in sorted(self._entries.values(), key=lambda item: item.domain)
            ]
