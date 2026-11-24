"""Offline tests for 014: Prozorro Winners B2B profile and auto-enrichment triggers."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import FixtureFetcher, make_settings
from fastapi.testclient import TestClient

from app.discovery import SearchHit, SearchProvider
from app.extraction.api_json import extract_json_records
from app.llm.router import LLMRouter
from app.main import create_app
from app.models import (
    ApiSourceSpec,
    ChangeEvent,
    DeliveryFilters,
    FieldSpec,
    PageContent,
    SourcePolicy,
    TaskSpec,
)
from app.pipeline.delivery import DeltaDispatcher
from app.pipeline.planner import Planner
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.scraping.page import html_to_markdown, html_to_text
from app.utils import hash_text

TERMINAL = {"succeeded", "failed", "partial", "cancelled"}
API_BASE = "http://api.openprocurement.local/api/2.5"


def _wait(client: TestClient, run_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["run"]["status"] in TERMINAL:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish")


class _FakeSearch(SearchProvider):
    name = "fake-search"

    def __init__(self, mapping: dict[str, list[SearchHit]]) -> None:
        self.mapping = mapping
        self.queries: list[str] = []

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        self.queries.append(query)
        lowered = query.casefold()
        for needle, hits in self.mapping.items():
            if needle.casefold() in lowered:
                return hits[:limit]
        return []


class _WinnersFixtureFetcher(FixtureFetcher):
    def __init__(self, json_docs: dict[str, dict], html_docs: dict[str, str]) -> None:
        super().__init__(routes={})
        self.json_docs = json_docs
        self.html_docs = html_docs

    async def fetch(
        self, url: str, *, force_refresh: bool = False, allow_json: bool = False
    ) -> FetchResult:
        self.calls.append(url)
        if url in self.json_docs:
            if not allow_json:
                return FetchResult(kind="permanent", status=415, error="JSON requires allow_json")
            raw = json.dumps(self.json_docs[url], ensure_ascii=False)
            return FetchResult(
                page=PageContent(
                    url=url,
                    final_url=url,
                    status_code=200,
                    html=raw,
                    text=raw,
                    content_hash=hash_text(raw),
                    engine="fixture",
                ),
                status=200,
            )
        normalized = url.rstrip("/")
        html_body = self.html_docs.get(url) or self.html_docs.get(normalized)
        if html_body is not None:
            return FetchResult(
                page=PageContent(
                    url=url,
                    final_url=url,
                    status_code=200,
                    html=html_body,
                    text=html_to_text(html_body),
                    markdown=html_to_markdown(html_body, url),
                    content_hash=hash_text(html_body),
                    engine="fixture",
                ),
                status=200,
            )
        return FetchResult(kind="permanent", status=404, error=f"no fixture for {url}")


@pytest.mark.asyncio
async def test_prozorro_winners_preset_and_trigger_config(tmp_path: Path) -> None:
    """REQ-PLAN-015: Prozorro Winners prompt builds B2B intent spec; basic Prozorro stays unchanged."""
    settings = make_settings(tmp_path)
    store = None
    router = None
    from app.db.sqlite_store import OperationalStore

    store = OperationalStore(settings.paths)
    router = LLMRouter(settings, store)
    planner = Planner(settings, router, enrichers=["domain_resolver", "site_audit"], store=store)
    try:
        winners_spec = await planner.plan(
            "Знайди переможців тендерів Prozorro через API та проведи аудит їх сайтів",
            ["https://public.api.openprocurement.org/api/2.5/tenders"],
        )
        assert winners_spec.extraction == "api_json"
        assert winners_spec.api_source is not None
        assert not winners_spec.pii_columns
        cols = winners_spec.column_map
        assert {"winner_name", "winner_edrpou", "award_amount", "award_status", "website"} <= set(cols)
        assert cols["email"].pii == "business_contact"
        assert cols["phone"].pii == "business_contact"
        assert cols["website"].method == "enrichment"
        assert cols["tech_stack"].method == "enrichment"
        assert [item.enricher for item in winners_spec.enrichment[:2]] == [
            "domain_resolver",
            "site_audit",
        ]
        assert winners_spec.enrichment[0].config.get("name_field") == "winner_name"
        assert winners_spec.auto_enrich is not None
        assert winners_spec.auto_enrich.enabled is True
        assert winners_spec.auto_enrich.min_amount == 1_000_000.0
        assert winners_spec.auto_enrich.require_fields == ["winner_name"]

        custom_threshold_spec = await planner.plan(
            "Переможці тендерів Prozorro API від 500000 грн",
            ["https://public.api.openprocurement.org/api/2.5/tenders"],
        )
        assert custom_threshold_spec.auto_enrich is not None
        assert custom_threshold_spec.auto_enrich.min_amount == 500_000.0

        basic_spec = await planner.plan(
            "Збери нові закупівлі та аукціони Prozorro.Продажі через відкритий API",
            ["https://public.api.openprocurement.org/api/0/auctions"],
        )
        assert basic_spec.auto_enrich is None
        assert "winner_name" not in basic_spec.column_map
    finally:
        await router.aclose()
        store.close()


def test_json_path_filtered_award_selection() -> None:
    """REQ-EXT-011: filtered list selector key[field=value] selects active award over failed ones."""
    spec = TaskSpec(
        title="Prozorro Winners",
        prompt="Переможці Prozorro API",
        entity="tender",
        columns=[
            FieldSpec(key="tender_id", title="ID", method="api_json"),
            FieldSpec(key="winner_name", title="Переможець", method="api_json"),
            FieldSpec(key="winner_edrpou", title="ЄДРПОУ", method="api_json"),
            FieldSpec(key="award_amount", title="Сума аварду", dtype="number", method="api_json"),
            FieldSpec(key="award_status", title="Статус аварду", method="api_json"),
        ],
        seeds=[f"{API_BASE}/tenders"],
        extraction="api_json",
        api_source=ApiSourceSpec(
            items_path="data",
            id_path="id",
            field_map={
                "tender_id": "tenderID",
                "winner_name": "awards[status=active].suppliers.0.name|awards.0.suppliers.0.name",
                "winner_edrpou": "awards[status=active].suppliers.0.identifier.id|awards.0.suppliers.0.identifier.id",
                "award_amount": "awards[status=active].value.amount|awards.0.value.amount",
                "award_status": "awards[status=active].status|awards.0.status",
            },
        ),
    )
    doc = {
        "data": {
            "id": "tender-multi-award",
            "tenderID": "UA-2026-11-24-000555-a",
            "awards": [
                {
                    "status": "unsuccessful",
                    "value": {"amount": 900000},
                    "suppliers": [{"name": "ТОВ Відхилений", "identifier": {"id": "11111111"}}],
                },
                {
                    "status": "active",
                    "value": {"amount": 1450000},
                    "suppliers": [{"name": "ТОВ МегаБуд Сервіс", "identifier": {"id": "22223333"}}],
                },
            ],
        }
    }
    records, warnings = extract_json_records(json.dumps(doc), spec, f"{API_BASE}/tenders/tender-multi-award")
    assert not warnings
    assert len(records) == 1
    assert records[0].payload["winner_name"] == "ТОВ МегаБуд Сервіс"
    assert records[0].payload["winner_edrpou"] == "22223333"
    assert records[0].payload["award_amount"] == 1450000.0
    assert records[0].payload["award_status"] == "active"

    missing_doc = {
        "data": {
            "id": "tender-no-active",
            "tenderID": "UA-2026-11-24-000556-a",
            "awards": [{"status": "unsuccessful"}],
        }
    }
    spec_strict = spec.model_copy(
        update={
            "api_source": spec.api_source.model_copy(
                update={"field_map": {"tender_id": "tenderID", "winner_name": "awards[status=active].suppliers.0.name"}}
            )
        }
    )
    records_missing, warnings_missing = extract_json_records(
        json.dumps(missing_doc), spec_strict, f"{API_BASE}/tenders/tender-no-active"
    )
    assert not warnings_missing
    assert len(records_missing) == 1
    assert "winner_name" not in records_missing[0].payload


def test_collect_winner_trigger_resolves_domain_and_audits_site(tmp_path: Path) -> None:
    """REQ-ENR-012: collect runs winner trigger >= min_amount, resolves domain via winner_name, audits site + tech."""
    json_docs = {
        f"{API_BASE}/tenders": {
            "data": [
                {"id": "t-big", "dateModified": "2026-11-24T10:00:00+02:00"},
                {"id": "t-small", "dateModified": "2026-11-24T10:05:00+02:00"},
            ]
        },
        f"{API_BASE}/tenders/t-big": {
            "data": {
                "id": "t-big",
                "tenderID": "UA-2026-11-24-100001-a",
                "title": "Капітальний ремонт серверної та мережі",
                "status": "complete",
                "dateModified": "2026-11-24T10:00:00+02:00",
                "value": {"amount": 1_500_000, "currency": "UAH"},
                "procuringEntity": {
                    "name": "Міністерство Цифрової Інфраструктури",
                    "identifier": {"id": "00011122"},
                    "address": {"region": "м. Київ"},
                },
                "items": [{"classification": {"id": "45310000-3"}}],
                "awards": [
                    {
                        "status": "active",
                        "date": "2026-11-24T09:50:00+02:00",
                        "value": {"amount": 1_420_000, "currency": "UAH"},
                        "suppliers": [
                            {
                                "name": "ТОВ МегаСервіс Тех",
                                "identifier": {"id": "33445566"},
                                "address": {"region": "м. Київ"},
                            }
                        ],
                    }
                ],
            }
        },
        f"{API_BASE}/tenders/t-small": {
            "data": {
                "id": "t-small",
                "tenderID": "UA-2026-11-24-100002-a",
                "title": "Постачання канцтоварів",
                "status": "complete",
                "dateModified": "2026-11-24T10:05:00+02:00",
                "value": {"amount": 85_000, "currency": "UAH"},
                "procuringEntity": {
                    "name": "Школа №10",
                    "identifier": {"id": "99887766"},
                    "address": {"region": "Львівська область"},
                },
                "items": [{"classification": {"id": "30190000-7"}}],
                "awards": [
                    {
                        "status": "active",
                        "value": {"amount": 82_000, "currency": "UAH"},
                        "suppliers": [{"name": "ФОП Дрібний", "identifier": {"id": "55556666"}}],
                    }
                ],
            }
        },
    }
    winner_html = """
    <html>
      <head>
        <title>ТОВ МегаСервіс Тех — інженерні рішення</title>
        <meta name="generator" content="WordPress 6.5" />
        <script src="https://cdn.example.com/wp-content/themes/mega/app.js"></script>
      </head>
      <body>
        <h1>ТОВ МегаСервіс Тех</h1>
        <p>Офіс у Києві: <a href="mailto:sales@megaservice.example">sales@megaservice.example</a></p>
        <p>Телефон: +380 (44) 123-45-67</p>
        <a href="https://t.me/megaservice_tech">Telegram</a>
      </body>
    </html>
    """
    fetcher = _WinnersFixtureFetcher(
        json_docs=json_docs,
        html_docs={"https://megaservice.example": winner_html},
    )
    search = _FakeSearch(
        {
            "МегаСервіс Тех": [
                SearchHit(title="ТОВ МегаСервіс Тех", url="https://megaservice.example"),
            ]
        }
    )

    def studio_factory(s):
        st = Studio(s, fetcher=fetcher)
        st.search = search
        st.runner.search = search
        return st

    settings = make_settings(tmp_path)
    app = create_app(settings, studio_factory=studio_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "prompt": "Знайди переможців тендерів Prozorro через API та проведи аудит їх сайтів",
                "seeds": [f"{API_BASE}/tenders"],
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]

        run_resp = client.post(f"/api/tasks/{task_id}/run", json={})
        assert run_resp.status_code == 200, run_resp.text
        res = _wait(client, run_resp.json()["run"]["id"])
        assert res["run"]["status"] == "succeeded"

        # Search queried winner_name, NOT procuringEntity ("Міністерство") and NOT small tender.
        assert any("МегаСервіс Тех" in q for q in search.queries)
        assert not any("Міністерство" in q for q in search.queries)
        assert not any("Дрібний" in q for q in search.queries)

        records = {
            row["record_key"]: row["payload"]
            for row in client.get(f"/api/tasks/{task_id}/records").json()["items"]
        }
        big = records["api:t-big"]
        assert big["winner_name"] == "ТОВ МегаСервіс Тех"
        assert big["winner_edrpou"] == "33445566"
        assert big["award_amount"] == 1_420_000.0
        assert big["website"] == "https://megaservice.example"
        assert big["email"] == "sales@megaservice.example"
        assert big["phone"]
        assert "WordPress" in (big.get("tech_stack") or [])

        small = records["api:t-small"]
        assert "website" not in small
        assert "email" not in small

        events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
        trig = [ev for ev in events if ev["type"] == "auto_enrichment_triggered"]
        assert len(trig) == 1
        assert trig[0]["data"]["matched"] == 1
        assert trig[0]["data"]["enriched"] == 1

        # Second run with same records must NOT re-query search for already enriched t-big.
        search.queries.clear()
        run_resp2 = client.post(f"/api/tasks/{task_id}/run", json={})
        assert run_resp2.status_code == 200
        res2 = _wait(client, run_resp2.json()["run"]["id"])
        assert res2["run"]["status"] == "succeeded"
        assert not search.queries


def test_winner_trigger_respects_threshold_idempotency_and_compliance(tmp_path: Path) -> None:
    """REQ-ENR-012: missing winner skipped, already enriched skipped, forbidden site blocked safely."""
    json_docs = {
        f"{API_BASE}/tenders": {
            "data": [
                {"id": "t-no-award", "dateModified": "2026-11-24T10:00:00+02:00"},
                {"id": "t-forbidden", "dateModified": "2026-11-24T10:05:00+02:00"},
            ]
        },
        f"{API_BASE}/tenders/t-no-award": {
            "data": {
                "id": "t-no-award",
                "tenderID": "UA-2026-11-24-200001-a",
                "title": "Оголошений тендер без переможця",
                "status": "active.tendering",
                "value": {"amount": 3_000_000, "currency": "UAH"},
                "procuringEntity": {"name": "КП Водоканал", "identifier": {"id": "10101010"}},
            }
        },
        f"{API_BASE}/tenders/t-forbidden": {
            "data": {
                "id": "t-forbidden",
                "tenderID": "UA-2026-11-24-200002-a",
                "title": "Великий будівельний підряд",
                "status": "complete",
                "value": {"amount": 2_500_000, "currency": "UAH"},
                "procuringEntity": {"name": "Служба доріг", "identifier": {"id": "20202020"}},
                "awards": [
                    {
                        "status": "active",
                        "value": {"amount": 2_400_000, "currency": "UAH"},
                        "suppliers": [
                            {
                                "name": "ТОВ Блокований Сайт",
                                "identifier": {"id": "44556677"},
                                "contactPoint": {"url": "https://blocked-winner.example"},
                            }
                        ],
                    }
                ],
            }
        },
    }
    fetcher = _WinnersFixtureFetcher(json_docs=json_docs, html_docs={})
    search = _FakeSearch({})

    def studio_factory(s):
        st = Studio(s, fetcher=fetcher)
        st.search = search
        st.runner.search = search
        st.store.upsert_source_policy(SourcePolicy(domain="blocked-winner.example", tos_risk="forbidden"))
        return st

    settings = make_settings(tmp_path)
    app = create_app(settings, studio_factory=studio_factory)
    with TestClient(app) as client:
        created = client.post(
            "/api/tasks",
            json={
                "prompt": "Знайди переможців тендерів Prozorro через API",
                "seeds": [f"{API_BASE}/tenders"],
            },
        )
        assert created.status_code == 201
        task_id = created.json()["task"]["id"]

        run1 = client.post(f"/api/tasks/{task_id}/run", json={})
        res1 = _wait(client, run1.json()["run"]["id"])
        assert res1["run"]["status"] == "succeeded"

        # Tender without winner must NEVER trigger search using procuringEntity name.
        assert not search.queries

        # Forbidden winner domain was blocked by PolicyGuardFetcher without failing collect.
        events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
        assert any(ev["type"] == "compliance_blocked" for ev in events)
        records = client.get(f"/api/tasks/{task_id}/records").json()["items"]
        assert len(records) == 2


@pytest.mark.asyncio
async def test_winner_delivery_formats_contacts_and_protects_personal_pii(tmp_path: Path) -> None:
    """REQ-DEL-007: delivery filters on award_amount, prints B2B winner contacts, redacts personal PII."""
    sent_messages: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sendMessage"):
            sent_messages.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        return httpx.Response(404)

    settings = make_settings(
        tmp_path,
        telegram_bot_token="123456:TEST_TOKEN",
        allow_private_network=True,
    )
    studio = Studio(settings)
    try:
        task = await studio.create_task(
            "Переможці тендерів Prozorro через API",
            [f"{API_BASE}/tenders"],
        )
        spec_with_personal = task.spec.model_copy(deep=True)
        spec_with_personal.columns.append(
            FieldSpec(key="director_passport", title="Паспорт", pii="personal", method="manual")
        )
        task = studio.update_task(task.id, spec=spec_with_personal)
        assert task is not None

        studio.create_subscription(
            task.id,
            channel="telegram",
            target="998877",
            filters=DeliveryFilters(min_amount=1_000_000.0),
        )

        dispatcher = DeltaDispatcher(settings, studio.store)
        await dispatcher._client.aclose()
        dispatcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            changes = [
                ChangeEvent(
                    type="new_entity",
                    record_key="api:t-big",
                    source_url=f"{API_BASE}/tenders/t-big",
                )
            ]
            records_by_key = {
                "api:t-big": {
                    "title": "Капітальний ремонт мережі",
                    "winner_name": "ТОВ МегаСервіс Тех",
                    "winner_edrpou": "33445566",
                    "award_amount": 1_420_000.0,
                    "currency": "UAH",
                    "region": "м. Київ",
                    "cpv": "45310000-3",
                    "website": "https://megaservice.example",
                    "email": "sales@megaservice.example",
                    "phone": "+380441234567",
                    "tech_stack": ["WordPress", "Cloudflare"],
                    "director_passport": "АА123456",
                }
            }
            delivered = await dispatcher.dispatch_changes(
                task,
                version=1,
                run_id="run_test",
                changes=changes,
                records_by_key=records_by_key,
            )
            assert delivered["sent"] == 1
            assert len(sent_messages) == 1
            text = sent_messages[0]["text"]
            assert "ТОВ МегаСервіс Тех" in text
            assert "33445566" in text
            assert "1 420 000 UAH" in text
            assert "https://megaservice.example" in text
            assert "sales@megaservice.example" in text
            assert "+380441234567" in text
            assert "WordPress" in text
            assert "АА123456" not in text
        finally:
            await dispatcher.aclose()
    finally:
        await studio.aclose()
