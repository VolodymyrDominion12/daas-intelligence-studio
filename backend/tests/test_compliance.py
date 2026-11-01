"""Compliance enforcement (P1-04): forbidden/high sources, logged-out rule, fetch guard."""

from __future__ import annotations

from pathlib import Path

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
)
from app.db.sqlite_store import OperationalStore
from app.models import SourcePolicy

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
    ],
)
def test_login_violation_allows_public_urls(url: str) -> None:
    assert login_violation(url) is None


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
