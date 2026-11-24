"""Offline tests for P1-06: ProxyProvider (sticky, health, cost), ban detection, DomainCircuitBreaker, fallback, API/metrics/CLI."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.main import create_app
from app.models import FieldSpec, SourcePolicy, TaskSpec
from app.runtime import Studio
from app.scraping.fetcher import FetchResult, HttpFetcher
from app.scraping.resilience import (
    DomainCircuitBreaker,
    PoolProxyProvider,
    build_proxy_provider,
    detect_ban_marker,
    redact_proxy_url,
)


def test_proxy_provider_sticky_health_cost_and_redaction() -> None:
    """REQ-FETCH-020: PoolProxyProvider maintains sticky sessions, updates health/cooldown, counts bytes/cost, and redacts credentials."""
    now = [1000.0]

    provider = PoolProxyProvider(
        [
            "http://alice:supersecret@proxy1.local:8080",
            "http://bob:anothersecret@proxy2.local:8080",
        ],
        strategy="sticky_domain",
        sticky_ttl_s=300.0,
        cooldown_s=60.0,
        cost_per_gb_usd=10.0,
        ban_cooldown_after=2,
        clock=lambda: now[0],
    )

    # Secrets must never appear in redacted URL or snapshot
    redacted = redact_proxy_url("http://alice:supersecret@proxy1.local:8080")
    assert "supersecret" not in redacted
    assert "alice" not in redacted
    assert "proxy1.local:8080" in redacted

    snap0 = provider.snapshot()
    snap_json = json.dumps(snap0, ensure_ascii=False)
    assert "supersecret" not in snap_json
    assert "anothersecret" not in snap_json
    assert snap0["pool_size"] == 2
    assert snap0["healthy_count"] == 2

    # Sticky domain pins the same proxy across repeated requests within TTL
    p1 = provider.acquire("example.org")
    assert p1 is not None
    provider.report_outcome(p1.id, "example.org", "ok", bytes_transferred=512 * 1024)
    p2 = provider.acquire("example.org")
    assert p2 is not None and p2.id == p1.id

    # After TTL expiry, round-robin/health can rotate; more importantly, block clears sticky + lowers health + cools down after 2 blocks
    provider.report_outcome(p1.id, "example.org", "blocked", bytes_transferred=1024)
    p_rotated = provider.acquire("example.org")
    assert p_rotated is not None and p_rotated.id != p1.id

    # Second consecutive block puts p1 into cooldown
    provider.report_outcome(p1.id, "other.org", "blocked")
    snap_mid = provider.snapshot()
    p1_info = next(item for item in snap_mid["proxies"] if item["id"] == p1.id)
    assert p1_info["in_cooldown"] is True
    assert p1_info["health_score"] < 0.5
    assert snap_mid["healthy_count"] == 1

    # While p1 is cooling, only p_rotated is acquired
    assert provider.acquire("newdomain.org").id == p_rotated.id  # type: ignore[union-attr]

    # Put second proxy into cooldown -> acquire returns None cleanly without crashing
    provider.report_outcome(p_rotated.id, "newdomain.org", "blocked")
    provider.report_outcome(p_rotated.id, "newdomain.org", "blocked")
    assert provider.acquire("newdomain.org") is None

    # After cooldown expires, proxies become eligible again
    now[0] += 65.0
    recovered = provider.acquire("newdomain.org")
    assert recovered is not None

    # Accounting checks (bytes > 0 and cost_usd > 0)
    snap_end = provider.snapshot()
    assert snap_end["total_bytes"] >= 512 * 1024 + 1024
    assert snap_end["total_cost_usd"] > 0.0

    # Empty pool builds NoopProxyProvider cleanly
    empty_settings = build_proxy_provider(
        type("S", (), {"proxy_urls": "", "proxy_strategy": "sticky_domain", "proxy_sticky_ttl_s": 600.0, "proxy_cooldown_s": 60.0, "proxy_cost_per_gb_usd": 0.0})()  # type: ignore[arg-type]
    )
    assert empty_settings.pool_size == 0
    assert empty_settings.acquire("example.org") is None


@pytest.mark.asyncio
async def test_http_fetcher_ban_detection_and_circuit_breaker_lifecycle(tmp_path) -> None:
    """REQ-FETCH-021: HttpFetcher detects HTTP & HTML ban markers, rotates proxies, opens circuit after N blocks, pauses locally, and recovers via half_open."""
    now = [2000.0]
    calls_by_proxy: list[tuple[str | None, str]] = []
    mode = {"behavior": "proxy1_blocks_proxy2_ok"}

    def make_client(proxy_url: str | None) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            calls_by_proxy.append((proxy_url, str(request.url)))
            b = mode["behavior"]
            if b == "proxy1_blocks_proxy2_ok":
                if proxy_url and "p1" in proxy_url:
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/html"},
                        text="<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>",
                    )
                return httpx.Response(
                    200,
                    headers={"content-type": "text/html"},
                    text="<html><body><h1>Каталог клінік</h1></body></html>",
                )
            if b == "all_block":
                return httpx.Response(403, headers={"content-type": "text/html"}, text="Forbidden")
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body><h1>Відновлено</h1></body></html>",
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Deterministic ban detection helper check
    assert detect_ban_marker("<html><title>Just a moment...</title>cf_chl_opt</html>", 200) is not None
    assert detect_ban_marker("<html><body><h1>Офіційна сторінка клініки Альфа</h1></body></html>", 200) is None

    provider = PoolProxyProvider(
        ["http://user:pass1@p1.proxy.local:8080", "http://user:pass2@p2.proxy.local:8080"],
        strategy="sticky_domain",
        cooldown_s=30.0,
        cost_per_gb_usd=5.0,
        clock=lambda: now[0],
    )
    breaker = DomainCircuitBreaker(threshold=2, cooldown_s=45.0, clock=lambda: now[0])
    settings = make_settings(tmp_path, max_retries=1, respect_robots=False, per_domain_rps=100.0)
    fetcher = HttpFetcher(
        settings,
        proxy_provider=provider,
        circuit_breaker=breaker,
        client_factory=make_client,
    )
    try:
        # 1) Proxy 1 hits CAPTCHA challenge on HTTP 200 -> rotated to Proxy 2 -> succeeds! Circuit stays closed!
        res1 = await fetcher.fetch("https://target.example/page1")
        assert res1.ok and res1.page is not None
        assert "Каталог клінік" in res1.page.text
        assert breaker.status("target.example").state == "closed"

        # 2) Now all proxies return 403 -> 2 blocked fetch() calls trip circuit breaker to 'open'
        mode["behavior"] = "all_block"
        b1 = await fetcher.fetch("https://target.example/page2")
        assert b1.kind == "blocked"
        assert breaker.status("target.example").state == "closed"

        b2 = await fetcher.fetch("https://target.example/page3")
        assert b2.kind == "blocked"
        assert breaker.status("target.example").state == "open"

        # 3) Next request is rejected immediately as 'circuit_open' WITHOUT invoking HTTP transport
        before_count = len(calls_by_proxy)
        paused = await fetcher.fetch("https://target.example/page4")
        assert paused.kind == "circuit_open"
        assert len(calls_by_proxy) == before_count

        # 4) After cooldown, half_open probe executes: if blocked, re-opens; if ok after another cooldown, closes!
        now[0] += 50.0
        probe_fail = await fetcher.fetch("https://target.example/page5")
        assert probe_fail.kind == "blocked"
        assert breaker.status("target.example").state == "open"

        now[0] += 50.0
        mode["behavior"] = "recover"
        probe_ok = await fetcher.fetch("https://target.example/page6")
        assert probe_ok.ok
        assert breaker.status("target.example").state == "closed"
        assert breaker.status("target.example").consecutive_blocks == 0
    finally:
        await fetcher.aclose()


class _BlockingPrimaryMirrorFetcher:
    """Offline test fetcher simulating blocked primary domain and working/forbidden mirrors with breaker."""

    name = "mock-circuit"

    def __init__(self, breaker: DomainCircuitBreaker) -> None:
        self.breaker = breaker
        self.calls: list[str] = []

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        self.calls.append(url)
        host = (urlsplit(url).hostname or "").lower()
        if host == "blocked.example":
            allowed, _state, _wait = self.breaker.allow(host)
            if not allowed:
                return FetchResult(kind="circuit_open", error=f"Circuit breaker відкритий для {host}")
            self.breaker.record_block(host, "HTTP 403")
            return FetchResult(kind="blocked", status=403, error="HTTP 403 (anti-bot)")

        if host == "mirror.example":
            from app.models import PageContent
            from app.scraping.page import html_to_markdown, html_to_text
            from app.utils import hash_text

            html = """<!doctype html><html><body>
              <article class="clinic"><h2 class="nm">Дзеркальна Клініка</h2><span class="ct">Київ</span></article>
            </body></html>"""
            page = PageContent(
                url=url,
                final_url=url,
                status_code=200,
                html=html,
                text=html_to_text(html),
                markdown=html_to_markdown(html, url),
                content_hash=hash_text(html),
                engine=self.name,
            )
            return FetchResult(page=page, status=200)

        return FetchResult(kind="permanent", status=404, error="not found")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_pipeline_circuit_breaker_fallback_and_compliance_guard(tmp_path) -> None:
    """REQ-RUN-050: pipeline opens circuit after N blocks, switches to allowed mirror via PolicyGuardFetcher, emits events, and blocks forbidden mirrors."""
    settings = make_settings(tmp_path, circuit_breaker_threshold=2, circuit_breaker_cooldown_s=120.0)
    breaker = DomainCircuitBreaker(threshold=2, cooldown_s=120.0)
    fetcher = _BlockingPrimaryMirrorFetcher(breaker)
    studio = Studio(settings, fetcher=fetcher)
    # Inject our breaker so runner/resilience share the same instance as fetcher
    studio.circuit_breaker = breaker
    studio.runner.circuit_breaker = breaker

    try:
        from app.models import CssField, CssSchema

        # 1) Configure fallback mirror for blocked.example (mirror.example is allowed, evil-mirror.example is forbidden)
        studio.store.upsert_source_policy(
            SourcePolicy(
                domain="blocked.example",
                tos_risk="low",
                fallback_urls=["https://mirror.example/api/clinics"],
            )
        )
        studio.store.upsert_source_policy(
            SourcePolicy(domain="mirror.example", tos_risk="low")
        )
        studio.store.save_selector(
            "mirror.example",
            "auto:clinic",
            CssSchema(
                container="article.clinic",
                item_scope=True,
                fields=[
                    CssField(key="name", selector="h2.nm"),
                    CssField(key="city", selector="span.ct"),
                ],
            ),
        )
        studio.store.save_selector(
            "blocked.example",
            "auto:clinic",
            CssSchema(
                container="article.clinic",
                item_scope=True,
                fields=[
                    CssField(key="name", selector="h2.nm"),
                    CssField(key="city", selector="span.ct"),
                ],
            ),
        )

        spec = TaskSpec(
            title="Клініки з fallback",
            prompt="Збери клініки: назва, місто",
            entity="clinic",
            extraction="css",
            seeds=[
                "https://blocked.example/p1",
                "https://blocked.example/p2",
                "https://blocked.example/p3",
            ],
            columns=[
                FieldSpec(key="name", title="Назва"),
                FieldSpec(key="city", title="Місто"),
            ],
        )
        task = studio.store.create_task(spec)
        run = await studio.collect(task.id, skip_calibration=True)
        done = await studio.wait(run.id, timeout=20)
        assert done is not None

        # Records extracted from mirror.example via fallback!
        rows = studio.analytics.all_current(task.id)
        assert any(r["payload"]["name"] == "Дзеркальна Клініка" for r in rows)

        events = [e.type for e in studio.store.list_events(task.id, limit=100)]
        assert "circuit_opened" in events
        assert "circuit_fallback" in events
        assert breaker.status("blocked.example").state == "open"
        latest_manifest = studio.store.dataset_manifest(task.dataset_id or task.id)
        assert latest_manifest.get("full_crawl") is False

        # 2) Now mark mirror.example as forbidden and verify PolicyGuardFetcher blocks fallback
        studio.store.upsert_source_policy(
            SourcePolicy(
                domain="blocked.example",
                tos_risk="low",
                fallback_urls=["https://forbidden-mirror.example/data"],
            )
        )
        studio.store.upsert_source_policy(
            SourcePolicy(domain="forbidden-mirror.example", tos_risk="forbidden")
        )
        breaker.reset("blocked.example")

        spec2 = spec.model_copy(
            update={"title": "Заборонене дзеркало", "seeds": ["https://blocked.example/a", "https://blocked.example/b"]}
        )
        task2 = studio.store.create_task(spec2)
        run2 = await studio.collect(task2.id, skip_calibration=True)
        await studio.wait(run2.id, timeout=20)
        assert studio.analytics.all_current(task2.id) == []
        events2 = [e.type for e in studio.store.list_events(task2.id, limit=100)]
        assert "compliance_blocked" in events2
    finally:
        await studio.aclose()


def test_resilience_api_metrics_validation_and_cli(tmp_path, monkeypatch) -> None:
    """REQ-API-011: /api/resilience, /api/metrics, PUT /api/sources fallback_urls validation, circuit reset and CLI work offline without exposing secrets."""
    settings = make_settings(
        tmp_path,
        proxy_urls="http://alice:verysecretpass@proxy.example:8080",
        proxy_cost_per_gb_usd=4.0,
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_s=120.0,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        studio: Studio = client.app.state.studio  # type: ignore[attr-defined]
        proxy = studio.proxy_provider.acquire("alpha.example")
        assert proxy is not None
        studio.proxy_provider.report_outcome(
            proxy.id, "alpha.example", "ok", bytes_transferred=1024 * 1024
        )
        studio.circuit_breaker.record_block("alpha.example", "HTTP 403")
        studio.circuit_breaker.record_block("alpha.example", "HTTP 403")
        allowed, _, _ = studio.circuit_breaker.allow("alpha.example")
        assert allowed is False

        # 1) GET /api/resilience hides credentials and exposes metrics/states
        res = client.get("/api/resilience")
        assert res.status_code == 200, res.text
        body = res.json()
        assert "verysecretpass" not in res.text
        assert "alice" not in res.text
        assert body["pool_size"] == 1
        assert body["healthy_count"] == 1
        assert body["total_bytes"] == 1024 * 1024
        assert body["total_cost_usd"] > 0.0
        assert any(c["domain"] == "alpha.example" and c["state"] == "open" for c in body["circuits"])

        # 2) Prometheus /api/metrics includes proxy and circuit metrics without secrets
        metrics_resp = client.get("/api/metrics")
        assert metrics_resp.status_code == 200
        metrics_text = metrics_resp.text
        assert "verysecretpass" not in metrics_text
        assert "daas_proxy_pool_size 1" in metrics_text
        assert "daas_proxy_healthy 1" in metrics_text
        assert f"daas_proxy_bytes_total {1024 * 1024}" in metrics_text
        assert "daas_proxy_cost_usd_total " in metrics_text
        assert "daas_circuit_open_domains 1" in metrics_text
        assert 'daas_blocked_total{reason="circuit_open"} 1' in metrics_text

        # 3) PUT /api/sources/{domain} validates fallback_urls (rejects credentials/tokens, accepts valid public URLs)
        bad_src = client.put(
            "/api/sources/alpha.example",
            json={"tos_risk": "low", "fallback_urls": ["https://mirror.example/api?token=secret123"]},
        )
        assert bad_src.status_code in (400, 422)

        ok_src = client.put(
            "/api/sources/alpha.example",
            json={
                "tos_risk": "low",
                "license": "open",
                "fallback_urls": ["https://mirror.example/clinics"],
                "reviewed": True,
            },
        )
        assert ok_src.status_code == 200, ok_src.text
        assert ok_src.json()["item"]["fallback_urls"] == ["https://mirror.example/clinics"]

        # 4) POST /api/resilience/circuits/{domain}/reset closes open breaker and logs event
        reset_resp = client.post("/api/resilience/circuits/alpha.example/reset")
        assert reset_resp.status_code == 200, reset_resp.text
        assert reset_resp.json()["circuit"]["state"] == "closed"
        assert "daas_circuit_open_domains 0" in client.get("/api/metrics").text

    # 5) CLI proxy-status and circuit-reset
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    runner = CliRunner()
    status_res = runner.invoke(cli_app, ["proxy-status"])
    assert status_res.exit_code == 0, status_res.output
    assert "verysecretpass" not in status_res.output
    assert "pool_size" in status_res.output

    reset_cli = runner.invoke(cli_app, ["circuit-reset", "alpha.example"])
    assert reset_cli.exit_code == 0, reset_cli.output
