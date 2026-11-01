"""Compliance enforcement (P1-04, docs/00 §«бар'єри», docs/02 §2).

The source registry used to be advisory. Now it is enforced in two places:

1. **Run gate** — before a calibration or collection starts, every seed domain is checked
   against the live registry: ``forbidden`` is a hard stop, ``high`` needs an explicit
   acknowledgement with a reason, which is written to the audit log.
2. **Fetch guard** — every request (crawl links, enrichment, domain resolution) passes through
   :class:`PolicyGuardFetcher`, so a forbidden domain cannot slip in through a side door.

Logged-out only: URLs carrying credentials (``user:pass@``) or session/auth tokens in the query
string are rejected. Collecting behind a login turns public-data scraping into a ToS / access
violation (hiQ v. LinkedIn, Meta v. Bright Data), which the product does not do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlsplit

from .models import SourcePolicy
from .utils import normalize_domain

if TYPE_CHECKING:  # pragma: no cover
    from .db.sqlite_store import OperationalStore
    from .scraping.fetcher import Fetcher, FetchResult

RiskLevel = Literal["low", "medium", "high", "forbidden", "unknown"]

AUTH_QUERY_KEYS = frozenset(
    {
        "token", "access_token", "auth", "auth_token", "authorization", "api_key", "apikey",
        "key", "session", "sessionid", "session_id", "sid", "phpsessid", "jsessionid",
        "password", "passwd", "pwd", "secret", "signature", "sig", "jwt", "bearer",
    }
)


class ComplianceError(RuntimeError):
    """A run or URL violates the source policy."""

    def __init__(
        self,
        code: Literal["forbidden", "needs_ack", "login_required"],
        message: str,
        *,
        domains: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.domains = domains or []


@dataclass(slots=True)
class GateDecision:
    """Outcome of :func:`check_run`; ``acknowledged`` lists high-risk domains let through."""

    policies: dict[str, SourcePolicy] = field(default_factory=dict)
    acknowledged: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# logged-out rule
# ---------------------------------------------------------------------------


def login_violation(url: str) -> str | None:
    """Return a reason if the URL carries credentials or an auth/session token."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.username or parts.password:
        return "URL містить логін/пароль"
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if key.strip().lower() in AUTH_QUERY_KEYS and value:
            return f"URL містить токен доступу або сесії (параметр «{key}»)"
    return None


def assert_logged_out(url: str) -> None:
    reason = login_violation(url)
    if reason:
        raise ComplianceError(
            "login_required",
            f"{reason}. Збираємо лише публічні сторінки без авторизації.",
            domains=[normalize_domain(url)],
        )


# ---------------------------------------------------------------------------
# registry lookups
# ---------------------------------------------------------------------------


def domain_chain(domain: str) -> list[str]:
    """``a.b.example.com`` → ``[a.b.example.com, b.example.com, example.com]``."""
    labels = [label for label in domain.split(".") if label]
    return [".".join(labels[index:]) for index in range(len(labels) - 1)] or [domain]


def effective_policy(store: OperationalStore, url_or_domain: str) -> SourcePolicy | None:
    """The most specific registry entry for a host; a parent domain's policy covers subdomains."""
    domain = normalize_domain(url_or_domain)
    if not domain:
        return None
    for candidate in domain_chain(domain):
        policy = store.get_source_policy(candidate)
        if policy is not None and policy.tos_risk != "unknown":
            return policy
    return store.get_source_policy(domain)


def check_run(
    store: OperationalStore,
    urls: list[str],
    *,
    acknowledge_high_risk: bool = False,
    reason: str = "",
) -> GateDecision:
    """Gate a calibration/collection over ``urls`` (seed URLs of the task)."""
    decision = GateDecision()
    forbidden: list[str] = []
    high: list[str] = []
    for url in urls:
        assert_logged_out(url)
        policy = effective_policy(store, url)
        domain = normalize_domain(url)
        if policy is None or not domain:
            continue
        decision.policies[domain] = policy
        if policy.tos_risk == "forbidden":
            forbidden.append(domain)
        elif policy.tos_risk == "high":
            high.append(domain)

    if forbidden:
        raise ComplianceError(
            "forbidden",
            "Джерело заборонене в реєстрі джерел (tos_risk=forbidden): "
            + ", ".join(sorted(set(forbidden)))
            + ". Змініть політику джерела або приберіть seed.",
            domains=sorted(set(forbidden)),
        )
    if high:
        if not acknowledge_high_risk or len(reason.strip()) < 10:
            raise ComplianceError(
                "needs_ack",
                "Джерело з високим ризиком ToS: "
                + ", ".join(sorted(set(high)))
                + ". Підтвердіть запуск і вкажіть причину (мінімум 10 символів) — "
                "це буде записано в журнал аудиту.",
                domains=sorted(set(high)),
            )
        decision.acknowledged = sorted(set(high))
    return decision


# ---------------------------------------------------------------------------
# fetch guard
# ---------------------------------------------------------------------------


class PolicyGuardFetcher:
    """Wraps any Fetcher and refuses requests to forbidden or logged-in URLs."""

    def __init__(self, inner: Fetcher, store: OperationalStore) -> None:
        self.inner = inner
        self.store = store
        self.name = getattr(inner, "name", "http")

    def __getattr__(self, item: str):  # delegate extras (calls, cache, …) to the wrapped fetcher
        return getattr(self.inner, item)

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        from .scraping.fetcher import FetchResult

        reason = login_violation(url)
        if reason:
            return FetchResult(kind="policy", error=f"logged-out policy: {reason}")
        policy = effective_policy(self.store, url)
        if policy is not None and policy.tos_risk == "forbidden":
            return FetchResult(
                kind="policy", error=f"source registry: {policy.domain} is forbidden"
            )
        return await self.inner.fetch(url, force_refresh=force_refresh)

    async def aclose(self) -> None:
        await self.inner.aclose()
