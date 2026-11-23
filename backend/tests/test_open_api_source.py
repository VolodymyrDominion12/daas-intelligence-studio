"""Offline tests for D-02: Open JSON API source connector (Prozorro / OpenProcurement)."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest
from conftest import FixtureFetcher, make_settings, read_fixture
from fastapi.testclient import TestClient

from app.compliance import ComplianceError, PolicyGuardFetcher
from app.db.sqlite_store import OperationalStore
from app.extraction.api_json import extract_json_list_targets, extract_json_records
from app.llm.router import LLMRouter
from app.main import create_app
from app.models import ApiSourceSpec, FieldSpec, SourcePolicy, TaskSpec
from app.pipeline.planner import Planner
from app.runtime import Studio
from app.scraping.fetcher import FetchResult, HttpFetcher
from app.utils import iso

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


def test_api_source_spec_validation_and_logged_out() -> None:
    """REQ-DM-009: valid ApiSourceSpec preserves seed query; invalid/credentials rejected."""
    columns = [
        FieldSpec(key="tender_id", title="ID тендера", method="api_json"),
        FieldSpec(key="title", title="Предмет", method="api_json"),
        FieldSpec(key="amount", title="Сума", dtype="number", method="api_json"),
    ]
    api_source = ApiSourceSpec(
        items_path="data",
        id_path="id",
        field_map={"tender_id": "tenderID", "title": "title", "amount": "value.amount"},
        detail_url_template=f"{API_BASE}/tenders/{{id}}",
    )
    spec = TaskSpec(
        title="Prozorro",
        prompt="Prozorro API тендери",
        entity="tender",
        columns=columns,
        seeds=[f"{API_BASE}/tenders?descending=1&limit=10"],
        extraction="api_json",
        api_source=api_source,
    )
    assert spec.seeds == [f"{API_BASE}/tenders?descending=1&limit=10"]

    with pytest.raises(ValueError):
        ApiSourceSpec(field_map={})

    with pytest.raises(ComplianceError):
        ApiSourceSpec(
            field_map={"title": "title"},
            detail_url_template="https://api.example.com/tenders/{id}?api_key=secret123",
        )


@pytest.mark.asyncio
async def test_openprocurement_preset_builds_non_pii_api_spec(tmp_path: Path) -> None:
    """REQ-PLAN-012: OpenProcurement preset works offline with 0 LLM and 0 personal PII."""
    settings = make_settings(tmp_path)
    store = OperationalStore(settings.paths)
    router = LLMRouter(settings, store)
    planner = Planner(settings, router, enrichers=[], store=store)
    try:
        spec = await planner.plan(
            "Збери нові закупівлі та аукціони Prozorro.Продажі через відкритий API",
            ["https://public.api.openprocurement.org/api/2.5/tenders?limit=50"],
        )
        assert spec.extraction == "api_json"
        assert spec.api_source is not None
        assert spec.api_source.detail_url_template
        assert not spec.pii_columns
        keys = {col.key for col in spec.columns}
        assert {"tender_id", "title", "name", "amount", "currency", "status"} <= keys
        assert spec.seeds == ["https://public.api.openprocurement.org/api/2.5/tenders?limit=50"]
    finally:
        await router.aclose()
        store.close()


@pytest.mark.asyncio
async def test_fetcher_json_mode_and_compliance_guard(tmp_path: Path) -> None:
    """REQ-FETCH-006, REQ-FETCH-011: allow_json accepts JSON, rejects by default; guard applies."""
    body = read_fixture("prozorro_list_page1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect-private":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text=body, headers={"Content-Type": "application/json; charset=utf-8"})

    settings = make_settings(tmp_path, allow_private_network=False)
    store = OperationalStore(settings.paths)
    store.upsert_source_policy(SourcePolicy(domain="blocked.example", tos_risk="forbidden"))

    fetcher = HttpFetcher(settings)
    await fetcher._client.aclose()
    fetcher._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    guard = PolicyGuardFetcher(fetcher, store)
    try:
        # Without allow_json, application/json is rejected as permanent.
        rejected = await guard.fetch("https://api.example/tenders", allow_json=False)
        assert rejected.kind == "permanent"

        # With allow_json=True on an allowed domain, JSON succeeds (bypassing DNS via MockTransport).
        fetcher.settings = make_settings(tmp_path, allow_private_network=True)
        ok = await guard.fetch("https://api.example/tenders", allow_json=True)
        assert ok.ok and ok.page is not None
        assert json.loads(ok.page.html)["data"][0]["id"] == "tender-001"

        # Forbidden source is still blocked in API mode.
        blocked = await guard.fetch("https://blocked.example/tenders", allow_json=True)
        assert blocked.kind == "policy"

        # Private redirect is still blocked when allow_private_network=False.
        fetcher.settings = make_settings(tmp_path, allow_private_network=False)
        # Stub public check on initial domain by using IP literal check? MockTransport doesn't need DNS for IP.
        # Use direct redirect check via HttpFetcher._attempt with allow_private_network=False on public IP:
        redir = await guard.fetch("http://93.184.216.34/redirect-private", allow_json=True)
        assert redir.kind == "unsafe"
    finally:
        await guard.aclose()
        store.close()


def test_json_extractor_maps_fields_and_handles_broken_json() -> None:
    """REQ-EXT-010: deterministic extraction with api:<id> key, api_json provenance, safe failure."""
    spec = TaskSpec(
        title="Prozorro",
        prompt="Prozorro API",
        entity="tender",
        columns=[
            FieldSpec(key="tender_id", title="ID", method="api_json"),
            FieldSpec(key="title", title="Назва", method="api_json"),
            FieldSpec(key="name", title="Замовник", method="api_json"),
            FieldSpec(key="amount", title="Сума", dtype="number", method="api_json"),
            FieldSpec(key="cpv", title="CPV", method="api_json"),
        ],
        seeds=[f"{API_BASE}/tenders"],
        extraction="api_json",
        api_source=ApiSourceSpec(
            items_path="data",
            id_path="id",
            field_map={
                "tender_id": "tenderID",
                "title": "title",
                "name": "procuringEntity.name",
                "amount": "value.amount",
                "cpv": "items.0.classification.id",
            },
        ),
    )

    list_raw = read_fixture("prozorro_list_page1.json")
    targets, next_url, cursor, warnings = extract_json_list_targets(
        list_raw,
        spec.api_source,
        detail_url_template=f"{API_BASE}/tenders/{{id}}",
    )
    assert not warnings
    assert len(targets) == 2
    assert targets[0][1] == f"{API_BASE}/tenders/tender-001"
    assert cursor == "2026-11-23T09:45:00+02:00"
    assert next_url == f"{API_BASE}/tenders?offset=2026-11-23T09:45:00"

    detail_raw = read_fixture("prozorro_tender_001.json")
    records, rec_warnings = extract_json_records(detail_raw, spec, f"{API_BASE}/tenders/tender-001")
    assert not rec_warnings
    assert len(records) == 1
    rec = records[0]
    assert rec.record_key == "api:tender-001"
    assert rec.payload["amount"] == 1250000.0
    assert rec.payload["cpv"] == "72000000-5"
    assert rec.provenance["title"].method == "api_json"
    assert rec.provenance["title"].verified is True

    broken_raw = read_fixture("broken_api.json")
    bad_records, bad_warnings = extract_json_records(broken_raw, spec, f"{API_BASE}/broken")
    assert bad_records == []
    assert bad_warnings


def test_api_collection_pagination_cursor_and_failure_safety(tmp_path: Path) -> None:
    """REQ-RUN-029, REQ-RUN-030, REQ-CAL-008: collect without calibration, incremental cursor, safe error."""
    state = {"phase": 1, "fail_page2": False}

    class ApiFixtureFetcher(FixtureFetcher):
        async def fetch(
            self, url: str, *, force_refresh: bool = False, allow_json: bool = False
        ) -> FetchResult:
            self.calls.append(url)
            if url == f"{API_BASE}/tenders?offset=2026-11-23T09:45:00":
                if state["fail_page2"]:
                    return FetchResult(kind="transient", status=500, error="HTTP 500")
                if state["phase"] == 1:
                    fixture = "prozorro_list_page2.json"
                else:
                    # Phase 2: 1 updated record with price_drop; tender-002 omitted (must NOT tombstone!).
                    payload = {
                        "data": [{"id": "tender-001", "dateModified": "2026-11-23T11:00:00+02:00"}],
                        "next_page": {
                            "offset": "2026-11-23T11:00:00+02:00",
                            "uri": f"{API_BASE}/tenders?offset=2026-11-23T11:00:00",
                        },
                    }
                    from app.models import PageContent
                    from app.utils import hash_text

                    raw = json.dumps(payload, ensure_ascii=False)
                    return FetchResult(
                        page=PageContent(
                            url=url, final_url=url, status_code=200, html=raw, text=raw,
                            content_hash=hash_text(raw), engine="fixture",
                        ),
                        status=200,
                    )
            elif url == f"{API_BASE}/tenders?offset=2026-11-23T11:00:00":
                fixture = "prozorro_list_page2.json"
            elif url == f"{API_BASE}/tenders":
                fixture = "prozorro_list_page1.json"
            elif url == f"{API_BASE}/tenders/tender-001":
                if state["phase"] == 2:
                    data = json.loads(read_fixture("prozorro_tender_001.json"))
                    data["data"]["value"]["amount"] = 900000.0
                    raw = json.dumps(data, ensure_ascii=False)
                    from app.models import PageContent
                    from app.utils import hash_text

                    return FetchResult(
                        page=PageContent(
                            url=url, final_url=url, status_code=200, html=raw, text=raw,
                            content_hash=hash_text(raw), engine="fixture",
                        ),
                        status=200,
                    )
                fixture = "prozorro_tender_001.json"
            elif url == f"{API_BASE}/tenders/tender-002":
                fixture = "prozorro_tender_002.json"
            else:
                return FetchResult(kind="permanent", status=404, error=f"no fixture for {url}")

            from app.models import PageContent
            from app.utils import hash_text

            raw = read_fixture(fixture)
            return FetchResult(
                page=PageContent(
                    url=url, final_url=url, status_code=200, html=raw, text=raw,
                    content_hash=hash_text(raw), engine="fixture",
                ),
                status=200,
            )

    fetcher = ApiFixtureFetcher()
    settings = make_settings(tmp_path)
    app = create_app(settings, studio_factory=lambda s: Studio(s, fetcher=fetcher))
    with TestClient(app) as client:
        api_source = {
            "items_path": "data",
            "id_path": "id",
            "next_page_path": "next_page.uri",
            "cursor_path": "next_page.offset",
            "detail_url_template": f"{API_BASE}/tenders/{{id}}",
            "incremental": True,
            "field_map": {
                "tender_id": "tenderID",
                "title": "title",
                "name": "procuringEntity.name",
                "amount": "value.amount",
                "currency": "value.currency",
                "status": "status",
            },
        }
        created = client.post(
            "/api/tasks",
            json={
                "prompt": "Тендери Prozorro API",
                "seeds": [f"{API_BASE}/tenders"],
                "api_source": api_source,
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["task"]["id"]

        # Failure safety: 500 on page 2 must NOT advance saved incremental cursor.
        state["fail_page2"] = True
        run_fail = client.post(f"/api/tasks/{task_id}/run", json={})
        assert run_fail.status_code == 200, run_fail.text
        fail_res = _wait(client, run_fail.json()["run"]["id"])
        assert fail_res["run"]["status"] == "partial"

        # Now run cleanly (cursor was NOT saved after failure, so page 1 runs again).
        state["fail_page2"] = False
        run1 = client.post(f"/api/tasks/{task_id}/run", json={})
        assert run1.status_code == 200, run1.text
        res1 = _wait(client, run1.json()["run"]["id"])
        assert res1["run"]["status"] == "succeeded"

        records1 = client.get(f"/api/tasks/{task_id}/records").json()
        assert records1["total"] == 2
        keys = {row["record_key"] for row in records1["items"]}
        assert keys == {"api:tender-001", "api:tender-002"}

        # Phase 2: incremental run starts from saved cursor, updates tender-001 with price_drop,
        # and does NOT tombstone tender-002.
        state["phase"] = 2
        fetcher.calls.clear()
        run2 = client.post(f"/api/tasks/{task_id}/run", json={})
        assert run2.status_code == 200, run2.text
        res2 = _wait(client, run2.json()["run"]["id"])
        assert res2["run"]["status"] == "succeeded"
        assert fetcher.calls[0] == f"{API_BASE}/tenders?offset=2026-11-23T09:45:00"

        records2 = client.get(f"/api/tasks/{task_id}/records").json()
        assert records2["total"] == 2
        changes = client.get(f"/api/tasks/{task_id}/changes").json()["items"]
        assert any(
            item["type"] == "price_drop" and item["data"]["record_key"] == "api:tender-001"
            for item in changes
        )
        assert not any(item["type"] == "removed_entity" for item in changes)
        events = client.get(f"/api/tasks/{task_id}/events").json()["items"]
        assert any(event["type"] == "api_cursor_saved" for event in events)


def test_sqlite_migration_v4_opens_v3_database(tmp_path: Path) -> None:
    """Migration v4 adds api_state cleanly on an existing user_version=3 database."""
    settings = make_settings(tmp_path)
    settings.paths.ensure()
    conn = sqlite3.connect(settings.paths.sqlite)
    conn.executescript(
        f"""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks (id, title, prompt, spec_json, status, created_at, updated_at)
        VALUES ('old1', 'Old', 'Old prompt', '{{}}', 'draft', '{iso()}', '{iso()}');
        PRAGMA user_version = 3;
        """
    )
    conn.close()

    store = OperationalStore(settings.paths)
    try:
        assert store.get_api_state("old1") is None
        store.save_api_state("old1", next_cursor="c1", next_url="https://api.example/next")
        saved = store.get_api_state("old1")
        assert saved is not None
        assert saved["next_cursor"] == "c1"
        assert saved["next_url"] == "https://api.example/next"
    finally:
        store.close()
