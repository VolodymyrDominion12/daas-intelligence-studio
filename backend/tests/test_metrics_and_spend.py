"""Tests for P1-09: plan-phase LLM spend attribution, concurrent ledger isolation, and /api/metrics."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import DEMO_BASE, FixtureFetcher, make_settings
from fastapi.testclient import TestClient

from app.compliance import run_scope
from app.llm.base import LLMRequest, LLMResponse
from app.main import create_app
from app.runtime import Studio


class _PayingPlanProvider:
    name = "fake-cloud"
    model = "fake-gpt-4o-mini"
    is_local = False
    available = True

    def __init__(self, usd: float = 0.0125) -> None:
        self.usd = usd
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        data = {
            "title": "Стоматологічні клініки",
            "entity": "clinic",
            "rationale": "Опис плану з фейкового LLM",
            "columns": [
                {"key": "name", "title": "Назва", "dtype": "string", "method": "css"},
                {"key": "city", "title": "Місто", "dtype": "string", "method": "css"},
                {"key": "phone", "title": "Телефон", "dtype": "phone", "method": "css"},
                {"key": "website", "title": "Сайт", "dtype": "url", "method": "css"},
            ],
            "enrichment": [],
            "assumptions": [],
        }
        return LLMResponse(
            provider=self.name,
            model=self.model,
            data=data,
            text=json.dumps(data, ensure_ascii=False),
            tokens_in=200,
            tokens_out=80,
            usd=self.usd,
        )

    async def aclose(self) -> None:
        return None


class _ConcurrentScriptedProvider:
    name = "concurrent-mock"
    model = "concurrent-1"
    is_local = False
    available = True

    def __init__(self, delay_by_prompt: dict[str, tuple[float, float]]) -> None:
        self.delay_by_prompt = delay_by_prompt

    async def complete(self, request: LLMRequest) -> LLMResponse:
        delay, usd = self.delay_by_prompt[request.user]
        await asyncio.sleep(delay)
        return LLMResponse(
            provider=self.name,
            model=self.model,
            data={"ok": True, "user": request.user},
            text="{}",
            tokens_in=50,
            tokens_out=20,
            usd=usd,
        )

    async def aclose(self) -> None:
        return None


def test_plan_spend_attributed_to_created_task(tmp_path: Path) -> None:
    """REQ-PLAN-013: planning LLM spend is attributed to the created task and exposed via API."""
    settings = make_settings(
        tmp_path,
        disable_auth=False,
        api_token="plan-spend-token-1234",
        llm_provider="auto",
        llm_cache=False,
    )
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=FixtureFetcher()))

    with TestClient(app) as client:
        studio: Studio = app.state.studio
        provider = _PayingPlanProvider(usd=0.0125)
        studio.router.providers = [provider]

        headers = {"Authorization": "Bearer plan-spend-token-1234"}
        res = client.post(
            "/api/tasks",
            headers=headers,
            json={"prompt": "Збери клініки Києва", "seeds": [f"{DEMO_BASE}/catalog.html"]},
        )
        assert res.status_code == 201, res.text
        task_id = res.json()["task"]["id"]

        assert provider.calls == 1
        assert studio.store.task_spend(task_id) == pytest.approx(0.0125)

        rows = studio.store._all(
            "SELECT task_id, run_id, purpose, usd FROM llm_calls WHERE task_id = ?",
            (task_id,),
        )
        assert len(rows) == 1
        assert rows[0]["purpose"] == "plan"
        assert str(rows[0]["run_id"] or "").startswith("plan:")
        assert rows[0]["usd"] == pytest.approx(0.0125)

        events = [e for e in studio.store.list_events(task_id) if e.type == "task_created"]
        assert len(events) == 1
        assert events[0].data["plan_llm_usd"] == pytest.approx(0.0125)

        detail = client.get(f"/api/tasks/{task_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["spend_usd"] == pytest.approx(0.0125)

        # Failed task creation with logged-in URL must fail 403 and must not leak spend onto task_id
        rejected = client.post(
            "/api/tasks",
            headers=headers,
            json={
                "prompt": "Збери клініки",
                "seeds": ["https://user:secret@clinics.example/catalog.html"],
            },
        )
        assert rejected.status_code == 403
        assert studio.store.task_spend(task_id) == pytest.approx(0.0125)


def test_router_attribution_isolated_across_concurrent_scopes(tmp_path: Path) -> None:
    """REQ-LLM-007: concurrent run_scopes do not cross-contaminate llm_calls task_id/run_id."""
    settings = make_settings(tmp_path, llm_provider="auto", llm_cache=False)

    async def scenario() -> None:
        studio = Studio(settings, fetcher=FixtureFetcher())
        try:
            # Bind stale task/run on shared router to verify run_scope & request take precedence
            studio.router.bind(task_id="stale-task", run_id="stale-run")
            studio.router.providers = [
                _ConcurrentScriptedProvider(
                    {
                        "payload-A": (0.04, 0.011),
                        "payload-B": (0.01, 0.022),
                        "payload-plan": (0.02, 0.033),
                    }
                )
            ]

            async def call_a():
                with run_scope("task-A", "run-A"):
                    return await studio.router.complete_json(
                        LLMRequest(purpose="extraction", system="sys", user="payload-A")
                    )

            async def call_b():
                with run_scope("task-B", "run-B"):
                    return await studio.router.complete_json(
                        LLMRequest(purpose="ai_column", system="sys", user="payload-B")
                    )

            async def call_plan():
                return await studio.router.complete_json(
                    LLMRequest(
                        purpose="plan",
                        system="sys",
                        user="payload-plan",
                        task_id="task-plan",
                        run_id="plan:concurrent",
                    )
                )

            res_a, res_b, res_p = await asyncio.gather(call_a(), call_b(), call_plan())
            assert res_a.ok and res_b.ok and res_p.ok

            assert studio.store.task_spend("task-A") == pytest.approx(0.011)
            assert studio.store.run_spend("run-A") == pytest.approx(0.011)
            assert studio.store.task_spend("task-B") == pytest.approx(0.022)
            assert studio.store.run_spend("run-B") == pytest.approx(0.022)
            assert studio.store.task_spend("task-plan") == pytest.approx(0.033)
            assert studio.store.run_spend("plan:concurrent") == pytest.approx(0.033)
            assert studio.store.task_spend("stale-task") == 0.0
        finally:
            await studio.aclose()

    asyncio.run(scenario())


def test_prometheus_metrics_endpoint_requires_auth_and_reports_state(tmp_path: Path) -> None:
    """REQ-API-009: GET /api/metrics requires Bearer auth and exposes Prometheus counters/gauges."""
    settings = make_settings(
        tmp_path,
        disable_auth=False,
        api_token="metrics-secret-token-99",
        llm_provider="auto",
        llm_cache=False,
    )
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=FixtureFetcher()))

    with TestClient(app) as client:
        unauth = client.get("/api/metrics")
        assert unauth.status_code == 401
        assert "Bearer" in unauth.headers.get("www-authenticate", "")

        studio: Studio = app.state.studio
        studio.router.providers = [_PayingPlanProvider(usd=0.02)]

        headers = {"Authorization": "Bearer metrics-secret-token-99"}
        created = client.post(
            "/api/tasks",
            headers=headers,
            json={"prompt": "Збери клініки", "seeds": [f"{DEMO_BASE}/catalog.html"]},
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]

        # Turn off LLM before collect so run stays fast/deterministic
        studio.router.providers = []
        cal = client.post(f"/api/tasks/{task_id}/calibrate", headers=headers)
        assert cal.status_code == 200
        cal_id = cal.json()["run"]["id"]
        while client.get(f"/api/runs/{cal_id}", headers=headers).json()["run"]["status"] not in {
            "succeeded",
            "failed",
            "partial",
            "cancelled",
        }:
            pass

        col = client.post(f"/api/tasks/{task_id}/run", headers=headers, json={})
        assert col.status_code == 200
        col_id = col.json()["run"]["id"]
        while client.get(f"/api/runs/{col_id}", headers=headers).json()["run"]["status"] not in {
            "succeeded",
            "failed",
            "partial",
            "cancelled",
        }:
            pass

        # Seed a quarantine item to verify daas_records_quarantined > 0
        studio.analytics.quarantine(
            task_id, col_id, f"{DEMO_BASE}/catalog.html", "Тест", {"name": "X"}
        )

        resp = client.get("/api/metrics", headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text

        for expected in (
            "# TYPE daas_up gauge",
            "daas_up 1",
            "# TYPE daas_tasks_total gauge",
            'daas_tasks_total{status="done"} 1',
            "# TYPE daas_runs_total counter",
            'daas_runs_total{status="succeeded"}',
            "# TYPE daas_queue_jobs gauge",
            'daas_queue_jobs{status="queued"} 0',
            "# TYPE daas_pages_fetched_total counter",
            "# TYPE daas_blocked_total counter",
            'daas_blocked_total{reason="robots"}',
            'daas_blocked_total{reason="compliance"}',
            "# TYPE daas_records_current gauge",
            "daas_records_current 3",
            "# TYPE daas_records_quarantined gauge",
            "daas_records_quarantined 1",
            "# TYPE daas_llm_spend_usd_total counter",
            "daas_llm_spend_usd_total 0.02",
            "daas_llm_calls_total 1",
            "# TYPE daas_raw_cache_pages gauge",
        ):
            assert expected in body, f"Missing {expected!r} in:\n{body}"

        assert "metrics-secret-token-99" not in body
