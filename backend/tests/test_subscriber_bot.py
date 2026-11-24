"""Offline tests for the thin Telegram subscriber bot (SDD 015: REQ-DEL-008..011)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.bot.app import create_bot_app, poll_bot_once
from app.bot.client import StudioApiClient, TelegramBotClient
from app.bot.service import SubscriberBotService
from app.bot.state import BotSubscriberStore
from app.cli import app as cli_app
from app.config import reset_settings_cache
from app.main import create_app
from app.models import DeliveryFilters
from app.runtime import Studio
from tests.conftest import FixtureFetcher, make_settings


def _mock_telegram_client(sent_messages: list[dict[str, Any]], callbacks: list[dict[str, Any]] | None = None) -> TelegramBotClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8") or "{}")
        if request.url.path.endswith("/sendMessage"):
            sent_messages.append(payload)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent_messages)}})
        if request.url.path.endswith("/answerCallbackQuery"):
            if callbacks is not None:
                callbacks.append(payload)
            return httpx.Response(200, json={"ok": True, "result": True})
        return httpx.Response(404, json={"ok": False, "description": "unknown method"})

    return TelegramBotClient("123:test-token", transport=httpx.MockTransport(handler))


def _make_studio_app(tmp_path: Path, *, token: str = "bot-studio-secret-123456"):
    settings = make_settings(
        tmp_path,
        disable_auth=False,
        api_token=token,
        bot_studio_token=token,
        bot_webhook_secret="tg-webhook-secret",
        bot_billing_secret="billing-hmac-secret",
        bot_payment_url="https://pay.example/checkout",
    )
    created_studios: list[Studio] = []

    def studio_factory(cfg):
        studio = Studio(cfg, fetcher=FixtureFetcher())
        created_studios.append(studio)
        return studio

    studio_app = create_app(settings, studio_factory=studio_factory)
    return settings, studio_app, created_studios


def test_bot_uses_studio_rest_api_and_isolates_chat_ids(tmp_path: Path) -> None:
    """REQ-DEL-008: bot uses Studio REST API only and strictly isolates chat_id state."""
    settings, studio_app, _studios = _make_studio_app(tmp_path)

    with TestClient(studio_app) as studio_tc:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        create_resp = studio_tc.post(
            "/api/tasks",
            headers=headers,
            json={
                "prompt": "Prozorro тендери на серверне обладнання",
                "seeds": ["http://demo.local/catalog.html"],
                "title": "Сервери Prozorro",
            },
        )
        assert create_resp.status_code == 201
        task_id = create_resp.json()["task"]["id"]

        # Seed subscriptions for chat 101000101, chat 999000202, and a webhook (must stay isolated).
        r1 = studio_tc.post(
            f"/api/tasks/{task_id}/subscriptions",
            headers=headers,
            json={"name": "tg-101", "channel": "telegram", "target": "101000101", "filters": {}},
        )
        r2 = studio_tc.post(
            f"/api/tasks/{task_id}/subscriptions",
            headers=headers,
            json={"name": "tg-202", "channel": "telegram", "target": "999000202", "filters": {}},
        )
        r3 = studio_tc.post(
            f"/api/tasks/{task_id}/subscriptions",
            headers=headers,
            json={
                "name": "internal-webhook",
                "channel": "webhook",
                "target": "https://hooks.example/secret-endpoint",
                "secret": "whsec-top-secret",
                "filters": {},
            },
        )
        assert r1.status_code == 201 and r2.status_code == 201 and r3.status_code == 201

        async def run_flow() -> None:
            sent: list[dict[str, Any]] = []
            callbacks: list[dict[str, Any]] = []
            api_client = StudioApiClient(
                "http://testserver",
                token=settings.api_token or "",
                transport=httpx.ASGITransport(app=studio_app),
            )
            tg_client = _mock_telegram_client(sent, callbacks)
            store = BotSubscriberStore(settings.bot_state_path, trial_days=7)
            service = SubscriberBotService(api_client, store, tg_client)
            try:
                await service.handle_update({"message": {"chat": {"id": 101000101}, "text": "/status"}})
                status_msg = sent[-1]["text"]
                assert "101000101" in status_msg
                assert "999000202" not in status_msg
                assert "hooks.example" not in status_msg
                assert "whsec-top-secret" not in status_msg
                assert (settings.api_token or "") not in status_msg

                # Inline callback button select + pause for chat 101000101 must not affect chat 999000202 or webhook.
                await service.handle_update(
                    {
                        "callback_query": {
                            "id": "cb-1",
                            "message": {"chat": {"id": 101000101}},
                            "data": f"select:{task_id}",
                        }
                    }
                )
                assert callbacks and callbacks[-1]["callback_query_id"] == "cb-1"

                await service.handle_update({"message": {"chat": {"id": 101000101}, "text": "/pause"}})
            finally:
                await api_client.aclose()
                await tg_client.aclose()

            # Check unreachable Studio API failure handling
            def failing_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("connection refused", request=request)

            broken_api = StudioApiClient(
                "http://127.0.0.1:59999",
                token="super-secret-bearer-token",
                transport=httpx.MockTransport(failing_handler),
            )
            broken_sent: list[dict[str, Any]] = []
            broken_tg = _mock_telegram_client(broken_sent)
            broken_svc = SubscriberBotService(broken_api, store, broken_tg)
            try:
                await broken_svc.handle_update({"message": {"chat": {"id": 101000101}, "text": "/tasks"}})
                err_text = broken_sent[-1]["text"]
                assert "недоступ" in err_text.lower() or "помилк" in err_text.lower()
                assert "super-secret-bearer-token" not in err_text
            finally:
                await broken_api.aclose()
                await broken_tg.aclose()

        import asyncio

        asyncio.run(run_flow())

        subs = studio_tc.get(f"/api/tasks/{task_id}/subscriptions", headers=headers).json()["items"]
        by_name = {item["name"]: item for item in subs}
        assert by_name["tg-101"]["enabled"] is False
        assert by_name["tg-202"]["enabled"] is True
        assert by_name["internal-webhook"]["enabled"] is True


def test_bot_free_trial_upgrade_and_plan_enforcement(tmp_path: Path) -> None:
    """REQ-DEL-009: 7-day Free Trial on /start, free plan blocks instant, /upgrade exposes safe URL."""
    settings, studio_app, _studios = _make_studio_app(tmp_path)

    with TestClient(studio_app) as studio_tc:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        create_resp = studio_tc.post(
            "/api/tasks",
            headers=headers,
            json={
                "prompt": "Prozorro закупівлі електромонтажу",
                "seeds": ["http://demo.local/catalog.html"],
                "title": "Електромонтаж",
            },
        )
        task_id = create_resp.json()["task"]["id"]

        clock = {"now": datetime(2026, 11, 1, 10, 0, tzinfo=UTC)}

        async def run_flow() -> None:
            sent: list[dict[str, Any]] = []
            api_client = StudioApiClient(
                "http://testserver",
                token=settings.api_token or "",
                transport=httpx.ASGITransport(app=studio_app),
            )
            tg_client = _mock_telegram_client(sent)
            store = BotSubscriberStore(
                settings.bot_state_path, trial_days=7, now_fn=lambda: clock["now"]
            )
            service = SubscriberBotService(
                api_client, store, tg_client, payment_url=settings.bot_payment_url
            )
            try:
                await service.handle_update({"message": {"chat": {"id": 500}, "text": "/start"}})
                start_text = sent[-1]["text"]
                assert "trial" in start_text.lower() or "тріал" in start_text.lower() or "7" in start_text

                await service.handle_update({"message": {"chat": {"id": 500}, "text": f"/select {task_id}"}})
                await service.handle_update({"message": {"chat": {"id": 500}, "text": "/mode instant"}})
                assert "instant" in sent[-1]["text"].lower()

                subs = await api_client.list_subscriptions(task_id)
                own = [s for s in subs if s["target"] == "500"]
                assert len(own) == 1
                assert own[0]["filters"]["delivery_mode"] == "instant"

                # Switch to digest, then expire trial (> 7 days)
                await service.handle_update({"message": {"chat": {"id": 500}, "text": "/mode digest"}})
                clock["now"] = clock["now"] + timedelta(days=8)

                await service.handle_update({"message": {"chat": {"id": 500}, "text": "/mode instant"}})
                denied_text = sent[-1]["text"]
                assert "/upgrade" in denied_text
                subs_after = await api_client.list_subscriptions(task_id)
                own_after = [s for s in subs_after if s["target"] == "500"]
                assert own_after[0]["filters"]["delivery_mode"] == "digest_daily"

                await service.handle_update({"message": {"chat": {"id": 500}, "text": "/upgrade"}})
                upgrade_text = sent[-1]["text"]
                assert "https://pay.example/checkout" in upgrade_text
                assert "chat_id=500" in upgrade_text
                assert "pro" in upgrade_text.lower()
            finally:
                await api_client.aclose()
                await tg_client.aclose()

        import asyncio

        asyncio.run(run_flow())


def test_bot_filter_setup_pause_resume_and_flush(tmp_path: Path) -> None:
    """REQ-DEL-010: full self-service filter flow, pause/resume, flush, and invalid arg guards."""
    settings, studio_app, studios = _make_studio_app(tmp_path)

    with TestClient(studio_app) as studio_tc:
        headers = {"Authorization": f"Bearer {settings.api_token}"}
        t1 = studio_tc.post(
            "/api/tasks",
            headers=headers,
            json={
                "prompt": "Будівництво та ремонт доріг",
                "seeds": ["http://demo.local/catalog.html"],
                "title": "Дороги Prozorro",
            },
        ).json()["task"]["id"]
        t2 = studio_tc.post(
            "/api/tasks",
            headers=headers,
            json={
                "prompt": "Закупівля ІТ-обладнання",
                "seeds": ["http://demo.local/catalog.html"],
                "title": "ІТ-тендери",
            },
        ).json()["task"]["id"]

        async def run_flow() -> None:
            sent: list[dict[str, Any]] = []
            api_client = StudioApiClient(
                "http://testserver",
                token=settings.api_token or "",
                transport=httpx.ASGITransport(app=studio_app),
            )
            tg_client = _mock_telegram_client(sent)
            store = BotSubscriberStore(settings.bot_state_path, trial_days=7)
            service = SubscriberBotService(api_client, store, tg_client)
            try:
                # Filter edit before selecting task should give a clear prompt
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/cpv 4531"}})
                assert "/select" in sent[-1]["text"] or "/tasks" in sent[-1]["text"]

                # Invalid task selection
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/select unknown-task"}})
                assert "не знайдено" in sent[-1]["text"].lower()

                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/start"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": f"/select {t2}"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/cpv 4531, 4500"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/region Київ; Львівська"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/budget 500000-2000000"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/keywords сервер, комутатор"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/exclude канцтовари, папір"}})
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/mode instant"}})

                # Invalid budget must not overwrite existing budget
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/budget abc"}})
                assert "формат" in sent[-1]["text"].lower() or "числ" in sent[-1]["text"].lower()

                subs = await api_client.list_subscriptions(t2)
                assert len(subs) == 1
                flt = DeliveryFilters(**subs[0]["filters"])
                assert flt.cpv_prefixes == ["4531", "4500"]
                assert flt.regions == ["Київ", "Львівська"]
                assert flt.min_amount == 500000.0
                assert flt.max_amount == 2000000.0
                assert flt.keywords == ["сервер", "комутатор"]
                assert flt.exclude_keywords == ["канцтовари", "папір"]
                assert flt.delivery_mode == "instant"
                assert subs[0]["enabled"] is True

                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/pause"}})
                paused = await api_client.list_subscriptions(t2)
                assert paused[0]["enabled"] is False

                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/resume"}})
                resumed = await api_client.list_subscriptions(t2)
                assert resumed[0]["enabled"] is True

                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/mode digest"}})
                # Configure offline transport on Studio's dispatcher and queue a digest item
                from app.pipeline.delivery import DeltaDispatcher

                studios[0].settings = studios[0].settings.model_copy(
                    update={"telegram_bot_token": "123:studio-delivery"}
                )
                await studios[0].dispatcher.aclose()
                studios[0].dispatcher = DeltaDispatcher(
                    studios[0].settings,
                    studios[0].store,
                    transport=httpx.MockTransport(
                        lambda _req: httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
                    ),
                    retry_delay_s=0.0,
                )
                studios[0].store.enqueue_digest_items(
                    resumed[0]["id"],
                    t2,
                    1,
                    "run-test",
                    items=[
                        (
                            "tender-1",
                            {"type": "new_entity", "record_key": "tender-1"},
                            {"title": "Сервери для школи", "amount": 900000},
                        )
                    ],
                )
                await service.handle_update({"message": {"chat": {"id": 777}, "text": "/flush"}})
                assert "1" in sent[-1]["text"] or "дайджест" in sent[-1]["text"].lower()
                flushed = await api_client.list_subscriptions(t2)
                assert flushed[0]["pending_digest_count"] == 0
                # Ensure t1 was never touched
                assert await api_client.list_subscriptions(t1) == []
            finally:
                await api_client.aclose()
                await tg_client.aclose()

        import asyncio

        asyncio.run(run_flow())


def test_bot_webhooks_polling_and_cli_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ-DEL-011: webhook secrets, billing HMAC webhook, poll_bot_once, and CLI daas bot."""
    settings, studio_app, _studios = _make_studio_app(tmp_path)
    sent: list[dict[str, Any]] = []

    def tg_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 41,
                            "message": {"chat": {"id": 909}, "text": "/start"},
                        }
                    ],
                },
            )
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content.decode("utf-8") or "{}"))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})
        return httpx.Response(200, json={"ok": True, "result": True})

    tg_transport = httpx.MockTransport(tg_handler)
    studio_transport = httpx.ASGITransport(app=studio_app)

    # 1. Fail-closed billing webhook when secret is unconfigured
    unconfigured = settings.model_copy(update={"bot_billing_secret": None})
    with TestClient(studio_app), TestClient(
        create_bot_app(
            unconfigured,
            studio_transport=studio_transport,
            telegram_transport=tg_transport,
        )
    ) as bot_tc:
        resp_unconf = bot_tc.post(
            "/billing/webhook",
            json={"chat_id": "909", "plan": "pro", "days": 30},
        )
        assert resp_unconf.status_code == 503

    # 2. Configured webhooks: /health, /telegram/webhook auth, /billing/webhook HMAC + /status check
    bot_app = create_bot_app(
        settings,
        studio_transport=studio_transport,
        telegram_transport=tg_transport,
    )
    with TestClient(studio_app) as studio_tc, TestClient(bot_app) as bot_tc:
        # Ensure at least one task exists in Studio
        studio_tc.post(
            "/api/tasks",
            headers={"Authorization": f"Bearer {settings.api_token}"},
            json={"prompt": "Демо моніторинг", "seeds": ["http://demo.local/catalog.html"]},
        )

        health = bot_tc.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        # Telegram webhook secret check
        bad_tg = bot_tc.post(
            "/telegram/webhook",
            json={"update_id": 1, "message": {"chat": {"id": 909}, "text": "/status"}},
        )
        assert bad_tg.status_code == 401

        bad_tg2 = bot_tc.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            json={"update_id": 1, "message": {"chat": {"id": 909}, "text": "/status"}},
        )
        assert bad_tg2.status_code == 401

        # Billing webhook bad HMAC -> 401
        billing_body = json.dumps({"chat_id": "909", "plan": "pro", "days": 30}).encode("utf-8")
        bad_bill = bot_tc.post(
            "/billing/webhook",
            content=billing_body,
            headers={"Content-Type": "application/json", "X-Signature": "sha256=deadbeef"},
        )
        assert bad_bill.status_code == 401

        sig = hmac.new(
            (settings.bot_billing_secret or "").encode("utf-8"),
            billing_body,
            hashlib.sha256,
        ).hexdigest()
        ok_bill = bot_tc.post(
            "/billing/webhook",
            content=billing_body,
            headers={"Content-Type": "application/json", "X-Signature": f"sha256={sig}"},
        )
        assert ok_bill.status_code == 200
        assert ok_bill.json()["profile"]["plan"] == "pro"

        ok_tg = bot_tc.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "tg-webhook-secret"},
            json={"update_id": 2, "message": {"chat": {"id": 909}, "text": "/status"}},
        )
        assert ok_tg.status_code == 200
        assert ok_tg.json()["handled"] is True
        assert "pro" in sent[-1]["text"].lower()

        # 3. poll_bot_once
        async def run_poll() -> int | None:
            api_client = StudioApiClient(
                "http://testserver",
                token=settings.api_token or "",
                transport=studio_transport,
            )
            tg_client = TelegramBotClient("123:test", transport=tg_transport)
            store = BotSubscriberStore(settings.bot_state_path, trial_days=7)
            svc = SubscriberBotService(api_client, store, tg_client)
            try:
                return await poll_bot_once(tg_client, svc, offset=None, timeout=1)
            finally:
                await api_client.aclose()
                await tg_client.aclose()

        import asyncio

        next_offset = asyncio.run(run_poll())
        assert next_offset == 42

    # 4. CLI commands: daas bot grant & daas bot status
    monkeypatch.setenv("DAAS_STORAGE_DIR", str(settings.paths.root))
    monkeypatch.setenv("DAAS_BOT_STATE_FILE", str(settings.bot_state_path))
    monkeypatch.setenv("DAAS_OFFLINE", "true")
    reset_settings_cache()

    runner = CliRunner()
    res_grant = runner.invoke(cli_app, ["bot", "grant", "888", "--plan", "b2b", "--days", "60"])
    assert res_grant.exit_code == 0, res_grant.output
    assert "b2b" in res_grant.output.lower()

    res_status = runner.invoke(cli_app, ["bot", "status", "--chat-id", "888"])
    assert res_status.exit_code == 0, res_status.output
    assert "888" in res_status.output
    assert "b2b" in res_status.output.lower()
