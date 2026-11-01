"""Compliance enforcement (P1-04): forbidden/high sources, logged-out rule, fetch guard."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import DEMO_BASE, FixtureFetcher, make_settings
from fastapi.testclient import TestClient
from test_api_flow import PROMPT, _client, _wait

from app.compliance import (
    ComplianceError,
    PolicyGuardFetcher,
    check_run,
    domain_chain,
    effective_policy,
    login_violation,
    redact_url,
    run_scope,
    source_note,
)
from app.db.sqlite_store import ActiveRunError, OperationalStore
from app.models import PageContent, SourcePolicy
from app.scraping.fetcher import FetchResult, HttpFetcher

SEED = f"{DEMO_BASE}/catalog.html"


# ---------------------------------------------------------------------------
# unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@shop.example/catalog",
        "https://shop.example/catalog?session=abc123",
        "https://shop.example/list?page=2&access_token=xyz",
        "https://shop.example/list?PHPSESSID=deadbeef",
        "https://shop.example/cb?oauth_token=abc",
        "https://shop.example/cb?refresh_token=abc",
        "https://shop.example/cb?session_token=abc",
        "https://shop.example/cb?x-api-key=abc",
        "https://shop.example/cb?client_secret=abc",
    ],
)
def test_login_violation_detects_credentials(url: str) -> None:
    assert login_violation(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example/catalog?page=2&sort=price",
        "https://shop.example/catalog?token=",  # empty value is not a credential
        "https://shop.example/",
        "https://shop.example/catalog?key=price",  # generic filter, not a credential
        "https://shop.example/catalog?sig=1&sort_key=rating",
        "https://shop.example/catalog?pageToken=CAoQAA",  # pagination cursor
        "https://shop.example/catalog?nextToken=abc",
    ],
)
def test_login_violation_allows_public_urls(url: str) -> None:
    assert login_violation(url) is None


def test_redact_url_hides_credentials() -> None:
    redacted = redact_url("https://bob:pw@shop.example/a?sid=SECRET&page=2")
    assert "SECRET" not in redacted and "bob" not in redacted and "pw@" not in redacted
    assert "page=2" in redacted and "sid=***" in redacted


def test_domain_chain() -> None:
    assert domain_chain("a.b.example.com") == ["a.b.example.com", "b.example.com", "example.com"]
    assert domain_chain("demo.local") == ["demo.local"]


def test_parent_policy_covers_subdomains(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        store.upsert_source_policy(SourcePolicy(domain="example.com", tos_risk="forbidden"))
        policy = effective_policy(store, "https://shop.example.com/x")
        assert policy is not None and policy.tos_risk == "forbidden"
        # a more specific reviewed entry wins over the parent
        store.upsert_source_policy(SourcePolicy(domain="open.example.com", tos_risk="low"))
        assert effective_policy(store, "https://open.example.com/").tos_risk == "low"
    finally:
        store.close()


def test_check_run_rules(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        store.upsert_source_policy(SourcePolicy(domain="bad.example", tos_risk="forbidden"))
        store.upsert_source_policy(SourcePolicy(domain="risky.example", tos_risk="high"))

        with pytest.raises(ComplianceError) as forbidden:
            check_run(store, ["https://bad.example/a", "https://risky.example/b"])
        assert forbidden.value.code == "forbidden"
        assert forbidden.value.domains == ["bad.example"]

        with pytest.raises(ComplianceError) as needs_ack:
            check_run(store, ["https://risky.example/b"])
        assert needs_ack.value.code == "needs_ack"

        with pytest.raises(ComplianceError):  # a reason is mandatory
            check_run(store, ["https://risky.example/b"], acknowledge_high_risk=True, reason="ok")

        decision = check_run(
            store, ["https://risky.example/b"],
            acknowledge_high_risk=True, reason="Публічний прайс, погоджено з юристом",
        )
        assert decision.acknowledged == ["risky.example"]

        assert check_run(store, ["https://unknown.example/"]).acknowledged == []
    finally:
        store.close()


async def test_guard_fetcher_blocks_forbidden_and_logged_in(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    inner = FixtureFetcher()
    guard = PolicyGuardFetcher(inner, store)
    try:
        store.upsert_source_policy(SourcePolicy(domain="alpha.example", tos_risk="forbidden"))
        blocked = await guard.fetch("https://alpha.example/")
        assert blocked.kind == "policy" and blocked.page is None

        login = await guard.fetch(f"{DEMO_BASE}/catalog.html?sid=42")
        assert login.kind == "policy"
        assert inner.calls == []  # the wrapped fetcher was never reached

        ok = await guard.fetch(SEED)
        assert ok.ok and inner.calls == [SEED]
        assert guard.name == inner.name
    finally:
        store.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path):
    test_client, fetcher = _client(tmp_path)
    with test_client as active:
        active.fetcher = fetcher  # type: ignore[attr-defined]
        yield active


def _create(client: TestClient) -> str:
    created = client.post("/api/tasks", json={"prompt": PROMPT, "seeds": [SEED]})
    assert created.status_code == 201, created.text
    return created.json()["task"]["id"]


def _set_risk(client: TestClient, risk: str) -> None:
    response = client.put("/api/sources/demo.local", json={"tos_risk": risk, "reviewed": True})
    assert response.status_code == 200, response.text


def _events(client: TestClient, task_id: str) -> list[dict]:
    return client.get(f"/api/tasks/{task_id}/events").json()["items"]


def test_forbidden_source_is_a_hard_stop(client: TestClient) -> None:
    task_id = _create(client)
    _set_risk(client, "forbidden")

    calibrate = client.post(f"/api/tasks/{task_id}/calibrate")
    assert calibrate.status_code == 403, calibrate.text
    assert calibrate.json()["code"] == "forbidden"
    assert calibrate.json()["domains"] == ["demo.local"]

    collect = client.post(
        f"/api/tasks/{task_id}/run",
        json={"skip_calibration": True, "acknowledge_high_risk": True, "reason": "x" * 20},
    )
    assert collect.status_code == 403  # an acknowledgement cannot unlock "forbidden"

    assert client.get(f"/api/tasks/{task_id}/runs").json()["items"] == []
    assert any(event["type"] == "compliance_blocked" for event in _events(client, task_id))


def test_high_risk_requires_acknowledgement_and_is_audited(client: TestClient) -> None:
    task_id = _create(client)
    _set_risk(client, "high")

    first = client.post(f"/api/tasks/{task_id}/calibrate")
    assert first.status_code == 428, first.text
    assert first.json()["code"] == "needs_ack"

    reason = "Публічний каталог, ToS дозволяє некомерційний збір"
    second = client.post(
        f"/api/tasks/{task_id}/calibrate",
        params={"acknowledge_high_risk": "true", "reason": reason},
    )
    assert second.status_code == 200, second.text
    _wait(client, second.json()["run"]["id"])

    overrides = [e for e in _events(client, task_id) if e["type"] == "compliance_override"]
    assert overrides and overrides[0]["data"]["reason"] == reason
    assert overrides[0]["data"]["domains"] == ["demo.local"]


def test_seed_with_credentials_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        json={"prompt": PROMPT, "seeds": ["https://user:pass@demo.local/catalog.html"]},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "login_required"


def test_forbidden_website_is_skipped_during_collection(client: TestClient) -> None:
    """The fetch guard also covers pages reached by crawling, not only seeds."""
    task_id = _create(client)
    client.put("/api/sources/demo.local", json={"tos_risk": "low", "reviewed": True})
    calibrate = client.post(f"/api/tasks/{task_id}/calibrate")
    assert calibrate.status_code == 200, calibrate.text
    _wait(client, calibrate.json()["run"]["id"])

    # The catalog stays allowed; the clinic's own website (visited by enrichment) is forbidden.
    client.put("/api/sources/alpha.example", json={"tos_risk": "forbidden", "reviewed": True})
    collect = client.post(f"/api/tasks/{task_id}/run", json={})
    assert collect.status_code == 200, collect.text
    _wait(client, collect.json()["run"]["id"])
    enrich = client.post(f"/api/tasks/{task_id}/enrich", json={})
    assert enrich.status_code == 200, enrich.text
    _wait(client, enrich.json()["run"]["id"])

    fetched = client.fetcher.calls  # type: ignore[attr-defined]
    assert not any(url.startswith("https://alpha.example") for url in fetched)


# ---------------------------------------------------------------------------
# review follow-ups
# ---------------------------------------------------------------------------


def test_policy_domains_are_normalized(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        store.upsert_source_policy(SourcePolicy(domain="WWW.Shop.Example", tos_risk="forbidden"))
        policy = effective_policy(store, "https://shop.example/catalog")
        assert policy is not None and policy.tos_risk == "forbidden"
        assert policy.domain == "shop.example"
        assert [item.domain for item in store.list_source_policies()] == ["shop.example"]
    finally:
        store.close()


async def test_high_risk_needs_acknowledgement_inside_the_run(tmp_path: Path) -> None:
    """Discovered or newly high-risk domains are not fetched unless acknowledged for the run."""
    store = OperationalStore(make_settings(tmp_path).paths)
    inner = FixtureFetcher()
    guard = PolicyGuardFetcher(inner, store)
    task = store.create_task(make_spec_for_guard())
    try:
        store.upsert_source_policy(SourcePolicy(domain="alpha.example", tos_risk="high"))
        assert (await guard.fetch("https://alpha.example/")).kind == "policy"  # no run scope

        with run_scope(task.id, "run-1", acknowledged=["demo.local"]):
            blocked = await guard.fetch("https://alpha.example/")
        assert blocked.kind == "policy" and "not acknowledged" in (blocked.error or "")

        with run_scope(task.id, "run-2", acknowledged=["alpha.example"]):
            allowed = await guard.fetch("https://alpha.example/")
        assert allowed.ok

        events = [e for e in store.list_events(task.id) if e.type == "compliance_blocked"]
        assert len(events) == 1 and events[0].run_id == "run-1"
    finally:
        store.close()


async def test_guard_reports_once_and_redacts(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    guard = PolicyGuardFetcher(FixtureFetcher(), store)
    task = store.create_task(make_spec_for_guard())
    try:
        with run_scope(task.id, "run-1"):
            for page in range(3):
                result = await guard.fetch(f"{DEMO_BASE}/list?page={page}&sid=TOPSECRET")
                assert result.kind == "policy"
        events = [e for e in store.list_events(task.id) if e.type == "compliance_blocked"]
        assert len(events) == 1
        assert "TOPSECRET" not in events[0].message
        assert "TOPSECRET" not in str(events[0].data)
    finally:
        store.close()


class _RedirectingFetcher:
    """Pretends the engine followed a redirect internally (like Playwright)."""

    name = "fake"

    def __init__(self, final_url: str) -> None:
        self.final_url = final_url

    async def fetch(self, url: str, *, force_refresh: bool = False) -> FetchResult:
        page = PageContent(url=url, final_url=self.final_url, html="<p>x</p>")
        return FetchResult(page=page, status=200)

    async def aclose(self) -> None:
        return None


async def test_guard_rejects_forbidden_final_url(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        store.upsert_source_policy(SourcePolicy(domain="bad.example", tos_risk="forbidden"))
        guard = PolicyGuardFetcher(_RedirectingFetcher("https://bad.example/landing"), store)
        result = await guard.fetch("https://good.example/")
        assert result.kind == "policy" and result.page is None
    finally:
        store.close()


async def test_http_fetcher_checks_every_redirect_hop(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow_private_network=True, max_retries=0)
    store = OperationalStore(settings.paths)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "good.example":
            return httpx.Response(302, headers={"location": "https://bad.example/x"})
        return httpx.Response(200, text="<html>secret data</html>",
                              headers={"content-type": "text/html"})

    inner = HttpFetcher(settings)
    await inner._client.aclose()
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    guard = PolicyGuardFetcher(inner, store)
    try:
        store.upsert_source_policy(SourcePolicy(domain="bad.example", tos_risk="forbidden"))
        result = await guard.fetch("https://good.example/start")
        assert result.kind == "policy" and result.page is None
        assert requested == ["https://good.example/start"]  # the forbidden hop was never requested
    finally:
        await guard.aclose()
        store.close()


def test_exclusive_run_creation_is_atomic_with_audit(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        task = store.create_task(make_spec_for_guard())
        audit = ("compliance_override", "warning", "ok", {"reason": "перевірено юристом"})
        run = store.create_run(task.id, "collect", exclusive=True, audit=audit)
        events = store.list_events(task.id)
        assert [(e.type, e.run_id) for e in events] == [("compliance_override", run.id)]

        with pytest.raises(ActiveRunError):
            store.create_run(task.id, "collect", exclusive=True, audit=audit)
        assert len(store.list_runs(task.id)) == 1
        assert len(store.list_events(task.id)) == 1  # the refused run left no audit record
    finally:
        store.close()


def test_source_note_uses_live_registry(tmp_path: Path) -> None:
    store = OperationalStore(make_settings(tmp_path).paths)
    try:
        store.upsert_source_policy(SourcePolicy(domain="demo.local", tos_risk="low", license="cc-by"))
        assert "demo.local (low/cc-by" in source_note(store, [SEED, SEED])
        store.upsert_source_policy(SourcePolicy(domain="demo.local", tos_risk="high", license="tos"))
        assert "demo.local (high/tos" in source_note(store, [SEED])
    finally:
        store.close()


async def test_discovered_seeds_with_tokens_are_dropped(tmp_path: Path) -> None:
    from app.runtime import Studio

    studio = Studio(make_settings(tmp_path), fetcher=FixtureFetcher())
    original = studio.planner.plan

    async def plan_with_discovery(prompt, seeds, *, title=None):
        spec = await original(prompt, [SEED], title=title)
        spec.seeds = [SEED, f"{DEMO_BASE}/admin?session_token=abc"]
        return spec

    studio.planner.plan = plan_with_discovery  # type: ignore[method-assign]
    try:
        task = await studio.create_task(PROMPT, [])
        assert task.spec.seeds == [SEED]
        assert any("токенами доступу" in note for note in task.spec.assumptions)
    finally:
        await studio.aclose()


def test_forbidden_enrichment_target_is_audited(client: TestClient) -> None:
    task_id = _create(client)
    # Saved with a case/www variant: must still apply to the canonical host.
    client.put("/api/sources/WWW.Alpha.Example", json={"tos_risk": "forbidden"})
    calibrate = client.post(f"/api/tasks/{task_id}/calibrate")
    _wait(client, calibrate.json()["run"]["id"])
    collect = client.post(f"/api/tasks/{task_id}/run", json={})
    _wait(client, collect.json()["run"]["id"])
    enrich = client.post(f"/api/tasks/{task_id}/enrich", json={})
    _wait(client, enrich.json()["run"]["id"])

    blocked = [e for e in _events(client, task_id) if e["type"] == "compliance_blocked"]
    assert any("alpha.example" in e["message"] for e in blocked)


def make_spec_for_guard():
    from conftest import make_spec

    return make_spec()
