"""FastAPI webhook app (Telegram + billing) and long-polling runner for the bot."""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import Settings, get_settings, load_runtime_token
from .client import StudioApiClient, TelegramBotClient
from .service import SubscriberBotService
from .state import BotSubscriberStore, SubscriberPlan

logger = logging.getLogger(__name__)


class BillingWebhookRequest(BaseModel):
    """Authenticated payment callback activating a subscriber's paid plan."""

    chat_id: str = Field(min_length=1, max_length=64)
    plan: SubscriberPlan = "pro"
    days: int = Field(default=30, ge=1, le=3650)


def _resolve_studio_token(settings: Settings) -> str:
    if settings.bot_studio_token:
        return settings.bot_studio_token
    if settings.api_token:
        return settings.api_token
    if settings.disable_auth:
        return ""
    return load_runtime_token(settings)


def build_bot_service(
    settings: Settings,
    *,
    studio_transport: httpx.AsyncBaseTransport | None = None,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[SubscriberBotService, StudioApiClient, TelegramBotClient]:
    """Assemble isolated REST/Telegram clients and subscriber JSON store."""
    studio = StudioApiClient(
        settings.bot_studio_url,
        token=_resolve_studio_token(settings),
        timeout_s=settings.delivery_timeout_s,
        transport=studio_transport,
    )
    telegram = TelegramBotClient(
        settings.telegram_bot_token,
        timeout_s=settings.bot_poll_timeout_s + 5.0,
        transport=telegram_transport,
    )
    store = BotSubscriberStore(
        settings.bot_state_path,
        trial_days=settings.bot_trial_days,
    )
    service = SubscriberBotService(
        studio,
        store,
        telegram,
        payment_url=settings.bot_payment_url,
    )
    return service, studio, telegram


def _verify_billing_auth(request: Request, body: bytes, secret: str) -> None:
    header_secret = (request.headers.get("X-Billing-Secret") or "").strip()
    if header_secret and hmac.compare_digest(header_secret, secret):
        return
    sig_header = (request.headers.get("X-Signature") or "").strip()
    if sig_header.lower().startswith("sha256="):
        provided = sig_header.split("=", 1)[1].strip().lower()
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest().lower()
        if provided and hmac.compare_digest(provided, expected):
            return
    raise HTTPException(status_code=401, detail="Невірний секрет або підпис платіжного webhook")


def create_bot_app(
    settings: Settings | None = None,
    *,
    service: SubscriberBotService | None = None,
    studio_transport: httpx.AsyncBaseTransport | None = None,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Create thin FastAPI webhook app exposing /health, /telegram/webhook, /billing/webhook."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.bot_service = service
            yield
            return
        built_service, studio_client, tg_client = build_bot_service(
            cfg,
            studio_transport=studio_transport,
            telegram_transport=telegram_transport,
        )
        app.state.bot_service = built_service
        try:
            yield
        finally:
            await studio_client.aclose()
            await tg_client.aclose()

    app = FastAPI(
        title="DaaS Subscriber Bot",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "studio_url": cfg.bot_studio_url}

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        expected_secret = (cfg.bot_webhook_secret or "").strip()
        if expected_secret:
            provided = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
            if not provided or not hmac.compare_digest(provided, expected_secret):
                raise HTTPException(status_code=401, detail="Невірний Telegram webhook secret")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некоректний JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Очікується JSON-об'єкт")
        bot_service: SubscriberBotService = request.app.state.bot_service
        handled = await bot_service.handle_update(payload)
        return {"ok": True, "handled": handled}

    @app.post("/billing/webhook")
    async def billing_webhook(request: Request) -> dict[str, Any]:
        billing_secret = (cfg.bot_billing_secret or "").strip()
        if not billing_secret:
            raise HTTPException(
                status_code=503,
                detail="Платіжний webhook вимкнено: не задано DAAS_BOT_BILLING_SECRET",
            )
        body = await request.body()
        _verify_billing_auth(request, body, billing_secret)
        try:
            req = BillingWebhookRequest.model_validate_json(body)
        except Exception as exc:  # noqa: BLE001 - pydantic validation error
            raise HTTPException(status_code=400, detail=f"Некоректний payload оплати: {exc}") from exc
        bot_service: SubscriberBotService = request.app.state.bot_service
        profile = bot_service.store.grant_plan(req.chat_id, req.plan, days=req.days)
        return {"ok": True, "profile": profile.model_dump(mode="json")}

    return app


async def poll_bot_once(
    telegram: TelegramBotClient,
    service: SubscriberBotService,
    *,
    offset: int | None = None,
    timeout: int = 25,
) -> int | None:
    """Fetch and process one batch of Telegram getUpdates, returning the next offset."""
    updates = await telegram.get_updates(offset=offset, timeout=timeout)
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        try:
            await service.handle_update(update)
        except Exception:  # noqa: BLE001 - isolate bad update inside polling loop
            logger.exception("failed to handle telegram update %s", update_id)
        if isinstance(update_id, int):
            next_offset = update_id + 1 if next_offset is None else max(next_offset, update_id + 1)
    return next_offset


async def run_bot_polling(
    settings: Settings,
    *,
    service: SubscriberBotService | None = None,
    max_iterations: int | None = None,
) -> None:
    """Run long-polling loop until cancelled or max_iterations reached."""
    if service is not None:
        assert service.telegram is not None
        tg = service.telegram
        studio = None
        svc = service
    else:
        svc, studio, tg = build_bot_service(settings)

    offset: int | None = None
    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            offset = await poll_bot_once(
                tg,
                svc,
                offset=offset,
                timeout=int(settings.bot_poll_timeout_s),
            )
            iterations += 1
    finally:
        if studio is not None:
            await studio.aclose()
            await tg.aclose()
