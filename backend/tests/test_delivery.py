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


def test_sqlite_migration_v9_opens_v8_database(tmp_path: Path) -> None:
    """REQ-OPS-010: migration v9 adds digest columns and delivery_digest_queue on user_version=8 DB."""
    settings = make_settings(tmp_path)
    settings.paths.ensure()
    conn = sqlite3.connect(settings.paths.sqlite)
    legacy_filters = json.dumps(
        {"event_types": ["new_entity"], "keywords": ["меблі"], "min_amount": 10000}
    )
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
        CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL,
            target TEXT NOT NULL,
            secret TEXT NOT NULL DEFAULT '',
            filters_json TEXT NOT NULL DEFAULT '{{}}',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE delivery_log (
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            dedup_key TEXT NOT NULL,
            run_id TEXT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            PRIMARY KEY (subscription_id, dedup_key)
        );
        INSERT INTO tasks (id, title, prompt, spec_json, status, created_at, updated_at)
        VALUES ('t_v8', 'V8 Task', 'Old prompt', '{{}}', 'draft', '{iso()}', '{iso()}');
        INSERT INTO subscriptions (id, task_id, name, channel, target, secret, filters_json, enabled, created_at, updated_at)
        VALUES ('sub_v8', 't_v8', 'Legacy Sub', 'telegram', '-100111', '', '{legacy_filters}', 1, '{iso()}', '{iso()}');
        PRAGMA user_version = 8;
        """
    )
    conn.close()

    store = OperationalStore(settings.paths)
    try:
        sub = store.get_subscription("sub_v8")
        assert sub is not None
        assert sub.filters.keywords == ["меблі"]
        assert sub.filters.delivery_mode == "instant"
        assert sub.filters.digest_cron == "0 6 * * *"
        assert sub.filters.exclude_keywords == []
        assert sub.filters.cpv_prefixes == []
        assert sub.filters.regions == []
        assert sub.filters.field_filters == {}
        assert sub.next_digest_at is None
        assert sub.last_digest_at is None
        assert sub.pending_digest_count == 0

        # Queue table accepts items referencing legacy subscription
        queued = store.enqueue_digest_items(
            "sub_v8",
            "t_v8",
            2,
            "run-v8",
            items=[("delta:v2:abc", {"type": "new_entity", "record_key": "k1"}, {"title": "Стіл"})],
        )
        assert queued == 1
        sub_after = store.get_subscription("sub_v8")
        assert sub_after is not None
        assert sub_after.pending_digest_count == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_advanced_delivery_filters_cpv_region_exclude_and_fields(tmp_path: Path) -> None:
    """REQ-DEL-004: exclude_keywords, cpv_prefixes, regions, field_filters (AND/OR) and PII safety."""
    from app.pipeline.delivery import match_filters

    filters = DeliveryFilters(
        event_types=["new_entity", "price_drop"],
        keywords=["ремонт"],
        exclude_keywords=["субвенція", "ліфт"],
        cpv_prefixes=["45", "3019"],
        regions=["Київ", "Львів"],
        field_filters={"status": ["active*", "complete"], "currency": ["UAH"]},
        min_amount=10_000,
    )

    good_payload = {
        "title": "Капітальний ремонт школи",
        "cpv": "45210000-2",
        "region": "Київська область",
        "city": "Бориспіль",
        "status": "active.tendering",
        "currency": "UAH",
        "amount": 250_000,
    }
    ch_good = ChangeEvent(type="new_entity", record_key="lot:1")
    assert match_filters(ch_good, good_payload, filters) is True

    # Stop-word overrides positive keyword match
    assert match_filters(ch_good, {**good_payload, "title": "Ремонт школи (субвенція)"}, filters) is False
    # Wrong CPV prefix or missing CPV
    assert match_filters(ch_good, {**good_payload, "cpv": "33100000-1"}, filters) is False
    assert match_filters(ch_good, {k: v for k, v in good_payload.items() if k != "cpv"}, filters) is False
    # Region mismatch (city fallback works for Львів)
    assert match_filters(ch_good, {**good_payload, "region": "", "city": "м. Львів"}, filters) is True
    assert match_filters(ch_good, {**good_payload, "region": "Одеська", "city": "Одеса"}, filters) is False
    # field_filters exact + wildcard prefix
    assert match_filters(ch_good, {**good_payload, "status": "cancelled"}, filters) is False
    assert match_filters(ch_good, {**good_payload, "status": "Complete", "currency": "uah"}, filters) is True

    # Personal PII columns must be redacted before filtering
    sent_bodies: list[dict] = []
    settings = make_settings(tmp_path, allow_private_network=True, delivery_max_retries=1)
    store = OperationalStore(settings.paths)
    try:
        task_id = _make_sample_task(store, with_personal_pii=True)
        task = store.get_task(task_id)
        assert task is not None
        store.create_subscription(
            task_id,
            name="PII Filter Guard",
            channel="webhook",
            target="https://93.184.216.34/pii-filter",
            filters=DeliveryFilters(keywords=["таємне_слово"]),
        )
        dispatcher = DeltaDispatcher(
            settings,
            store,
            transport=httpx.MockTransport(
                lambda req: (sent_bodies.append(json.loads(req.content.decode("utf-8"))), httpx.Response(200))[1]
            ),
            retry_delay_s=0.0,
        )
        # "таємне_слово" exists ONLY in personal PII column -> must NOT match after redaction!
        res = await dispatcher.dispatch_changes(
            task,
            version=1,
            run_id="r-pii",
            changes=[ChangeEvent(type="new_entity", record_key="lot:pii")],
            records_by_key={
                "lot:pii": {
                    "tender_id": "UA-1",
                    "title": "Офісні меблі",
                    "amount": 50_000,
                    "contact_person": "таємне_слово Іван",
                }
            },
        )
        assert res["sent"] == 0
        assert res["filtered_out"] == 1
        assert sent_bodies == []
        await dispatcher.aclose()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_digest_daily_queue_scheduler_flush_and_failure_safety(tmp_path: Path) -> None:
    """REQ-DEL-005: digest_daily validates cron, persists queue across restart, flushes on tick/API & preserves on failure."""
    from datetime import timedelta

    from app.pipeline.scheduler import TaskScheduler

    settings = make_settings(
        tmp_path,
        scheduler_enabled=False,
        allow_private_network=True,
        delivery_max_retries=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        t_resp = client.post(
            "/api/tasks",
            json={"prompt": "Дайджест тендерів Prozorro", "seeds": [f"{API_BASE}/tenders?limit=10"]},
        )
        assert t_resp.status_code == 201
        task_id = t_resp.json()["task"]["id"]

        # Invalid digest_cron must return 422
        bad_cron = client.post(
            f"/api/tasks/{task_id}/subscriptions",
            json={
                "name": "Bad Cron",
                "channel": "webhook",
                "target": "https://93.184.216.34/digest",
                "filters": {"delivery_mode": "digest_daily", "digest_cron": "99 99 * * *"},
            },
        )
        assert bad_cron.status_code == 422

        # Create valid daily digest subscription
        good_sub = client.post(
            f"/api/tasks/{task_id}/subscriptions",
            json={
                "name": "Daily 06:00",
                "channel": "webhook",
                "target": "https://93.184.216.34/digest",
                "secret": "digest-secret",
                "filters": {
                    "event_types": ["new_entity", "price_drop"],
                    "delivery_mode": "digest_daily",
                    "digest_cron": "0 6 * * *",
                },
            },
        )
        assert good_sub.status_code == 201, good_sub.text
        sub_data = good_sub.json()["subscription"]
        sub_id = sub_data["id"]
        assert sub_data["next_digest_at"] is not None
        assert sub_data["pending_digest_count"] == 0

        # Unknown subscription flush returns 404
        not_found = client.post(f"/api/tasks/{task_id}/subscriptions/nonexistent/flush")
        assert not_found.status_code == 404

    # Simulate two runs queuing items via Studio/DeltaDispatcher with restart between runs
    deliveries: list[tuple[dict[str, str], dict]] = []
    fail_mode = {"fail": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if fail_mode["fail"]:
            return httpx.Response(500, text="temporary outage")
        deliveries.append(
            ({k.lower(): v for k, v in req.headers.items()}, json.loads(req.content.decode("utf-8")))
        )
        return httpx.Response(200, json={"ok": True})

    studio1 = Studio(settings)
    studio1.dispatcher = DeltaDispatcher(
        settings, studio1.store, transport=httpx.MockTransport(handler), retry_delay_s=0.0
    )
    try:
        task = studio1.store.get_task(task_id)
        assert task is not None
        res1 = await studio1.dispatcher.dispatch_changes(
            task,
            version=2,
            run_id="run-2",
            changes=[ChangeEvent(type="new_entity", record_key="lot:1")],
            records_by_key={"lot:1": {"title": "Лот 1", "amount": 100_000}},
        )
        assert res1["sent"] == 0
        assert res1["queued"] == 1
        assert deliveries == []
    finally:
        await studio1.aclose()

    # Reopen Studio (simulated process restart) and queue second run
    studio2 = Studio(settings)
    studio2.dispatcher = DeltaDispatcher(
        settings, studio2.store, transport=httpx.MockTransport(handler), retry_delay_s=0.0
    )
    try:
        task = studio2.store.get_task(task_id)
        assert task is not None
        sub = studio2.store.get_subscription(sub_id)
        assert sub is not None
        assert sub.pending_digest_count == 1

        res2 = await studio2.dispatcher.dispatch_changes(
            task,
            version=3,
            run_id="run-3",
            changes=[ChangeEvent(type="price_drop", record_key="lot:2", field="amount", old=90_000, new=70_000)],
            records_by_key={"lot:2": {"title": "Лот 2", "amount": 70_000}},
        )
        assert res2["queued"] == 1
        assert deliveries == []

        sub_before_tick = studio2.store.get_subscription(sub_id)
        assert sub_before_tick is not None and sub_before_tick.next_digest_at is not None
        due_at = sub_before_tick.next_digest_at

        scheduler = TaskScheduler(studio2)
        # Before due time: nothing flushed
        await scheduler.tick(now=due_at - timedelta(minutes=5))
        assert deliveries == []

        # At due time: single combined digest payload sent
        await scheduler.tick(now=due_at)
        assert len(deliveries) == 1
        headers, body = deliveries[0]
        assert body["event"] == "digest"
        assert len(body["changes"]) == 2
        assert "x-daas-signature" in headers

        sub_after_tick = studio2.store.get_subscription(sub_id)
        assert sub_after_tick is not None
        assert sub_after_tick.pending_digest_count == 0
        assert sub_after_tick.last_digest_at is not None
        assert sub_after_tick.next_digest_at is not None
        assert sub_after_tick.next_digest_at > due_at

        # Next due slot with empty queue must NOT issue any HTTP request
        deliveries.clear()
        await scheduler.tick(now=sub_after_tick.next_digest_at)
        assert deliveries == []

        # Queue another change and simulate transient failure -> queue must NOT be lost!
        await studio2.dispatcher.dispatch_changes(
            task,
            version=4,
            run_id="run-4",
            changes=[ChangeEvent(type="new_entity", record_key="lot:3")],
            records_by_key={"lot:3": {"title": "Лот 3", "amount": 55_000}},
        )
        fail_mode["fail"] = True
        sub_retry = studio2.store.get_subscription(sub_id)
        assert sub_retry is not None and sub_retry.next_digest_at is not None
        failed_due = sub_retry.next_digest_at
        await scheduler.tick(now=failed_due)

        sub_after_fail = studio2.store.get_subscription(sub_id)
        assert sub_after_fail is not None
        assert sub_after_fail.pending_digest_count == 1
        assert sub_after_fail.next_digest_at is not None and sub_after_fail.next_digest_at > failed_due

        # Recovery + manual flush endpoint
        fail_mode["fail"] = False
        app2 = create_app(settings)
        with TestClient(app2) as client2:
            # Replace dispatcher transport on app's studio for offline verification
            app2.state.studio.dispatcher._client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=False
            )
            flush_resp = client2.post(f"/api/tasks/{task_id}/subscriptions/{sub_id}/flush")
            assert flush_resp.status_code == 200, flush_resp.text
            assert flush_resp.json()["ok"] is True
            assert flush_resp.json()["count"] == 1
            assert len(deliveries) == 1
            assert deliveries[0][1]["changes"][0]["record_key"] == "lot:3"

            # Second flush has nothing left to send
            flush_empty = client2.post(f"/api/tasks/{task_id}/subscriptions/{sub_id}/flush")
            assert flush_empty.status_code == 200
            assert flush_empty.json()["count"] == 0
            assert len(deliveries) == 1
    finally:
        await studio2.aclose()


@pytest.mark.asyncio
async def test_telegram_formatting_and_xlsx_attachment_over_limit(tmp_path: Path) -> None:
    """REQ-DEL-006: formatted Telegram messages, XLSX sendDocument > inline limit, PII masking, defuse, partial fail."""
    import io
    from email.parser import BytesParser

    from openpyxl import load_workbook

    calls: list[tuple[str, dict[str, str], bytes]] = []
    fail_doc = {"flag": False}

    def tg_handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        calls.append((url, {k.lower(): v for k, v in req.headers.items()}, req.content))
        if fail_doc["flag"] and url.endswith("/sendDocument"):
            return httpx.Response(500, text="doc upload error")
        return httpx.Response(200, json={"ok": True})

    secret_token = "112233:BOT_SECRET_TOKEN"
    settings = make_settings(
        tmp_path,
        allow_private_network=True,
        telegram_bot_token=secret_token,
        delivery_max_retries=1,
        delivery_inline_limit=10,
    )
    store = OperationalStore(settings.paths)
    try:
        task_id = _make_sample_task(store, with_personal_pii=True)
        task = store.get_task(task_id)
        assert task is not None
        store.create_subscription(
            task_id,
            name="TG Prozorro",
            channel="telegram",
            target="-100777666555",
        )
        dispatcher = DeltaDispatcher(
            settings, store, transport=httpx.MockTransport(tg_handler), retry_delay_s=0.0
        )

        # 1) <= 10 changes -> sendMessage only, NO sendDocument
        small_changes = [
            ChangeEvent(
                type="new_entity",
                record_key=f"s:{i}",
                source_url=f"{API_BASE}/tenders/s-{i}",
            )
            for i in range(10)
        ]
        small_records = {
            f"s:{i}": {
                "tender_id": f"UA-S-{i}",
                "title": f"Папір {i}",
                "amount": 150_000,
                "currency": "UAH",
                "region": "Київська область",
                "cpv": "30197630-1",
                "contact_person": "Олександр Шевченко",
            }
            for i in range(10)
        }
        res_small = await dispatcher.dispatch_changes(
            task, version=1, run_id="r1", changes=small_changes, records_by_key=small_records
        )
        assert res_small["sent"] == 1
        assert [c[0].rsplit("/", 1)[-1] for c in calls] == ["sendMessage"]
        msg_text = json.loads(calls[0][2].decode("utf-8"))["text"]
        assert "150 000" in msg_text
        assert "Київська область" in msg_text
        assert "30197630-1" in msg_text
        assert "Олександр Шевченко" not in msg_text

        # 2) 12 changes (> 10 inline limit) -> sendMessage + sendDocument (.xlsx)
        calls.clear()
        large_changes = [
            ChangeEvent(
                type="new_entity",
                record_key=f"b:{i}",
                source_url=f"{API_BASE}/tenders/b-{i}",
            )
            for i in range(12)
        ]
        large_records = {
            f"b:{i}": {
                "tender_id": f"UA-B-{i}",
                "title": "=cmd|'/C calc'!A0" if i == 0 else f"Будівництво {i}",
                "amount": 250_000 + i,
                "currency": "UAH",
                "region": "Львівська область",
                "cpv": "45210000-2",
                "contact_person": "Олександр Шевченко",
            }
            for i in range(12)
        }
        res_large = await dispatcher.dispatch_changes(
            task, version=2, run_id="r2", changes=large_changes, records_by_key=large_records
        )
        assert res_large["sent"] == 1
        endpoints = [c[0].rsplit("/", 1)[-1] for c in calls]
        assert endpoints == ["sendMessage", "sendDocument"]

        doc_call = calls[1]
        raw_mime = b"Content-Type: " + doc_call[1]["content-type"].encode("latin-1") + b"\r\n\r\n" + doc_call[2]
        parsed_mime = BytesParser().parsebytes(raw_mime)
        xlsx_bytes = None
        for part in parsed_mime.walk():
            if part.get_param("name", header="content-disposition") == "document":
                xlsx_bytes = part.get_payload(decode=True)
                assert (part.get_filename() or "").endswith(".xlsx")
        assert xlsx_bytes is not None
        assert "Олександр Шевченко".encode() not in xlsx_bytes

        wb = load_workbook(io.BytesIO(xlsx_bytes))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        # Header + 12 data rows
        assert len(rows) == 13
        joined_xlsx = "\n".join(str(cell) for row in rows for cell in row if cell is not None)
        assert "Олександр Шевченко" not in joined_xlsx
        assert "[redacted]" in joined_xlsx
        assert "'=cmd|'/C calc'!A0" in joined_xlsx

        # 3) Partial failure: sendMessage succeeds, sendDocument fails -> delivery_log keeps dedup, event logs attachment error
        calls.clear()
        fail_doc["flag"] = True
        fail_changes = [
            ChangeEvent(type="new_entity", record_key=f"f:{i}", source_url=f"{API_BASE}/tenders/f-{i}")
            for i in range(11)
        ]
        fail_records = {
            f"f:{i}": {"tender_id": f"UA-F-{i}", "title": f"Лот {i}", "amount": 1000 + i}
            for i in range(11)
        }
        await dispatcher.dispatch_changes(
            task, version=3, run_id="r3", changes=fail_changes, records_by_key=fail_records
        )
        # Re-dispatching same batch must NOT resend text message (dedup recorded for sendMessage)
        calls.clear()
        res_dup = await dispatcher.dispatch_changes(
            task, version=3, run_id="r3", changes=fail_changes, records_by_key=fail_records
        )
        assert res_dup["skipped_dedup"] == 11
        assert calls == []

        events = store.list_events(task_id, limit=50)
        attach_fail = [e for e in events if e.type == "delivery_failed" and e.data.get("attachment") is True]
        assert attach_fail
        assert secret_token not in attach_fail[0].message
        await dispatcher.aclose()
    finally:
        store.close()


def test_cli_notify_advanced_filters_and_flush(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-DEL-004, REQ-DEL-005, REQ-OPS-030: `daas notify add` filter/digest flags and `daas notify flush`."""
    settings = make_settings(tmp_path, scheduler_enabled=False, allow_private_network=True)
    hook_calls: list[dict] = []

    def _cli_studio(_settings=None):
        st = Studio(settings)
        st.dispatcher = DeltaDispatcher(
            settings,
            st.store,
            transport=httpx.MockTransport(
                lambda req: (
                    hook_calls.append(json.loads(req.content.decode("utf-8"))),
                    httpx.Response(200, json={"ok": True}),
                )[1]
            ),
            retry_delay_s=0.0,
        )
        st.runner.dispatcher = st.dispatcher
        return st

    monkeypatch.setattr("app.cli.get_settings", lambda: settings)
    monkeypatch.setattr("app.cli._studio", _cli_studio)

    setup = _cli_studio()
    task = asyncio.run(setup.create_task("CLI digest task", [f"{API_BASE}/tenders?limit=10"]))
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
            "https://93.184.216.34/cli-digest",
            "--name",
            "CLI Digest",
            "--exclude",
            "субвенція",
            "--cpv",
            "45",
            "--region",
            "Київ",
            "--field",
            "status=active*",
            "--mode",
            "digest_daily",
            "--digest-cron",
            "0 6 * * *",
        ],
    )
    assert res_add.exit_code == 0, res_add.output
    sub_id = res_add.output.strip().splitlines()[0].split()[1]

    # Queue an item for that subscription
    st2 = _cli_studio()
    t_loaded = st2.store.get_task(task.id)
    assert t_loaded is not None
    asyncio.run(
        st2.dispatcher.dispatch_changes(
            t_loaded,
            version=2,
            run_id="r-cli",
            changes=[ChangeEvent(type="new_entity", record_key="cli:1")],
            records_by_key={
                "cli:1": {
                    "title": "Ремонт даху",
                    "cpv": "45260000-7",
                    "region": "Київ",
                    "status": "active.tendering",
                }
            },
        )
    )
    asyncio.run(st2.aclose())
    assert hook_calls == []

    res_flush = runner.invoke(cli_app, ["notify", "flush", task.id, sub_id])
    assert res_flush.exit_code == 0, res_flush.output
    assert len(hook_calls) == 1
    assert hook_calls[0]["event"] == "digest"
    assert len(hook_calls[0]["changes"]) == 1

