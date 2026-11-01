"""Compliance enforcement (P1-04, docs/00 §«бар'єри», docs/02 §2).

The source registry used to be advisory. Now it is enforced in two layers:

1. **Run gate** (:func:`check_run`) — before a calibration or collection starts, every seed is
   checked against the live registry: ``forbidden`` is a hard stop, ``high`` needs an explicit
   acknowledgement with a reason, written to the audit log together with the run.
2. **Fetch guard** (:class:`PolicyGuardFetcher`) — *every* request, including redirect hops,
   crawl links and enrichment fetches, is re-checked against the live registry at fetch time.
   High-risk domains pass only if they were acknowledged for the current run (see
   :func:`run_scope`). This also covers seeds or policies changed after the gate passed.

Logged-out only: URLs carrying credentials (``user:pass@``) or auth/session tokens in the query
string are never requested and never stored. Collecting behind a login turns public-data
scraping into a ToS / access violation (hiQ v. LinkedIn, Meta v. Bright Data).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import SourcePolicy
from .utils import normalize_domain

if TYPE_CHECKING:  # pragma: no cover
    from .db.sqlite_store import OperationalStore
    from .scraping.fetcher import Fetcher, FetchResult

logger = logging.getLogger(__name__)

RiskLevel = Literal["low", "medium", "high", "forbidden", "unknown"]

# Exact query keys that carry credentials. Generic names such as ``key`` or ``sig`` are left out on
# purpose: public catalogs use them as filters (``?key=price``).
AUTH_QUERY_KEYS = frozenset(
    {
        "token", "access_token", "refresh_token", "id_token", "oauth_token", "oauth_verifier",
        "auth", "auth_token", "authorization", "bearer", "jwt", "api_key", "apikey",
        "client_secret", "session", "sessionid", "session_id", "session_token", "sid",
        "phpsessid", "jsessionid", "aspsessionid", "cfid", "cftoken", "password", "passwd",
        "pwd", "secret", "signature", "x_amz_signature", "x_amz_security_token",
        "x_goog_signature", "x_goog_credential",
    }
)
# Pagination cursors look like tokens but are public.
_PUBLIC_TOKEN_HINTS = ("page", "next", "cursor", "continuation", "csrf")
_SECRET_KEY_HINTS = ("api", "access", "secret", "private", "auth")


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


def is_auth_key(key: str) -> bool:
    name = key.strip().lower().replace("-", "_")
    if name in AUTH_QUERY_KEYS:
        return True
    if any(hint in name for hint in _PUBLIC_TOKEN_HINTS):
        return False
    if name.endswith(("token", "sessionid", "session_id", "sessid", "_session", "_secret",
                      "_password", "_passwd")):
        return True
    return name.endswith("_key") and any(hint in name for hint in _SECRET_KEY_HINTS)


def login_violation(url: str) -> str | None:
    """Return a reason if the URL carries credentials or an auth/session token."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.username or parts.password:
        return "URL містить логін/пароль"
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if value and is_auth_key(key):
            return f"URL містить токен доступу або сесії (параметр «{key}»)"
    return None


def redact_url(url: str) -> str:
    """Drop userinfo and mask credential query values, for logs and events."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[некоректний URL]"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    query = urlencode(
        [(key, "***" if value and is_auth_key(key) else value)
         for key, value in parse_qsl(parts.query, keep_blank_values=True)],
        safe="*",
    )
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def assert_logged_out(url: str) -> None:
    reason = login_violation(url)
    if reason:
        raise ComplianceError(
            "login_required",
            f"{reason}: {redact_url(url)}. Збираємо лише публічні сторінки без авторизації.",
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
    forbidden: set[str] = set()
    high: set[str] = set()
    for url in urls:
        assert_logged_out(url)
        policy = effective_policy(store, url)
        domain = normalize_domain(url)
        if policy is None or not domain:
            continue
        decision.policies[domain] = policy
        if policy.tos_risk == "forbidden":
            forbidden.add(domain)
        elif policy.tos_risk == "high":
            high.add(domain)

    if forbidden:
        raise ComplianceError(
            "forbidden",
            "Джерело заборонене в реєстрі джерел (tos_risk=forbidden): "
            + ", ".join(sorted(forbidden))
            + ". Змініть політику джерела або приберіть seed.",
            domains=sorted(forbidden),
        )
    if high:
        if not acknowledge_high_risk or len(reason.strip()) < 10:
            raise ComplianceError(
                "needs_ack",
                "Джерело з високим ризиком ToS: "
                + ", ".join(sorted(high))
                + ". Підтвердіть запуск і вкажіть причину (мінімум 10 символів) — "
                "це буде записано в журнал аудиту.",
                domains=sorted(high),
            )
        decision.acknowledged = sorted(high)
    return decision


def source_note(store: OperationalStore, urls: Iterable[str]) -> str:
    """Export note built from the *live* registry (not the snapshot stored in the task spec)."""
    parts: list[str] = []
    seen: set[str] = set()
    for url in urls:
        domain = normalize_domain(url)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        policy = effective_policy(store, domain) or SourcePolicy(domain=domain)
        reviewed = policy.reviewed_at.date().isoformat() if policy.reviewed_at else "not reviewed"
        parts.append(f"{domain} ({policy.tos_risk}/{policy.license}, {reviewed})")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# run scope: which run is fetching, and what it was allowed to touch
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunScope:
    task_id: str
    run_id: str | None = None
    acknowledged: frozenset[str] = frozenset()
    reported: set[str] = field(default_factory=set)

    def allows_high(self, url_domain: str, policy: SourcePolicy) -> bool:
        return url_domain in self.acknowledged or policy.domain in self.acknowledged


_SCOPE: ContextVar[RunScope | None] = ContextVar("daas_run_scope", default=None)


def current_scope() -> RunScope | None:
    return _SCOPE.get()


@contextlib.contextmanager
def run_scope(
    task_id: str, run_id: str | None = None, acknowledged: Iterable[str] = ()
) -> Iterator[RunScope]:
    scope = RunScope(task_id=task_id, run_id=run_id, acknowledged=frozenset(acknowledged))
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)


# ---------------------------------------------------------------------------
# fetch guard
# ---------------------------------------------------------------------------


class PolicyGuardFetcher:
    """Wraps any Fetcher; every URL (and redirect hop) is checked against the live registry."""

    def __init__(self, inner: Fetcher, store: OperationalStore) -> None:
        self.inner = inner
        self.store = store
        self.name = getattr(inner, "name", "http")
        if hasattr(inner, "redirect_guard"):
            inner.redirect_guard = self.violation  # type: ignore[attr-defined]

    def __getattr__(self, item: str):  # delegate extras (calls, cache, …) to the wrapped fetcher
        return getattr(self.inner, item)

    def violation(self, url: str) -> str | None:
        """Reason the URL may not be requested in the current run scope, or ``None``."""
        reason = login_violation(url)
        if reason:
            return f"logged-out policy: {reason}"
        policy = effective_policy(self.store, url)
        if policy is None:
            return None
        if policy.tos_risk == "forbidden":
            return f"source registry: {policy.domain} is forbidden"
        if policy.tos_risk == "high":
            scope = current_scope()
            if scope is None or not scope.allows_high(normalize_domain(url), policy):
                return f"source registry: {policy.domain} is high-risk and not acknowledged"
        return None

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        from .scraping.fetcher import FetchResult

        reason = self.violation(url)
        if reason:
            self._report(url, reason)
            return FetchResult(kind="policy", error=reason)
        result = await self.inner.fetch(url, force_refresh=force_refresh)
        # Engines that follow redirects internally (or a cached page) end up on final_url.
        final_url = result.page.final_url if result.page is not None else None
        if final_url and final_url != url:
            reason = self.violation(final_url)
            if reason:
                self._report(final_url, reason)
                return FetchResult(kind="policy", error=f"redirect: {reason}")
        if result.kind == "policy" and result.error:
            self._report(url, result.error)
        return result

    async def aclose(self) -> None:
        await self.inner.aclose()

    def _report(self, url: str, reason: str) -> None:
        scope = current_scope()
        if scope is None:
            return
        safe_url = redact_url(url)
        key = f"{normalize_domain(url)}|{reason.split(':', 1)[0]}"
        first = key not in scope.reported
        scope.reported.add(key)
        if not first:
            return  # one event per domain and rule per run, not one per link
        try:
            self.store.add_event(
                scope.task_id,
                "compliance_blocked",
                f"Заблоковано політикою джерел: {safe_url}",
                run_id=scope.run_id,
                level="warning",
                data={"url": safe_url, "reason": reason},
            )
        except Exception:  # noqa: BLE001 - auditing must not crash a fetch
            logger.warning("could not record compliance event for %s", safe_url, exc_info=True)
