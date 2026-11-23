"""Offline tests for D-01: Webhook & Telegram delta delivery, filtering, PII masking and dedup."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import make_settings, read_fixture
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.db.sqlite_store import OperationalStore
from app.main import create_app
from app.models import (
    ApiSourceSpec,
    ChangeEvent,
    DeliveryFilters,
    FieldSpec,
    PageContent,
    TaskSpec,
)
from app.pipeline.delivery import DeltaDispatcher
from app.runtime import Studio
from app.scraping.fetcher import FetchResult
from app.utils import hash_text, iso

API_BASE = "http://api.openprocurement.local/api/2.5"


def _make_sample_task(store: OperationalStore, *, with_personal_pii: bool = False) -> str:
    cols = [
        FieldSpec(key="tender_id", title="ID тендера", method="api_json"),
        FieldSpec(key="title", title="Предмет", method="api_json"),
        FieldSpec(key="amount", title="Сума", dtype="number", method="api_json"),
    ]
    if with_personal_pii:
        cols.append(
            FieldSpec(key="contact_person", title="Контактна особа", pii="personal", method="api_json")
        )
    spec = TaskSpec(
        title="Офісні меблі Prozorro",
        prompt="Моніторинг тендерів Prozorro",
        entity="tender",
        columns=cols,
        seeds=[f"{API_BASE}/tenders?limit=10"],
        extraction="api_json",
    )
    return store.create_task(spec).id


def test_subscription_crud_ssrf_and_secret_masking(tmp_path: Path) -> None:
    """REQ-DEL-001: subscriptions CRUD validates SSRF/logged-out URLs, hides secret and emits events."""
    settings = make_settings(tmp_path, scheduler_enabled=False, allow_private_network=False)
    app = create_app(settings)
    with TestClient(app) as client:
        created_task = client.post(
            "/api/tasks",
            json={
                "prompt": "Закупівлі Prozorro API для алертів",
                "seeds": [f"{API_BASE}/tenders?limit=10"],
            },
        )
        assert created_task.status_code == 201
        task_id = created_task.json()["task"]["id"]

        # SSRF private/loopback webhook must be rejected (400)
        ssrf_resp = client.post(
            f"/api/tasks/{task_id}/subscriptions",
            json={
                "name": "Local hook",
                "channel": "webhook",
                "target": "http://127.0.0.1:9000/hook",
            },
        )
        assert ssrf_resp.status_code == 400

        # Logged-out rule: webhook URL with credential query token must be rejected
        cred_resp = client.post(
            f"/api/tasks/{task_id}/subscriptions",
            json={
                "name": "Token hook",
                "channel": "webhook",
                "target": "https://hooks.example/hook?api_key=secret123",
            },
        )
        assert cred_resp.status_code in {400, 403}

        # Valid webhook subscription: secret must NOT leak in JSON output
        wh_resp = client.post(
            f"/api/tasks/{task_id}/subscriptions",
            json={
                "name": "CRM Webhook",
                "channel": "webhook",
                "target": "https://93.184.216.34/webhook",
                "secret": "super-secret-signing-key",
                "filters": {
                    "event_types": ["new_entity", "price_drop"],
                    "keywords": ["меблі"],
                    "min_amount": 50000,
                },
            },
        )
        assert wh_resp.status_code == 201, wh_resp.text
        wh_sub = wh_resp.json()["subscription"]
        assert wh_sub["has_secret"] is True
        assert "secret" not in wh_sub
        assert "super-secret-signing-key" not in wh_resp.text

        # Valid Telegram subscription
        tg_resp = client.post(
            f"/api/tasks/{task_id}/subscriptions",
            json={
                "name": "TG Alert",
                "channel": "telegram",
                "target": "-1001234567890",
                "filters": {"notify_on_error": True},
            },
        )
        assert tg_resp.status_code == 201, tg_resp.text
        tg_sub_id = tg_resp.json()["subscription"]["id"]

        listed = client.get(f"/api/tasks/{task_id}/subscriptions")
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 2

        updated = client.patch(
            f"/api/tasks/{task_id}/subscriptions/{tg_sub_id}",
            json={"enabled": False, "name": "TG Paused"},
        )
        assert updated.status_code == 200
        assert updated.json()["subscription"]["enabled"] is False
        assert updated.json()["subscription"]["name"] == "TG Paused"

        deleted = client.delete(f"/api/tasks/{task_id}/subscriptions/{tg_sub_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}

        events = [ev["type"] for ev in client.get(f"/api/tasks/{task_id}/events").json()["items"]]
        assert {"subscription_created", "subscription_updated", "subscription_deleted"} <= set(events)


@pytest.mark.asyncio
async def test_delta_filtering_pii_redaction_and_hmac(tmp_path: Path) -> None:
    """REQ-DEL-002: filters match keywords/amount/event_types, masks personal PII, signs webhook."""
    sent_requests: list[tuple[str, dict[str, str], bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(
            (str(request.url), {k.lower(): v for k, v in request.headers.items()}, request.content)
        )
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        tmp_path,
        allow_private_network=True,
        telegram_bot_token="123456:FAKE_TELEGRAM_TOKEN",
        delivery_max_retries=1,
    )
    store = OperationalStore(settings.paths)
    try:
        task_id = _make_sample_task(store, with_personal_pii=True)
        task = store.get_task(task_id)
        assert task is not None

        store.create_subscription(
            task_id,
            name="Furniture High Value",
            channel="webhook",
            target="https://93.184.216.34/alerts",
            secret="hmac-key-xyz",
            filters=DeliveryFilters(
                event_types=["new_entity", "price_drop"],
                keywords=["меблі"],
                min_amount=100_000,
            ),
        )
        store.create_subscription(
            task_id,
            name="Price Drops Only",
            channel="telegram",
            target="-100999888777",
            filters=DeliveryFilters(event_types=["price_drop"]),
        )

        dispatcher = DeltaDispatcher(
            settings,
            store,
            transport=httpx.MockTransport(handler),
            retry_delay_s=0.0,
        )

        records_by_key = {
            "api:lot-match": {
                "tender_id": "UA-2026-001",
                "title": "Офісні меблі та крісла <Тендер>",
                "amount": 150_000.0,
                "contact_person": "Іван Петренко",
            },
            "api:lot-other": {
                "tender_id": "UA-2026-002",
                "title": "Папір офісний",
                "amount": 50_000.0,
                "contact_person": "Ольга Коваль",
            },
        }
        changes = [
            ChangeEvent(
                type="new_entity",
                record_key="api:lot-match",
                source_url=f"{API_BASE}/tenders/lot-match",
            ),
            ChangeEvent(
                type="price_drop",
                record_key="api:lot-match",
                field="amount",
                old=200_000.0,
                new=150_000.0,
                delta_pct=-25.0,
                source_url=f"{API_BASE}/tenders/lot-match",
            ),
            ChangeEvent(
                type="field_changed",
                record_key="api:lot-match",
                field="contact_person",
                old="Сергій Бондар",
                new="Іван Петренко",
                source_url=f"{API_BASE}/tenders/lot-match",
            ),
            ChangeEvent(
                type="new_entity",
                record_key="api:lot-other",
                source_url=f"{API_BASE}/tenders/lot-other",
            ),
        ]

        summary = await dispatcher.dispatch_changes(
            task, version=2, run_id="run-002", changes=changes, records_by_key=records_by_key
        )
        assert summary["sent"] == 2
        assert len(sent_requests) == 2

        wh_req = next(r for r in sent_requests if "93.184.216.34" in r[0])
        wh_headers, wh_raw = wh_req[1], wh_req[2]
        expected_sig = "sha256=" + hmac.new(b"hmac-key-xyz", wh_raw, hashlib.sha256).hexdigest()
        assert wh_headers.get("x-daas-signature") == expected_sig
        wh_body = json.loads(wh_raw.decode("utf-8"))
        assert len(wh_body["changes"]) == 2
        assert all(ch["record_key"] == "api:lot-match" for ch in wh_body["changes"])
        assert "Іван Петренко" not in wh_raw.decode("utf-8")
        assert "Ольга Коваль" not in wh_raw.decode("utf-8")

        tg_req = next(r for r in sent_requests if "api.telegram.org" in r[0])
        assert "/bot123456:FAKE_TELEGRAM_TOKEN/sendMessage" in tg_req[0]
        tg_body = json.loads(tg_req[2].decode("utf-8"))
        assert tg_body["chat_id"] == "-100999888777"
        assert "Ціна впала" in tg_body["text"] or "price_drop" in tg_body["text"]
        assert "&lt;Тендер&gt;" in tg_body["text"]
        assert "Іван Петренко" not in tg_body["text"]

        # Non-matching events do not issue HTTP requests
        sent_requests.clear()
        await dispatcher.dispatch_changes(
            task,
            version=3,
            run_id="run-003",
            changes=[ChangeEvent(type="new_entity", record_key="api:lot-other")],
            records_by_key=records_by_key,
        )
        assert sent_requests == []
        await dispatcher.aclose()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_delivery_retries_dedup_and_owner_error_alerts(tmp_path: Path) -> None:
    """REQ-DEL-003: retries transient 503, deduplicates across instances, sanitizes failed bot URL, alerts on 0/fail."""
    attempts_by_url: dict[str, int] = {}

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        attempts_by_url[url] = attempts_by_url.get(url, 0) + 1
        if "93.184.216.34/flaky" in url and attempts_by_url[url] < 2:
            return httpx.Response(503, text="upstream busy")
        if "api.telegram.org" in url and "fail-bot" in url:
            return httpx.Response(500, text="telegram internal error")
        return httpx.Response(200, json={"ok": True})

    secret_token = "999888:SECRET_DO_NOT_LEAK"
    settings = make_settings(
        tmp_path,
        allow_private_network=True,
        telegram_bot_token=secret_token,
        delivery_max_retries=3,
    )
    store = OperationalStore(settings.paths)
    try:
        task_id = _make_sample_task(store)
        task = store.get_task(task_id)
        assert task is not None

        store.create_subscription(
            task_id,
            name="Flaky Webhook",
            channel="webhook",
            target="https://93.184.216.34/flaky",
            filters=DeliveryFilters(notify_on_error=True),
        )

        transport = httpx.MockTransport(flaky_handler)
        dispatcher1 = DeltaDispatcher(settings, store, transport=transport, retry_delay_s=0.0)
        changes = [ChangeEvent(type="new_entity", record_key="api:t-1")]
        records = {"api:t-1": {"title": "Меблі", "amount": 200_000}}

        res1 = await dispatcher1.dispatch_changes(
            task, version=1, run_id="run-1", changes=changes, records_by_key=records
        )
        assert res1["sent"] == 1
        assert attempts_by_url["https://93.184.216.34/flaky"] == 2

        # Restart simulation: a new DeltaDispatcher instance must NOT resend already logged dedup keys
        dispatcher2 = DeltaDispatcher(settings, store, transport=transport, retry_delay_s=0.0)
        res2 = await dispatcher2.dispatch_changes(
            task, version=1, run_id="run-1", changes=changes, records_by_key=records
        )
        assert res2["sent"] == 0
        assert res2["skipped_dedup"] >= 1
        assert attempts_by_url["https://93.184.216.34/flaky"] == 2

        # Owner operational alert on empty / failed run
        alert_res = await dispatcher2.dispatch_alert(
            task, run_id="run-fail", kind="run_failed", message="API 502 error"
        )
        assert alert_res["sent"] == 1
        # Repeat same alert for same run_id -> deduplicated
        alert_dup = await dispatcher2.dispatch_alert(
            task, run_id="run-fail", kind="run_failed", message="API 502 error"
        )
        assert alert_dup["sent"] == 0

        # Failing Telegram alert must log warning without leaking token in event message/data
        store.create_subscription(
            task_id,
            name="Broken TG",
            channel="telegram",
            target="fail-bot",
            filters=DeliveryFilters(notify_on_error=True),
        )
        bad_settings = make_settings(
            tmp_path,
            allow_private_network=True,
            telegram_bot_token=None,
            delivery_max_retries=2,
        )
        dispatcher3 = DeltaDispatcher(bad_settings, store, transport=transport, retry_delay_s=0.0)
        await dispatcher3.dispatch_alert(
            task, run_id="run-fail-2", kind="empty_dataset", message="0 записів"
        )
        events = store.list_events(task_id, limit=100)
        assert any(ev.type == "delivery_sent" for ev in events)
        failed_events = [ev for ev in events if ev.type == "delivery_failed"]
        assert failed_events
        for ev in failed_events:
            assert ev.level == "warning"
            assert secret_token not in ev.message
            assert secret_token not in json.dumps(ev.data)

        await dispatcher1.aclose()
        await dispatcher2.aclose()
        await dispatcher3.aclose()
    finally:
        store.close()

    # Verify PipelineRunner dispatches changes during real offline collect and tolerates webhook failure
    class _ApiFetcher:
        name = "fixture"

        async def fetch(self, url: str, *, force_refresh: bool = False, allow_json: bool = False) -> FetchResult:
            if url.startswith(f"{API_BASE}/tenders?"):
                raw = read_fixture("prozorro_list_page1.json")
            elif url == f"{API_BASE}/tenders/tender-001":
                raw = read_fixture("prozorro_tender_001.json")
            elif url == f"{API_BASE}/tenders/tender-002":
                raw = read_fixture("prozorro_tender_002.json")
            else:
                return FetchResult(kind="permanent", status=404, error="not found")
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

        async def aclose(self) -> None:
            return None

    e2e_settings = make_settings(
        tmp_path / "e2e", allow_private_network=True, scheduler_enabled=False, delivery_max_retries=1
    )
    studio = Studio(e2e_settings, fetcher=_ApiFetcher())
    e2e_calls: list[dict] = []
    studio.dispatcher = DeltaDispatcher(
        e2e_settings,
        studio.store,
        transport=httpx.MockTransport(
            lambda req: (
                e2e_calls.append(json.loads(req.content.decode("utf-8"))),
                httpx.Response(200, json={"ok": True}),
            )[1]
        ),
        retry_delay_s=0.0,
    )
    studio.runner.dispatcher = studio.dispatcher
    try:
        api_task = await studio.create_task(
            "Prozorro API + Delivery",
            [f"{API_BASE}/tenders?limit=10"],
            api_source=ApiSourceSpec(
                items_path="data",
                id_path="id",
                detail_url_template=f"{API_BASE}/tenders/{{id}}",
                incremental=True,
                field_map={
                    "tender_id": "tenderID",
                    "title": "title",
                    "name": "procuringEntity.name",
                    "amount": "value.amount",
                    "currency": "value.currency",
                    "status": "status",
                },
            ),
        )
        studio.create_subscription(
            api_task.id,
            name="E2E Hook",
            channel="webhook",
            target="https://93.184.216.34/e2e",
        )
        run_info = await studio.collect(api_task.id)
        finished = await studio.wait(run_info.id, timeout=10.0)
        assert finished is not None and finished.status == "succeeded"
        assert len(e2e_calls) == 1
        assert e2e_calls[0]["event"] == "delta"
        assert len(e2e_calls[0]["changes"]) >= 2
    finally:
        await studio.aclose()


def test_sqlite_migration_v6_opens_v5_database(tmp_path: Path) -> None:
    """REQ-OPS-010: migration v6 adds subscriptions and delivery_log cleanly on user_version=5 DB."""
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
        CREATE TABLE schedules (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            cron TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            catch_up INTEGER NOT NULL DEFAULT 1,
            acknowledge_high_risk INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            next_run_at TEXT,
            last_run_at TEXT,
            last_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO tasks (id, title, prompt, spec_json, status, created_at, updated_at)
        VALUES ('t_v5', 'V5 Task', 'Old prompt', '{{}}', 'draft', '{iso()}', '{iso()}');
        PRAGMA user_version = 5;
        """
    )
    conn.close()

    store = OperationalStore(settings.paths)
    try:
        sub = store.create_subscription(
            "t_v5", name="Sub V6", channel="webhook", target="https://93.184.216.34/v6"
        )
        assert sub.task_id == "t_v5"
        assert store.is_delivered(sub.id, "k1") is False
        store.record_delivery(sub.id, "k1", run_id="r1", status="sent", attempts=1)
        assert store.is_delivered(sub.id, "k1") is True
    finally:
        store.close()


def test_cli_notify_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-DEL-001, REQ-OPS-030: `daas notify add | list | test | delete` works via Typer CLI."""
    settings = make_settings(tmp_path, scheduler_enabled=False, allow_private_network=True)
    hook_calls: list[str] = []

    def _cli_studio(_settings=None):
        st = Studio(settings)
        st.dispatcher = DeltaDispatcher(
            settings,
            st.store,
            transport=httpx.MockTransport(
                lambda req: (hook_calls.append(str(req.url)), httpx.Response(200, json={"ok": True}))[1]
            ),
            retry_delay_s=0.0,
        )
        st.runner.dispatcher = st.dispatcher
        return st

    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli._studio", _cli_studio)

    setup = _cli_studio()
    task = asyncio.run(
        setup.create_task("CLI notify task", [f"{API_BASE}/tenders?limit=10"])
    )
    asyncio.run(setup.aclose())

    runner = CliRunner()
    res_add = runner.invoke(
        cli_app,
        [
            "notify",
            "add",
            task.id,
            "--channel",
            "webhook",
            "--target",
            "https://93.184.216.34/cli-hook",
            "--name",
            "CLI Hook",
            "--keyword",
            "тендери",
            "--min-amount",
            "50000",
        ],
    )
    assert res_add.exit_code == 0, res_add.output
    sub_id = res_add.output.strip().splitlines()[0].split()[1]

    res_list = runner.invoke(cli_app, ["notify", "list", task.id])
    assert res_list.exit_code == 0, res_list.output
    assert sub_id in res_list.output

    res_test = runner.invoke(cli_app, ["notify", "test", task.id, sub_id])
    assert res_test.exit_code == 0, res_test.output
    assert hook_calls == ["https://93.184.216.34/cli-hook"]

    res_del = runner.invoke(cli_app, ["notify", "delete", task.id, sub_id])
    assert res_del.exit_code == 0, res_del.output
