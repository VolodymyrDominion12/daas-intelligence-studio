"""Offline tests for P1-07: deterministic JS-shell detection, Playwright sidecar, auto escalation, SSRF/policy/circuit safety, events, API/metrics, doctor, and deploy templates."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest
from conftest import make_settings
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.compliance import PolicyGuardFetcher, run_scope
from app.db.sqlite_store import OperationalStore
from app.main import create_app
from app.models import FieldSpec, SourcePolicy, TaskSpec
from app.runtime import Studio
from app.scraping.fetcher import HttpFetcher, build_fetcher
from app.scraping.js_render import (
    JsRenderStats,
    PlaywrightSidecarClient,
    detect_js_shell,
)
from app.scraping.resilience import DomainCircuitBreaker, PoolProxyProvider
from app.storage.raw_cache import RawCache

ROOT = Path(__file__).resolve().parents[2]


def test_detect_js_shell_heuristics() -> None:
    """REQ-FETCH-030: detect_js_shell identifies empty SPA shells deterministically without false positives."""
    react_shell = """<!doctype html><html><head><title>App</title>
      <script type="module" src="/assets/index-123.js"></script></head>
      <body><div id="root"></div></body></html>"""
    assert detect_js_shell(react_shell) is not None

    next_shell = """<!doctype html><html><head>
      <script src="/_next/static/chunks/main.js"></script></head>
      <body><div id="__next"></div><noscript>Please enable JavaScript to continue.</noscript></body></html>"""
    assert detect_js_shell(next_shell) is not None

    bundle_only = """<!doctype html><html><head>
      <script src="/bundle.js"></script><script src="/vendor.js"></script></head>
      <body><div id="app">Loading...</div></body></html>"""
    assert detect_js_shell(bundle_only) is not None

    ssr_catalog = (
        "<!doctype html><html><body><main><h1>Каталог стоматологічних клінік України</h1>"
        "<p>Клініка Альфа у Києві надає повний спектр стоматологічних послуг для дорослих і дітей. "
        "Адреса: вул. Хрещатик 10, телефон +380441112233, рейтинг 4.9 зірок.</p>"
        "</main></body></html>"
    )
    assert detect_js_shell(ssr_catalog) is None

    jsonld_page = """<!doctype html><html><head>
      <script type="application/ld+json">{"@type": "Organization", "name": "Clinic Alpha"}</script>
      <script type="module" src="/assets/app.js"></script></head>
      <body><div id="root"></div></body></html>"""
    assert detect_js_shell(jsonld_page) is None

    captcha_page = """<!doctype html><html><head><title>Just a moment...</title></head>
      <body>cf-browser-verification cf_chl_opt</body></html>"""
    assert detect_js_shell(captcha_page) is None

    assert detect_js_shell("") is None
    assert detect_js_shell("   \n  ") is None


@pytest.mark.asyncio
async def test_auto_fetch_escalates_js_shell_caches_and_falls_back(tmp_path: Path) -> None:
    """REQ-FETCH-030: fetch_engine='auto' keeps SSR on HTTP, escalates SPA shells to sidecar, caches rendered DOM, ignores JSON, and falls back safely on 503."""
    target_calls: list[str] = []
    sidecar_calls: list[dict] = []

    def site_handler(request: httpx.Request) -> httpx.Response:
        target_calls.append(str(request.url))
        path = request.url.path
        if path == "/ssr":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<html><body><h1>SSR Каталог</h1><p>Повноцінна серверна сторінка з реальним "
                    "контентом про компанії Києва, Львова, Одеси та Харкова з контактами і рейтингом.</p>"
                    "</body></html>"
                ),
            )
        if path in {"/spa", "/spa-broken"}:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html><head><script type="module" src="/app.js"></script></head><body><div id="root"></div></body></html>',
            )
        if path == "/api":
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"items": []})
        return httpx.Response(404, text="not found")

    def sidecar_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "engine": "playwright", "browser": "chromium"})
        payload = json.loads(request.content.decode("utf-8"))
        sidecar_calls.append(payload)
        if payload["url"].endswith("/spa-broken"):
            return httpx.Response(503, json={"detail": "sidecar overloaded"})
        return httpx.Response(
            200,
            json={
                "url": payload["url"],
                "final_url": payload["url"],
                "status_code": 200,
                "html": (
                    "<html><body><article class='clinic'><h2 class='nm'>SPA Клініка Бета</h2>"
                    "<span class='ct'>Львів</span><p>Стоматологія, ортодонтія, імплантація, "
                    "телефон +380322001122, графік роботи щодня з 9 до 20.</p></article></body></html>"
                ),
                "headers": {"content-type": "text/html"},
                "elapsed_ms": 42,
            },
        )

    settings = make_settings(
        tmp_path,
        fetch_engine="auto",
        playwright_sidecar_url="http://sidecar.local:8765",
        proxy_urls="http://alice:pw@proxy1.local:8080",
        proxy_cost_per_gb_usd=4.0,
        respect_robots=False,
        per_domain_rps=100.0,
    )
    store = OperationalStore(settings.paths)
    cache = RawCache(settings.paths.raw_cache, store)
    js_stats = JsRenderStats()
    sidecar = PlaywrightSidecarClient(
        "http://sidecar.local:8765",
        wait_until="domcontentloaded",
        wait_ms=100,
        transport=httpx.MockTransport(sidecar_handler),
    )
    provider = PoolProxyProvider(["http://alice:pw@proxy1.local:8080"], cost_per_gb_usd=4.0)
    fetcher = HttpFetcher(
        settings,
        cache=cache,
        proxy_provider=provider,
        client_factory=lambda _proxy: httpx.AsyncClient(transport=httpx.MockTransport(site_handler)),
        sidecar_client=sidecar,
        js_stats=js_stats,
    )
    try:
        # 1) SSR page does NOT trigger sidecar
        ssr = await fetcher.fetch("https://site.example/ssr")
        assert ssr.ok and ssr.page is not None
        assert ssr.page.engine == "http"
        assert len(sidecar_calls) == 0

        # 2) SPA shell triggers sidecar escalation and caches rendered HTML
        spa = await fetcher.fetch("https://site.example/spa")
        assert spa.ok and spa.page is not None
        assert "SPA Клініка Бета" in spa.page.text
        assert spa.page.engine == "playwright-sidecar"
        assert spa.page.headers.get("x-daas-js-rendered") == "1"
        assert len(sidecar_calls) == 1
        assert sidecar_calls[0]["proxy_url"] == "http://alice:pw@proxy1.local:8080"
        assert js_stats.rendered_ok == 1

        # 3) Repeated SPA fetch comes from RawCache without calling sidecar again
        spa_cached = await fetcher.fetch("https://site.example/spa")
        assert spa_cached.ok and spa_cached.page is not None
        assert spa_cached.page.from_cache is True
        assert "SPA Клініка Бета" in spa_cached.page.text
        assert len(sidecar_calls) == 1

        # 4) JSON API response never triggers JS escalation
        api_res = await fetcher.fetch("https://site.example/api", allow_json=True)
        assert api_res.ok and api_res.page is not None
        assert len(sidecar_calls) == 1

        # 5) Sidecar failure (503) in 'auto' mode safely falls back to original HTTP response
        broken = await fetcher.fetch("https://site.example/spa-broken")
        assert broken.ok and broken.page is not None
        assert broken.page.engine == "http"
        assert broken.page.headers.get("x-daas-js-fallback")
        assert js_stats.fallbacks == 1
    finally:
        await fetcher.aclose()
        store.close()


@pytest.mark.asyncio
async def test_playwright_sidecar_enforces_ssrf_policy_and_circuit_breaker(tmp_path: Path) -> None:
    """REQ-FETCH-031, REQ-FETCH-009: SidecarPlaywrightFetcher enforces SSRF on final_url, PolicyGuardFetcher, ban detection, CircuitBreaker, and falls back to HTTP when missing."""
    # Without sidecar URL and without local playwright module -> warns and falls back to HttpFetcher
    no_pw_settings = make_settings(tmp_path, fetch_engine="playwright", playwright_sidecar_url=None)
    fallback_fetcher = build_fetcher(no_pw_settings)
    try:
        assert fallback_fetcher.name == "http"
    finally:
        await fallback_fetcher.aclose()

    def sidecar_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        payload = json.loads(request.content.decode("utf-8"))
        url = payload["url"]
        if url.endswith("/ssrf-redirect"):
            return httpx.Response(
                200,
                json={
                    "url": url,
                    "final_url": "http://127.0.0.1/latest/meta-data",
                    "status_code": 200,
                    "html": "<html><body>secret metadata</body></html>",
                    "headers": {"content-type": "text/html"},
                },
            )
        if url.endswith("/policy-redirect"):
            return httpx.Response(
                200,
                json={
                    "url": url,
                    "final_url": "https://forbidden.example/private-area",
                    "status_code": 200,
                    "html": "<html><body><h1>Forbidden target</h1></body></html>",
                    "headers": {"content-type": "text/html"},
                },
            )
        if url.endswith("/captcha"):
            return httpx.Response(
                200,
                json={
                    "url": url,
                    "final_url": url,
                    "status_code": 200,
                    "html": "<html><head><title>Just a moment...</title></head><body>cf-browser-verification</body></html>",
                    "headers": {"content-type": "text/html"},
                },
            )
        return httpx.Response(
            200,
            json={
                "url": url,
                "final_url": url,
                "status_code": 200,
                "html": "<html><body><h1>Публічний SPA сайт</h1></body></html>",
                "headers": {"content-type": "text/html"},
            },
        )

    settings = make_settings(
        tmp_path,
        fetch_engine="playwright",
        playwright_sidecar_url="http://sidecar.local:8765",
        allow_private_network=False,
        respect_robots=False,
        per_domain_rps=100.0,
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_s=60.0,
    )
    store = OperationalStore(settings.paths)
    store.upsert_source_policy(SourcePolicy(domain="forbidden.example", tos_risk="forbidden"))
    breaker = DomainCircuitBreaker(threshold=2, cooldown_s=60.0)
    js_stats = JsRenderStats()
    sidecar = PlaywrightSidecarClient(
        "http://sidecar.local:8765",
        transport=httpx.MockTransport(sidecar_handler),
    )
    inner = build_fetcher(
        settings,
        circuit_breaker=breaker,
        sidecar_client=sidecar,
        js_stats=js_stats,
    )
    guarded = PolicyGuardFetcher(inner, store)
    try:
        assert inner.name == "playwright-sidecar"

        with run_scope("t-ssrf", "r-1"):
            ok_res = await guarded.fetch("https://8.8.8.8/ok")
            assert ok_res.ok and ok_res.page is not None
            assert "Публічний SPA сайт" in ok_res.page.text

            ssrf_res = await guarded.fetch("https://8.8.8.8/ssrf-redirect")
            assert ssrf_res.kind == "unsafe"

            policy_res = await guarded.fetch("https://8.8.8.8/policy-redirect")
            assert policy_res.kind == "policy"

            b1 = await guarded.fetch("https://9.9.9.9/captcha")
            b2 = await guarded.fetch("https://9.9.9.9/captcha")
            assert b1.kind == "blocked"
            assert b2.kind == "blocked"
            assert breaker.status("9.9.9.9").state == "open"
            paused = await guarded.fetch("https://9.9.9.9/ok")
            assert paused.kind == "circuit_open"
    finally:
        await guarded.aclose()
        store.close()


@pytest.mark.asyncio
async def test_pipeline_events_metrics_api_doctor_and_sidecar_deploy(tmp_path: Path, monkeypatch) -> None:
    """REQ-RUN-060, REQ-API-012: pipeline emits js_rendered/fallback events; /api/config, /api/resilience, /api/metrics & daas doctor expose redacted JS stats; deploy sidecar files are valid."""
    def site_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<html><head><script type="module" src="/spa.js"></script></head><body><div id="root"></div></body></html>',
        )

    def sidecar_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "engine": "playwright", "browser": "chromium"})
        payload = json.loads(request.content.decode("utf-8"))
        if "fail" in payload["url"]:
            return httpx.Response(503, json={"detail": "render timeout"})
        return httpx.Response(
            200,
            json={
                "url": payload["url"],
                "final_url": payload["url"],
                "status_code": 200,
                "html": (
                    "<html><body><main><section class='results'>"
                    "<article class='clinic-card'><h3 class='clinic-card__name'>Клініка Віта</h3><div class='clinic-card__city'>Одеса</div></article>"
                    "<article class='clinic-card'><h3 class='clinic-card__name'>Клініка Дента</h3><div class='clinic-card__city'>Київ</div></article>"
                    "<article class='clinic-card'><h3 class='clinic-card__name'>Клініка Норма</h3><div class='clinic-card__city'>Львів</div></article>"
                    "</section></main></body></html>"
                ),
                "headers": {"content-type": "text/html"},
                "elapsed_ms": 55,
            },
        )

    settings = make_settings(
        tmp_path,
        fetch_engine="auto",
        playwright_sidecar_url="http://sidecaruser:sidecarsecret@sidecar.example:8765",
        respect_robots=False,
        per_domain_rps=100.0,
    )
    js_stats = JsRenderStats()
    sidecar = PlaywrightSidecarClient(
        settings.playwright_sidecar_url or "",
        transport=httpx.MockTransport(sidecar_handler),
    )
    custom_fetcher = HttpFetcher(
        settings,
        client_factory=lambda _proxy: httpx.AsyncClient(transport=httpx.MockTransport(site_handler)),
        sidecar_client=sidecar,
        js_stats=js_stats,
    )
    studio = Studio(settings, fetcher=custom_fetcher)
    studio.js_stats = js_stats
    studio.sidecar_client = sidecar
    # Inject same cache/store wiring into custom_fetcher
    custom_fetcher.cache = studio.cache
    try:
        spec = TaskSpec(
            title="SPA клініки",
            prompt="Збери клініки: назва, місто",
            entity="clinic",
            extraction="css",
            seeds=["https://spa-clinics.example/list"],
            columns=[
                FieldSpec(key="name", title="Назва"),
                FieldSpec(key="city", title="Місто"),
            ],
        )
        task = studio.store.create_task(spec)
        cal_run = await studio.calibrate(task.id)
        cal_done = await studio.wait(cal_run.id, timeout=20)
        assert cal_done is not None and cal_done.status == "succeeded"

        events_cal = [e.type for e in studio.store.list_events(task.id, limit=100)]
        assert "js_rendered" in events_cal

        # Second task targeting fallback URL where sidecar fails 503
        fail_spec = TaskSpec(
            title="SPA fallback",
            prompt="Збери клініки: назва",
            entity="clinic",
            extraction="css",
            seeds=["https://spa-clinics.example/fail"],
            columns=[FieldSpec(key="name", title="Назва")],
        )
        fail_task = studio.store.create_task(fail_spec)
        fail_run = await studio.collect(fail_task.id, skip_calibration=True)
        await studio.wait(fail_run.id, timeout=20)
        events_fail = [e.type for e in studio.store.list_events(fail_task.id, limit=100)]
        assert "js_render_fallback" in events_fail
        # Third check: fetch_engine='http' warns in CalibrationReport when encountering a JS shell
        http_settings = make_settings(
            tmp_path / "http_hint",
            fetch_engine="http",
            respect_robots=False,
            per_domain_rps=100.0,
        )
        http_fetcher = HttpFetcher(
            http_settings,
            client_factory=lambda _proxy: httpx.AsyncClient(transport=httpx.MockTransport(site_handler)),
        )
        http_studio = Studio(http_settings, fetcher=http_fetcher)
        http_fetcher.cache = http_studio.cache
        try:
            http_task = http_studio.store.create_task(
                TaskSpec(
                    title="SPA with HTTP mode",
                    prompt="Збери клініки: назва",
                    entity="clinic",
                    extraction="css",
                    seeds=["https://spa-clinics.example/list"],
                    columns=[FieldSpec(key="name", title="Назва")],
                )
            )
            http_run = await http_studio.calibrate(http_task.id)
            await http_studio.wait(http_run.id, timeout=20)
            cal_json = http_studio.store.get_calibration(http_task.id)
            assert cal_json is not None and "SPA-оболонку" in cal_json
        finally:
            await http_studio.aclose()
    finally:
        await studio.aclose()


def test_js_api_metrics_doctor_and_sidecar_deploy(tmp_path: Path, monkeypatch) -> None:
    """REQ-API-012, REQ-FETCH-031: /api/config, /api/resilience, /api/metrics & daas doctor expose redacted JS stats; deploy sidecar files are valid."""
    settings = make_settings(
        tmp_path,
        fetch_engine="auto",
        playwright_sidecar_url="http://sidecaruser:sidecarsecret@sidecar.example:8765",
        respect_robots=False,
        per_domain_rps=100.0,
    )

    # API & Metrics checks with secret redaction
    app = create_app(settings)
    with TestClient(app) as client:
        api_studio: Studio = client.app.state.studio  # type: ignore[attr-defined]
        api_studio.js_stats.escalations = 2
        api_studio.js_stats.rendered_ok = 1
        api_studio.js_stats.fallbacks = 1
        api_studio.js_stats.errors = 1

        cfg = client.get("/api/config")
        assert cfg.status_code == 200
        assert "sidecarsecret" not in cfg.text
        js_cap = cfg.json()["capabilities"]["js_renderer"]
        assert js_cap["mode"] == "auto"
        assert js_cap["sidecar_configured"] is True
        assert "sidecarsecret" not in (js_cap["sidecar_url"] or "")
        assert js_cap["rendered_ok"] == 1

        resilience = client.get("/api/resilience")
        assert resilience.status_code == 200
        assert "sidecarsecret" not in resilience.text
        assert resilience.json()["js_renderer"]["fallbacks"] == 1

        metrics = client.get("/api/metrics")
        assert metrics.status_code == 200
        assert "sidecarsecret" not in metrics.text
        assert 'daas_js_renders_total{result="ok"} 1' in metrics.text
        assert 'daas_js_renders_total{result="fallback"} 1' in metrics.text
        assert 'daas_js_renders_total{result="error"} 1' in metrics.text

    # CLI doctor check (no secret leak, checks.js_renderer present)
    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    doc_res = CliRunner().invoke(cli_app, ["doctor"])
    assert doc_res.exit_code == 0, doc_res.output
    assert "sidecarsecret" not in doc_res.output
    json_blob = doc_res.output[: doc_res.output.rfind("}") + 1]
    parsed = json.loads(json_blob)
    assert "js_renderer" in parsed["checks"]

    # Deploy sidecar artifacts check
    sidecar_dir = ROOT / "deploy" / "playwright-sidecar"
    dockerfile = sidecar_dir / "Dockerfile"
    server_py = sidecar_dir / "server.py"
    compose = ROOT / "docker-compose.prod.yml"
    assert dockerfile.is_file()
    assert server_py.is_file()
    ast.parse(server_py.read_text(encoding="utf-8"))
    srv_text = server_py.read_text(encoding="utf-8")
    assert "/health" in srv_text and "/render" in srv_text
    cp_text = compose.read_text(encoding="utf-8")
    assert "playwright-sidecar:" in cp_text
    assert 'profiles: ["playwright"]' in cp_text or "- playwright" in cp_text
