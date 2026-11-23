"""Offline tests for P1-01: post-run data quality guards and offline gold-set eval."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from conftest import make_settings, read_fixture
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.main import create_app
from app.models import ApiSourceSpec, CssField, CssSchema, FieldSpec, PageContent, TaskSpec
from app.pipeline.delivery import DeltaDispatcher
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.utils import hash_text

API_BASE = "http://api.openprocurement.local/api/2.5"
GOLD_DIR = Path(__file__).resolve().parent / "fixtures" / "gold"


class _MappedFetcher:
    """Deterministic offline fetcher returning static responses per URL."""

    name = "fixture-map"

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses

    async def fetch(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        allow_json: bool = False,
        allow_cache: bool = True,
    ) -> FetchResult:
        status, body = self.responses.get(url, (404, "{}"))
        if status >= 400:
            return FetchResult(
                kind="permanent",
                error=f"HTTP {status}",
                retryable=False,
            )
        return FetchResult(
            kind="ok",
            page=PageContent(
                url=url,
                final_url=url,
                status_code=status,
                html=body,
                text="",
                markdown="",
                content_hash=hash_text(body),
                engine=self.name,
            ),
        )

    async def aclose(self) -> None:
        return None


def _prozorro_task_spec(*, broken: bool = False, required_amount: bool = True) -> TaskSpec:
    return TaskSpec(
        title="Тендери Prozorro",
        prompt="Моніторинг тендерів Prozorro",
        entity="tender",
        extraction="api_json",
        seeds=[f"{API_BASE}/tenders?limit=10"],
        columns=[
            FieldSpec(key="tender_id", title="ID", dtype="string", required=True, method="api_json"),
            FieldSpec(key="title", title="Предмет", dtype="string", required=True, method="api_json"),
            FieldSpec(
                key="amount",
                title="Очікувана вартість",
                dtype="number",
                required=required_amount,
                method="api_json",
            ),
            FieldSpec(key="status", title="Статус", dtype="string", method="api_json"),
            FieldSpec(key="cpv", title="CPV", dtype="string", method="api_json"),
            FieldSpec(key="region", title="Регіон", dtype="string", method="api_json"),
        ],
        api_source=ApiSourceSpec(
            items_path="data",
            id_path="id",
            field_map={
                "tender_id": "tenderID",
                "title": "nonexistent.broken_path" if broken else "title",
                "amount": "nonexistent.amount" if broken else "value.amount",
                "status": "status",
                "cpv": "items.0.classification.id",
                "region": "procuringEntity.address.region",
            },
            detail_url_template=f"{API_BASE}/tenders/{{id}}",
            incremental=False,
        ),
    )


def test_post_run_quality_guards_pass_and_warn(tmp_path: Path) -> None:
    """REQ-QUAL-001: healthy run passes; >50% row drop or empty required column emits quality_warning without losing rows."""
    settings = make_settings(
        tmp_path,
        quality_max_drop_ratio=0.5,
        quality_min_required_fill_pct=80.0,
    )

    async def scenario() -> None:
        list_full = json.dumps(
            {
                "data": [
                    {"id": "t1", "dateModified": "2026-11-23T10:00:00Z"},
                    {"id": "t2", "dateModified": "2026-11-23T10:01:00Z"},
                    {"id": "t3", "dateModified": "2026-11-23T10:02:00Z"},
                    {"id": "t4", "dateModified": "2026-11-23T10:03:00Z"},
                ]
            }
        )
        list_dropped = json.dumps(
            {
                "data": [
                    {"id": "t1", "dateModified": "2026-11-23T11:00:00Z"},
                ]
            }
        )
        t_good = lambda tid: json.dumps(  # noqa: E731
            {
                "data": {
                    "id": tid,
                    "tenderID": f"UA-{tid}",
                    "title": f"Лот {tid}",
                    "value": {"amount": 50000},
                    "status": "active.tendering",
                }
            }
        )
        t1_no_amount = json.dumps(
            {
                "data": {
                    "id": "t1",
                    "tenderID": "UA-t1",
                    "title": "Лот t1 без суми",
                    "status": "active.tendering",
                }
            }
        )

        responses: dict[str, tuple[int, str]] = {
            f"{API_BASE}/tenders?limit=10": (200, list_full),
            f"{API_BASE}/tenders/t1": (200, t_good("t1")),
            f"{API_BASE}/tenders/t2": (200, t_good("t2")),
            f"{API_BASE}/tenders/t3": (200, t_good("t3")),
            f"{API_BASE}/tenders/t4": (200, t_good("t4")),
        }

        studio = Studio(settings, fetcher=_MappedFetcher(responses))
        try:
            task = studio.store.create_task(_prozorro_task_spec(required_amount=True))

            # Run 1 (healthy): 4 rows, all required fields filled -> passed=True, no quality_warning event.
            run1 = await studio.collect(task.id, skip_calibration=True)
            done1 = await studio.wait(run1.id)
            assert done1 is not None and done1.status == "succeeded"

            versions1 = studio.store.dataset_versions(task.id)
            assert len(versions1) == 1
            qc1 = versions1[0]["manifest"]["quality_checks"]
            assert qc1["passed"] is True
            assert qc1["row_count"] == 4
            assert qc1["issues"] == []
            events1 = [e.type for e in studio.store.list_events(task.id)]
            assert "quality_warning" not in events1

            # Run 2 (anomalous): only 1 record survives (75% drop > 50%) AND required 'amount' missing.
            responses[f"{API_BASE}/tenders?limit=10"] = (200, list_dropped)
            responses[f"{API_BASE}/tenders/t1"] = (200, t1_no_amount)
            # Clear prior version's amount on t1? Simulate fresh dataset drop by forcing a new record only,
            # OR let's use a second task for clean isolation on required_field_empty and this task for drop!
            run2 = await studio.collect(task.id, skip_calibration=True, force=True)
            done2 = await studio.wait(run2.id)
            assert done2 is not None and done2.status == "succeeded"

            versions2 = studio.store.dataset_versions(task.id)
            assert len(versions2) == 2
            latest_qc = versions2[0]["manifest"]["quality_checks"]
            assert latest_qc["passed"] is False
            assert latest_qc["observed_count"] == 1
            assert latest_qc["previous_row_count"] == 4
            codes = {issue["code"] for issue in latest_qc["issues"]}
            assert "row_count_drop" in codes

            warn_events = [e for e in studio.store.list_events(task.id) if e.type == "quality_warning"]
            assert len(warn_events) == 1
            assert warn_events[0].level == "warning"
            assert studio.analytics.count_records(task.id) == 4

            # Check required_field_empty on a fresh task whose required column is 0% filled.
            task_req = studio.store.create_task(_prozorro_task_spec(required_amount=True))
            responses[f"{API_BASE}/tenders?limit=10"] = (200, list_dropped)
            responses[f"{API_BASE}/tenders/t1"] = (200, t1_no_amount)
            run_req = await studio.collect(task_req.id, skip_calibration=True)
            done_req = await studio.wait(run_req.id)
            assert done_req is not None and done_req.status == "succeeded"
            qc_req = studio.store.dataset_versions(task_req.id)[0]["manifest"]["quality_checks"]
            assert qc_req["passed"] is False
            req_issues = [i for i in qc_req["issues"] if i["code"] == "required_field_empty"]
            assert req_issues and req_issues[0]["field"] == "amount"
        finally:
            await studio.aclose()

    asyncio.run(scenario())


def test_quality_warning_sends_owner_alert(tmp_path: Path) -> None:
    """REQ-QUAL-001: quality anomaly sends quality_warning owner alert and survives delivery failure."""
    settings = make_settings(
        tmp_path,
        allow_private_network=False,
        quality_min_required_fill_pct=90.0,
    )

    async def scenario() -> None:
        sent_payloads: list[dict] = []
        fail_delivery = False

        def handler(request: httpx.Request) -> httpx.Response:
            if fail_delivery:
                return httpx.Response(500, json={"error": "boom"})
            sent_payloads.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True})

        list_one = json.dumps({"data": [{"id": "t1"}]})
        t1_missing_amount = json.dumps(
            {
                "data": {
                    "id": "t1",
                    "tenderID": "UA-t1",
                    "title": "Лот без обов'язкової ціни",
                    "status": "active",
                }
            }
        )
        responses = {
            f"{API_BASE}/tenders?limit=10": (200, list_one),
            f"{API_BASE}/tenders/t1": (200, t1_missing_amount),
        }

        studio = Studio(settings, fetcher=_MappedFetcher(responses))
        await studio.dispatcher.aclose()
        mock_dispatcher = DeltaDispatcher(
            settings,
            studio.store,
            transport=httpx.MockTransport(handler),
            retry_delay_s=0.0,
        )
        studio.dispatcher = mock_dispatcher
        studio.runner.dispatcher = mock_dispatcher
        try:
            task = studio.store.create_task(_prozorro_task_spec(required_amount=True))
            sub = studio.create_subscription(
                task.id,
                name="Owner Alerts",
                channel="webhook",
                target="https://alerts.example/hook",
            )
            # Update notify_on_error=True
            studio.update_subscription(
                task.id,
                sub.id,
                filters=sub.filters.model_copy(update={"notify_on_error": True}),
            )

            run = await studio.collect(task.id, skip_calibration=True)
            finished = await studio.wait(run.id)
            assert finished is not None and finished.status == "succeeded"

            alerts = [p for p in sent_payloads if p.get("event") == "alert"]
            assert len(alerts) == 1
            assert alerts[0]["alert_kind"] == "quality_warning"
            assert "amount" in alerts[0]["message"]

            # Even if delivery fails with 500, valid run & dataset version still succeed
            fail_delivery = True
            run2 = await studio.collect(task.id, skip_calibration=True, force=True)
            finished2 = await studio.wait(run2.id)
            assert finished2 is not None and finished2.status == "succeeded"
            assert len(studio.store.dataset_versions(task.id)) == 2
        finally:
            await studio.aclose()

    asyncio.run(scenario())


def test_gold_eval_api_json_and_html_regression_gate(tmp_path: Path) -> None:
    """REQ-QUAL-002: offline gold eval measures P/R/F1/coverage on API JSON and HTML and catches regressions."""
    settings = make_settings(tmp_path, eval_min_f1=0.85)

    async def scenario() -> None:
        studio = Studio(settings, fetcher=_MappedFetcher({}))
        try:
            # 1) API JSON task -> should achieve F1 == 1.0 on prozorro_gold
            api_task = studio.store.create_task(_prozorro_task_spec(broken=False))
            report = await studio.evaluate_task(api_task.id, gold_preset="prozorro_gold")
            assert report.passed is True
            assert report.f1 == 1.0
            assert report.precision == 1.0
            assert report.recall == 1.0
            assert report.llm_fallback_pct == 0.0
            assert report.usd_per_1k_records == 0.0
            assert report.records_expected == 2
            assert report.records_extracted == 2
            assert "amount" in report.fields
            assert report.fields["amount"].f1 == 1.0

            eval_events = [e for e in studio.store.list_events(api_task.id) if e.type == "eval_completed"]
            assert len(eval_events) == 1
            assert eval_events[0].level == "info"
            # PII/field values must not leak into event logs
            assert "UA-2026-11-23-000101-a" not in json.dumps(eval_events[0].data)

            # 2) Degraded API mapping -> F1 drops below threshold -> passed=False
            broken_task = studio.store.create_task(_prozorro_task_spec(broken=True))
            bad_report = await studio.evaluate_task(broken_task.id, gold_preset="prozorro_gold")
            assert bad_report.passed is False
            assert bad_report.f1 < 0.85

            # 3) HTML task with learned/explicit CssSchema -> eval on clinics_gold + persist into manifest.eval
            html_spec = TaskSpec(
                title="Клініки Києва",
                prompt="Зібрати клініки з каталогу",
                entity="clinic",
                extraction="css",
                seeds=["https://clinics.example/catalog.html"],
                columns=[
                    FieldSpec(key="name", title="Назва", dtype="string", required=True),
                    FieldSpec(key="city", title="Місто", dtype="string"),
                    FieldSpec(key="phone", title="Телефон", dtype="phone"),
                    FieldSpec(key="rating", title="Рейтинг", dtype="number"),
                    FieldSpec(key="website", title="Сайт", dtype="url"),
                ],
            )
            html_task = studio.store.create_task(html_spec)
            css_schema = CssSchema(
                container="article.clinic-card",
                item_scope=True,
                fields=[
                    CssField(key="name", selector="h3.clinic-card__name a"),
                    CssField(key="city", selector="div.clinic-card__city"),
                    CssField(key="phone", selector="div.clinic-card__phone"),
                    CssField(key="rating", selector="div.clinic-card__rating", dtype="number"),
                    CssField(key="website", selector="a.clinic-card__site", attr="href", dtype="url"),
                ],
            )
            studio.store.save_selector("clinics.example", "auto:clinic", css_schema)
            # Create a dataset version first so we verify manifest.eval is saved
            run = studio.store.create_run(html_task.id, "collect")
            studio.store.add_dataset_version(
                html_task.id, 1, run.id, 3, {"rows": 3, "quality_checks": {"passed": True}}
            )

            html_report = await studio.evaluate_task(html_task.id, gold_preset="clinics_gold")
            assert html_report.passed is True
            assert html_report.f1 == 1.0
            assert html_report.dataset_version == 1

            latest_manifest = studio.store.dataset_versions(html_task.id)[0]["manifest"]
            assert latest_manifest["quality_checks"] == {"passed": True}
            assert latest_manifest["eval"]["f1"] == 1.0
            assert latest_manifest["eval"]["passed"] is True
        finally:
            await studio.aclose()

    asyncio.run(scenario())


def test_eval_endpoint_and_cli_exit_codes(tmp_path: Path, monkeypatch) -> None:
    """REQ-QUAL-002: HTTP POST /api/tasks/{id}/eval validates inputs/LFI and CLI daas eval exits 0/1."""
    from app.config import reset_settings_cache

    settings = make_settings(tmp_path, eval_min_f1=0.8)
    monkeypatch.setenv("DAAS_STORAGE_DIR", str(settings.paths.root))
    monkeypatch.setenv("DAAS_OFFLINE", "true")
    monkeypatch.setenv("DAAS_LLM_PROVIDER", "disabled")
    reset_settings_cache()

    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "prompt": "Тендери Prozorro API",
                "seeds": [f"{API_BASE}/tenders?limit=10"],
                "api_source": _prozorro_task_spec().api_source.model_dump(mode="json"),
            },
        )
        # Create a clean task directly or update columns to match prozorro_gold fields
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]
        spec_payload = _prozorro_task_spec().model_dump(mode="json")
        upd = client.patch(f"/api/tasks/{task_id}", json={"spec": spec_payload})
        assert upd.status_code == 200

        # Inline cases eval
        raw_tender = read_fixture("prozorro_tender_001.json")
        inline_resp = client.post(
            f"/api/tasks/{task_id}/eval",
            json={
                "cases": [
                    {
                        "url": f"{API_BASE}/tenders/tender-001",
                        "content": raw_tender,
                        "expected": [
                            {
                                "tender_id": "UA-2026-11-23-000101-a",
                                "title": "Послуги з модернізації серверної інфраструктури",
                                "amount": 1250000.0,
                            }
                        ],
                    }
                ],
                "min_f1": 0.9,
            },
        )
        assert inline_resp.status_code == 200
        assert inline_resp.json()["report"]["passed"] is True
        assert inline_resp.json()["report"]["f1"] == 1.0

        # Preset eval
        preset_resp = client.post(
            f"/api/tasks/{task_id}/eval",
            json={"gold_preset": "prozorro_gold", "min_f1": 0.9},
        )
        assert preset_resp.status_code == 200
        assert preset_resp.json()["report"]["passed"] is True

        # Path traversal attempt in gold_preset -> 400
        lfi_resp = client.post(
            f"/api/tasks/{task_id}/eval",
            json={"gold_preset": "../../etc/passwd"},
        )
        assert lfi_resp.status_code == 400

        # Empty body -> 400
        empty_resp = client.post(f"/api/tasks/{task_id}/eval", json={})
        assert empty_resp.status_code == 400

        # Missing task -> 404
        missing_resp = client.post(
            "/api/tasks/nonexistent/eval",
            json={"gold_preset": "prozorro_gold"},
        )
        assert missing_resp.status_code == 404

        # Bad broken task for CLI failure exit code
        bad_created = client.post(
            "/api/tasks",
            json={
                "prompt": "Зламаний мапінг Prozorro API",
                "seeds": [f"{API_BASE}/tenders?limit=10"],
            },
        )
        bad_id = bad_created.json()["task"]["id"]
        client.patch(
            f"/api/tasks/{bad_id}",
            json={"spec": _prozorro_task_spec(broken=True).model_dump(mode="json")},
        )

    runner = CliRunner()
    gold_file = GOLD_DIR / "prozorro_gold.json"

    res_ok = runner.invoke(
        cli_app,
        ["eval", "--task", task_id, "--gold", str(gold_file), "--min-f1", "0.9"],
    )
    assert res_ok.exit_code == 0, res_ok.output
    assert "F1=100.0%" in res_ok.output or "1.00" in res_ok.output

    res_fail = runner.invoke(
        cli_app,
        ["eval", "--task", bad_id, "--gold", str(gold_file), "--min-f1", "0.9"],
    )
    assert res_fail.exit_code == 1
