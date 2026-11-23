"""Offline tests for P1-08: record quarantine validation, DuckDB migration, API & events."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from conftest import make_settings
from fastapi.testclient import TestClient

from app.db.duckdb_store import AnalyticsStore
from app.extraction.engine import ExtractionEngine
from app.llm.router import LLMRouter
from app.main import create_app
from app.models import ApiSourceSpec, CssField, CssSchema, FieldSpec, PageContent, TaskSpec
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.scraping.page import html_to_markdown, html_to_text
from app.utils import hash_text

TERMINAL = {"succeeded", "failed", "partial", "cancelled"}
API_BASE = "http://api.openprocurement.local/api/2.5"
HTML_BASE = "http://catalog.local"


class _MappedFetcher:
    name = "mapped-fixture"

    def __init__(self, responses: dict[str, tuple[int, str]]) -> None:
        self.responses = responses

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        entry = self.responses.get(url)
        if entry is None:
            return FetchResult(kind="permanent", status=404, error=f"no fixture for {url}")
        status, body = entry
        if status >= 400:
            return FetchResult(kind="transient" if status >= 500 else "permanent", status=status, error=f"HTTP {status}")
        is_html = body.lstrip().startswith("<")
        return FetchResult(
            status=status,
            kind="ok",
            page=PageContent(
                url=url,
                final_url=url,
                status_code=status,
                html=body,
                text=html_to_text(body) if is_html else body,
                markdown=html_to_markdown(body, url) if is_html else "",
                content_hash=hash_text(body),
                engine=self.name,
            ),
        )

    async def aclose(self) -> None:
        return None


def _wait(client: TestClient, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["run"]["status"] in TERMINAL:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


def test_old_duckdb_quarantine_is_migrated(tmp_path: Path) -> None:
    """REQ-HIST-040: existing DuckDB file without quarantine.id opens, migrates and lists legacy rows."""
    settings = make_settings(tmp_path)
    settings.paths.ensure()

    con = duckdb.connect(str(settings.paths.duckdb))
    con.execute(
        """
        CREATE TABLE record_versions (
            dataset_id VARCHAR NOT NULL,
            record_key VARCHAR NOT NULL,
            dataset_version INTEGER NOT NULL,
            revision BIGINT,
            run_id VARCHAR,
            source_url VARCHAR,
            content_hash VARCHAR,
            payload JSON,
            provenance JSON,
            observed_at TIMESTAMP NOT NULL
        );
        CREATE TABLE quarantine (
            dataset_id VARCHAR NOT NULL,
            run_id VARCHAR,
            source_url VARCHAR,
            reason VARCHAR,
            payload JSON,
            created_at TIMESTAMP NOT NULL
        );
        INSERT INTO quarantine VALUES (
            'task-legacy',
            'run-1',
            'https://example.com/item',
            'Невалідне число',
            '{"name": "Legacy Corp", "amount": "oops"}'::JSON,
            ?
        );
        """,
        [datetime.now(UTC).replace(tzinfo=None)],
    )
    con.close()

    analytics = AnalyticsStore(settings.paths)
    try:
        assert analytics.count_quarantine("task-legacy") == 1
        listed = analytics.list_quarantine("task-legacy", limit=20, offset=0)
        assert listed["total"] == 1
        item = listed["items"][0]
        assert isinstance(item["id"], str) and len(item["id"]) >= 8
        assert item["reason"] == "Невалідне число"
        assert item["payload"]["name"] == "Legacy Corp"
        assert analytics.get_quarantine_item("task-legacy", item["id"]) is not None
    finally:
        analytics.close()


@pytest.mark.asyncio
async def test_extraction_usability_counts_after_coercion_and_flags_quarantine(tmp_path: Path) -> None:
    """REQ-EXT-006 + REQ-HIST-040: HTML usability checks valid fields after dtype coercion and records quarantine."""
    settings = make_settings(tmp_path)
    router = LLMRouter(settings)
    engine = ExtractionEngine(settings, router)
    try:
        # 1) Record with 1 valid field ('name') and 1 uncoercible field ('price' = 'не число')
        # Previously _is_usable counted len(values)==2 before dropping 'price'.
        spec_product = TaskSpec(
            title="Товари",
            prompt="Товари з ціною",
            entity="product",
            columns=[
                FieldSpec(key="name", title="Назва", dtype="string"),
                FieldSpec(key="price", title="Ціна", dtype="number"),
            ],
            seeds=["https://shop.example/p1"],
        )
        broken_jsonld_html = """<!doctype html><html><head>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Товар Без Ціни","offers":{"price":"не число"}}
        </script></head><body><h1>Товар Без Ціни</h1></body></html>"""
        page = PageContent(
            url="https://shop.example/p1",
            final_url="https://shop.example/p1",
            html=broken_jsonld_html,
            text=html_to_text(broken_jsonld_html),
            markdown=html_to_markdown(broken_jsonld_html, "https://shop.example/p1"),
            content_hash=hash_text(broken_jsonld_html),
        )
        outcome = await engine.extract(page, spec_product, allow_llm=False)
        assert outcome.items == []
        assert len(outcome.quarantined) == 1
        assert outcome.quarantined[0]["payload"]["name"] == "Товар Без Ціни"
        assert "price" in outcome.quarantined[0]["reason"] or "2" in outcome.quarantined[0]["reason"]

        # 2) CSS item with >=2 fields but NO identity column (neither name/title/company/website/phone/email/address)
        spec_no_id = TaskSpec(
            title="Метрики",
            prompt="Місто і рейтинг",
            entity="metric",
            columns=[
                FieldSpec(key="city", title="Місто", dtype="string"),
                FieldSpec(key="rating", title="Рейтинг", dtype="number"),
            ],
            seeds=["https://shop.example/m1"],
        )
        schema_no_id = CssSchema(
            container="div.row",
            item_scope=True,
            fields=[
                CssField(key="city", selector=".city"),
                CssField(key="rating", selector=".rating", dtype="number"),
            ],
        )
        html_no_id = '<div class="row"><span class="city">Київ</span><span class="rating">4.9</span></div>'
        page_no_id = PageContent(
            url="https://shop.example/m1",
            final_url="https://shop.example/m1",
            html=html_no_id,
            text=html_to_text(html_no_id),
            markdown="",
            content_hash=hash_text(html_no_id),
        )
        outcome2 = await engine.extract(page_no_id, spec_no_id, css_schema=schema_no_id, allow_llm=False)
        assert outcome2.items == []
        assert len(outcome2.quarantined) == 1
        assert "ідентифікац" in outcome2.quarantined[0]["reason"].lower()
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_runner_quarantines_suspect_html_and_api_records(tmp_path: Path) -> None:
    """REQ-HIST-040: suspect HTML and API records route to quarantine with stats/events while healthy records persist."""
    html_doc = """<!doctype html><html><body>
      <article class="row"><h3 class="nm">Добра Клініка</h3><span class="rt">4.7</span></article>
      <article class="row"><h3 class="nm">Пошкоджена Клініка</h3><span class="rt">немає-числа</span></article>
    </body></html>"""

    list_json = json.dumps(
        {
            "data": [
                {"id": "t1"},
                {"id": "t2"},
                {"id": "t3"},
                {"id": "t4"},
            ]
        }
    )
    responses: dict[str, tuple[int, str]] = {
        f"{HTML_BASE}/list.html": (200, html_doc),
        f"{API_BASE}/tenders": (200, list_json),
        # t1: fully valid
        f"{API_BASE}/tenders/t1": (
            200,
            json.dumps({"data": {"id": "t1", "tenderID": "UA-1", "title": "Лот 1", "value": {"amount": 1000}}}),
        ),
        # t2: optional/required field 'amount' simply absent in JSON -> NOT quarantined (handled by REQ-QUAL-001)
        f"{API_BASE}/tenders/t2": (
            200,
            json.dumps({"data": {"id": "t2", "tenderID": "UA-2", "title": "Лот 2", "status": "active"}}),
        ),
        # t3: present 'value.amount' is invalid non-numeric string -> quarantined with reason
        f"{API_BASE}/tenders/t3": (
            200,
            json.dumps({"data": {"id": "t3", "tenderID": "UA-3", "title": "Лот 3", "value": {"amount": "невідомо"}}}),
        ),
        # t4: too sparse (< 2 populated fields) -> quarantined
        f"{API_BASE}/tenders/t4": (
            200,
            json.dumps({"data": {"id": "t4", "tenderID": "UA-4"}}),
        ),
    }

    settings = make_settings(tmp_path)
    studio = Studio(settings, fetcher=_MappedFetcher(responses))
    try:
        # 1) HTML task
        html_spec = TaskSpec(
            title="HTML тест карантину",
            prompt="Клініки і рейтинг",
            entity="clinic",
            extraction="css",
            seeds=[f"{HTML_BASE}/list.html"],
            columns=[
                FieldSpec(key="name", title="Назва", dtype="string"),
                FieldSpec(key="rating", title="Рейтинг", dtype="number"),
            ],
        )
        html_task = studio.store.create_task(html_spec)
        studio.store.save_selector(
            "catalog.local",
            "auto:clinic",
            CssSchema(
                container="article.row",
                item_scope=True,
                fields=[
                    CssField(key="name", selector="h3.nm"),
                    CssField(key="rating", selector="span.rt", dtype="number"),
                ],
            ),
        )
        run_html = await studio.collect(html_task.id, skip_calibration=True)
        done_html = await studio.wait(run_html.id, timeout=20)
        assert done_html is not None and done_html.status == "succeeded"
        assert done_html.stats.records_new == 1
        assert done_html.stats.records_quarantined == 1
        assert studio.analytics.count_records(html_task.id) == 1
        q_html = studio.analytics.list_quarantine(html_task.id)
        assert q_html["total"] == 1
        assert q_html["items"][0]["payload"]["name"] == "Пошкоджена Клініка"
        html_events = [e for e in studio.store.list_events(html_task.id) if e.type == "record_quarantined"]
        assert len(html_events) == 1
        assert html_events[0].level == "warning"

        # 2) API JSON task
        api_spec = TaskSpec(
            title="API тест карантину",
            prompt="Тендери Prozorro",
            entity="tender",
            extraction="api_json",
            seeds=[f"{API_BASE}/tenders"],
            columns=[
                FieldSpec(key="tender_id", title="ID", dtype="string", required=True, method="api_json"),
                FieldSpec(key="title", title="Предмет", dtype="string", required=True, method="api_json"),
                FieldSpec(key="amount", title="Сума", dtype="number", required=True, method="api_json"),
                FieldSpec(key="status", title="Статус", dtype="string", method="api_json"),
            ],
            api_source=ApiSourceSpec(
                items_path="data",
                id_path="id",
                field_map={
                    "tender_id": "tenderID",
                    "title": "title",
                    "amount": "value.amount",
                    "status": "status",
                },
                detail_url_template=f"{API_BASE}/tenders/{{id}}",
                incremental=False,
            ),
        )
        api_task = studio.store.create_task(api_spec)
        run_api = await studio.collect(api_task.id, skip_calibration=True)
        done_api = await studio.wait(run_api.id, timeout=20)
        assert done_api is not None and done_api.status == "succeeded"
        assert done_api.stats.records_new == 2  # t1 and t2
        assert done_api.stats.records_quarantined == 2  # t3 and t4
        assert studio.analytics.count_records(api_task.id) == 2
        q_api = studio.analytics.list_quarantine(api_task.id)
        assert q_api["total"] == 2
        reasons = " | ".join(item["reason"] for item in q_api["items"])
        assert "amount" in reasons or "Сума" in reasons
        assert "2" in reasons
        manifest = studio.store.dataset_manifest(api_task.id)
        assert manifest["quarantined"] == 2
    finally:
        await studio.aclose()


def test_quarantine_api_list_approve_dismiss_and_404(tmp_path: Path) -> None:
    """REQ-HIST-041: HTTP API lists quarantined records, approves to record_versions, dismisses, and handles 404."""
    list_json = json.dumps({"data": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}]})
    responses = {
        f"{API_BASE}/tenders": (200, list_json),
        f"{API_BASE}/tenders/t1": (
            200,
            json.dumps({"data": {"id": "t1", "tenderID": "UA-1", "title": "Валідний лот", "value": {"amount": 2500}}}),
        ),
        f"{API_BASE}/tenders/t2": (
            200,
            json.dumps({"data": {"id": "t2", "tenderID": "UA-2", "title": "Кандидат для апруву", "value": {"amount": "N/A"}}}),
        ),
        f"{API_BASE}/tenders/t3": (
            200,
            json.dumps({"data": {"id": "t3", "tenderID": "UA-3"}}),
        ),
    }

    settings = make_settings(tmp_path)
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=_MappedFetcher(responses)))
    with TestClient(app) as client:
        api_source = {
            "items_path": "data",
            "id_path": "id",
            "detail_url_template": f"{API_BASE}/tenders/{{id}}",
            "incremental": False,
            "field_map": {
                "tender_id": "tenderID",
                "title": "title",
                "amount": "value.amount",
            },
        }
        created = client.post(
            "/api/tasks",
            json={"prompt": "Тендери Prozorro API", "seeds": [f"{API_BASE}/tenders"], "api_source": api_source},
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]

        run_resp = client.post(f"/api/tasks/{task_id}/run", json={})
        assert run_resp.status_code == 200
        finished = _wait(client, run_resp.json()["run"]["id"])
        assert finished["run"]["stats"]["records_new"] == 1
        assert finished["run"]["stats"]["records_quarantined"] == 2

        task_detail = client.get(f"/api/tasks/{task_id}").json()
        assert task_detail["quarantine_count"] == 2

        q_resp = client.get(f"/api/tasks/{task_id}/quarantine")
        assert q_resp.status_code == 200
        q_data = q_resp.json()
        assert q_data["total"] == 2
        assert len(q_data["items"]) == 2

        to_approve = next(item for item in q_data["items"] if item["payload"].get("tender_id") == "UA-2")
        to_dismiss = next(item for item in q_data["items"] if item["payload"].get("tender_id") == "UA-3")

        # Approve promotes into record_versions & removes from quarantine
        app_resp = client.post(f"/api/tasks/{task_id}/quarantine/{to_approve['id']}/approve")
        assert app_resp.status_code == 200, app_resp.text
        approved_body = app_resp.json()
        assert approved_body["row_count"] == 2
        assert approved_body["record"]["payload"]["title"] == "Кандидат для апруву"
        assert approved_body["record"]["provenance"]["title"]["method"] == "manual"

        recs = client.get(f"/api/tasks/{task_id}/records").json()
        assert recs["total"] == 2

        # Dismiss deletes from quarantine without adding to records
        del_resp = client.delete(f"/api/tasks/{task_id}/quarantine/{to_dismiss['id']}")
        assert del_resp.status_code == 200
        assert del_resp.json() == {"deleted": True}
        assert client.get(f"/api/tasks/{task_id}/quarantine").json()["total"] == 0
        assert client.get(f"/api/tasks/{task_id}/records").json()["total"] == 2
        assert client.get(f"/api/tasks/{task_id}").json()["quarantine_count"] == 0

        # Audit events recorded
        event_types = [e["type"] for e in client.get(f"/api/tasks/{task_id}/events").json()["items"]]
        assert "quarantine_approved" in event_types
        assert "quarantine_dismissed" in event_types

        # 404 cases
        assert client.post(f"/api/tasks/{task_id}/quarantine/missing/approve").status_code == 404
        assert client.delete(f"/api/tasks/{task_id}/quarantine/missing").status_code == 404
        assert client.get("/api/tasks/nonexistent/quarantine").status_code == 404
