"""Outbound delta and owner-alert delivery (Webhook & Telegram Bot API, D-01)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
from typing import TYPE_CHECKING, Any

import httpx

from ..config import Settings
from ..models import ChangeEvent, DeliveryFilters, SubscriptionInfo, TaskInfo
from ..utils import assert_public_url, iso, parse_number

if TYPE_CHECKING:
    from ..db.sqlite_store import OperationalStore

logger = logging.getLogger(__name__)


def _personal_keys(task: TaskInfo) -> set[str]:
    return {col.key for col in task.spec.columns if col.pii == "personal"}


def _sanitize_payload(payload: dict[str, Any] | None, personal: set[str]) -> dict[str, Any]:
    if not payload:
        return {}
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key in personal:
            clean[key] = "[redacted]"
        else:
            clean[key] = value
    return clean


def _sanitize_change(change: ChangeEvent, personal: set[str]) -> dict[str, Any]:
    data = change.model_dump(mode="json")
    if change.field and change.field in personal:
        if data.get("old") is not None:
            data["old"] = "[redacted]"
        if data.get("new") is not None:
            data["new"] = "[redacted]"
    return data


def _extract_amount(change: ChangeEvent, payload: dict[str, Any] | None) -> float | None:
    if payload:
        for key in ("amount", "price", "value"):
            if payload.get(key) is not None:
                parsed = parse_number(payload[key])
                if parsed is not None:
                    return parsed
    if change.field in {"amount", "price"} and change.new is not None:
        return parse_number(change.new)
    return None


def match_filters(
    change: ChangeEvent,
    payload: dict[str, Any] | None,
    filters: DeliveryFilters,
) -> bool:
    """Return True if ``change`` matches every active condition in ``filters``."""
    if filters.event_types and change.type not in filters.event_types:
        return False

    if filters.keywords:
        parts = [change.record_key, str(change.field or ""), str(change.old or ""), str(change.new or "")]
        if payload:
            parts.extend(str(v) for v in payload.values() if v is not None)
        haystack = " ".join(parts).casefold()
        if not any(kw.strip().casefold() in haystack for kw in filters.keywords if kw.strip()):
            return False

    if filters.min_amount is not None or filters.max_amount is not None:
        amount = _extract_amount(change, payload)
        if amount is None:
            return False
        if filters.min_amount is not None and amount < filters.min_amount:
            return False
        if filters.max_amount is not None and amount > filters.max_amount:
            return False

    return True


def change_dedup_key(version: int, change: ChangeEvent) -> str:
    basis = f"v{version}|{change.type}|{change.record_key}|{change.field or ''}|{change.old}|{change.new}"
    digest = hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()[:20]
    return f"delta:v{version}:{digest}"


def _format_telegram_delta(
    task: TaskInfo,
    version: int,
    changes: list[dict[str, Any]],
    records_by_key: dict[str, dict[str, Any]],
) -> str:
    labels = {
        "new_entity": "🟢 Новий запис",
        "price_drop": "📉 Ціна впала",
        "field_changed": "✏️ Зміна поля",
        "removed_entity": "🔴 Зник",
    }
    lines = [f"<b>{html.escape(task.title)}</b> (v{version}, змін: {len(changes)})"]
    for item in changes[:10]:
        kind = labels.get(str(item.get("type", "")), str(item.get("type", "")))
        key = str(item.get("record_key", ""))
        rec = records_by_key.get(key) or {}
        headline = str(rec.get("title") or rec.get("name") or key)[:120]
        detail = f"\n{kind}: <b>{html.escape(headline)}</b>"
        amount = rec.get("amount") or rec.get("price")
        if amount is not None:
            detail += f" · {html.escape(str(amount))}"
        if item.get("field"):
            detail += (
                f" ({html.escape(str(item['field']))}: "
                f"{html.escape(str(item.get('old', '')))} → {html.escape(str(item.get('new', '')))})"
            )
        if item.get("delta_pct") is not None:
            detail += f" ({float(item['delta_pct']):+.1f}%)"
        if item.get("source_url"):
            detail += f"\n{html.escape(str(item['source_url']))}"
        lines.append(detail)
    if len(changes) > 10:
        lines.append(f"\n…та ще {len(changes) - 10} змін")
    text = "\n".join(lines)
    return text[:3900]


def _sanitize_error(message: str, *secrets: str | None) -> str:
    cleaned = message
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:400]


class DeltaDispatcher:
    """Delivers filtered change batches and owner operational alerts to subscribers."""

    def __init__(
        self,
        settings: Settings,
        store: OperationalStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay_s: float = 0.5,
    ) -> None:
        self.settings = settings
        self.store = store
        self._retry_delay_s = retry_delay_s
        self._client = httpx.AsyncClient(
            timeout=settings.delivery_timeout_s,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def dispatch_changes(
        self,
        task: TaskInfo,
        *,
        version: int,
        run_id: str | None,
        changes: list[ChangeEvent],
        records_by_key: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, int]:
        stats = {"sent": 0, "failed": 0, "skipped_dedup": 0, "filtered_out": 0}
        if not changes:
            return stats

        subscriptions = [s for s in self.store.list_subscriptions(task.id) if s.enabled]
        if not subscriptions:
            return stats

        personal = _personal_keys(task)
        safe_records = {
            key: _sanitize_payload(payload, personal)
            for key, payload in (records_by_key or {}).items()
        }

        for sub in subscriptions:
            matched: list[tuple[ChangeEvent, str]] = []
            for change in changes:
                raw_payload = safe_records.get(change.record_key)
                if not match_filters(change, raw_payload, sub.filters):
                    stats["filtered_out"] += 1
                    continue
                dkey = change_dedup_key(version, change)
                if self.store.is_delivered(sub.id, dkey):
                    stats["skipped_dedup"] += 1
                    continue
                matched.append((change, dkey))

            if not matched:
                continue

            safe_changes = [_sanitize_change(ch, personal) for ch, _ in matched]
            if sub.channel == "webhook":
                body_dict: dict[str, Any] = {
                    "event": "delta",
                    "task_id": task.id,
                    "task_title": task.title,
                    "dataset_version": version,
                    "run_id": run_id,
                    "sent_at": iso(),
                    "changes": [
                        {
                            **item,
                            "record": safe_records.get(str(item.get("record_key", "")), {}),
                        }
                        for item in safe_changes
                    ],
                }
                ok, attempts, err = await self._post_webhook(sub, body_dict)
            else:
                text = _format_telegram_delta(task, version, safe_changes, safe_records)
                ok, attempts, err = await self._post_telegram(sub.target, text)

            self._finalize_batch(
                sub=sub,
                run_id=run_id,
                dedup_keys=[dkey for _, dkey in matched],
                count=len(matched),
                ok=ok,
                attempts=attempts,
                error=err,
                task_id=task.id,
                stats=stats,
            )
        return stats

    async def dispatch_alert(
        self,
        task: TaskInfo,
        *,
        run_id: str | None,
        kind: str,
        message: str,
    ) -> dict[str, int]:
        """Send an owner operational alert (failed run or empty dataset) to opted-in channels."""
        stats = {"sent": 0, "failed": 0, "skipped_dedup": 0}
        subscriptions = [
            s
            for s in self.store.list_subscriptions(task.id)
            if s.enabled and s.filters.notify_on_error
        ]
        if not subscriptions:
            return stats

        dedup_key = f"alert:{run_id or 'none'}:{kind}"
        for sub in subscriptions:
            if self.store.is_delivered(sub.id, dedup_key):
                stats["skipped_dedup"] += 1
                continue

            if sub.channel == "webhook":
                payload = {
                    "event": "alert",
                    "alert_kind": kind,
                    "task_id": task.id,
                    "task_title": task.title,
                    "run_id": run_id,
                    "message": message,
                    "sent_at": iso(),
                }
                ok, attempts, err = await self._post_webhook(sub, payload)
            else:
                tg_text = (
                    f"⚠️ <b>Алерт студії: {html.escape(task.title)}</b>\n"
                    f"Тип: <code>{html.escape(kind)}</code>\n"
                    f"{html.escape(message[:1500])}"
                )
                ok, attempts, err = await self._post_telegram(sub.target, tg_text)

            self._finalize_batch(
                sub=sub,
                run_id=run_id,
                dedup_keys=[dedup_key],
                count=1,
                ok=ok,
                attempts=attempts,
                error=err,
                task_id=task.id,
                stats=stats,
            )
        return stats

    async def send_test(self, task: TaskInfo, sub: SubscriptionInfo) -> tuple[bool, str]:
        """Send an immediate test notification for a subscription."""
        if sub.channel == "webhook":
            payload = {
                "event": "test",
                "task_id": task.id,
                "task_title": task.title,
                "sent_at": iso(),
                "message": "Тестовий webhook від DaaS Intelligence Studio",
            }
            ok, _attempts, err = await self._post_webhook(sub, payload)
        else:
            tg_text = (
                f"✅ <b>Тест підписки: {html.escape(task.title)}</b>\n"
                f"Канал працює (<code>{html.escape(sub.name or sub.id)}</code>)."
            )
            ok, _attempts, err = await self._post_telegram(sub.target, tg_text)

        if ok:
            self.store.add_event(
                task.id,
                "delivery_sent",
                f"Тестове сповіщення доставлено ({sub.channel})",
                level="info",
                data={"subscription_id": sub.id, "channel": sub.channel, "test": True},
            )
            return True, "Доставлено"
        self.store.add_event(
            task.id,
            "delivery_failed",
            f"Помилка тестової доставки ({sub.channel}): {err}",
            level="warning",
            data={"subscription_id": sub.id, "channel": sub.channel, "error": err, "test": True},
        )
        return False, err or "Помилка доставки"

    def _finalize_batch(
        self,
        *,
        sub: SubscriptionInfo,
        run_id: str | None,
        dedup_keys: list[str],
        count: int,
        ok: bool,
        attempts: int,
        error: str | None,
        task_id: str,
        stats: dict[str, int],
    ) -> None:
        if ok:
            for dkey in dedup_keys:
                self.store.record_delivery(
                    sub.id, dkey, run_id=run_id, status="sent", attempts=attempts
                )
            stats["sent"] += 1
            self.store.add_event(
                task_id,
                "delivery_sent",
                f"Доставлено {count} сповіщень у {sub.channel} ({sub.name or sub.id})",
                run_id=run_id,
                level="info",
                data={"subscription_id": sub.id, "channel": sub.channel, "count": count},
            )
        else:
            stats["failed"] += 1
            self.store.add_event(
                task_id,
                "delivery_failed",
                f"Не вдалося доставити в {sub.channel} ({sub.name or sub.id}): {error}",
                run_id=run_id,
                level="warning",
                data={
                    "subscription_id": sub.id,
                    "channel": sub.channel,
                    "attempts": attempts,
                    "error": error or "delivery failed",
                },
            )

    async def _post_webhook(
        self, sub: SubscriptionInfo, payload: dict[str, Any]
    ) -> tuple[bool, int, str | None]:
        try:
            assert_public_url(sub.target, allow_private=self.settings.allow_private_network)
        except Exception as exc:  # noqa: BLE001
            return False, 1, _sanitize_error(str(exc), sub.secret)

        import json

        raw_body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if sub.secret:
            sig = hmac.new(sub.secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
            headers["X-Daas-Signature"] = f"sha256={sig}"

        return await self._send_with_retries(sub.target, content=raw_body, headers=headers)

    async def _post_telegram(self, chat_id: str, text: str) -> tuple[bool, int, str | None]:
        token = (self.settings.telegram_bot_token or "").strip()
        if not token:
            return False, 1, "Не налаштовано DAAS_TELEGRAM_BOT_TOKEN"
        import json

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        raw_body = json.dumps(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return await self._send_with_retries(
            url,
            content=raw_body,
            headers={"Content-Type": "application/json"},
            secrets=(token,),
        )

    async def _send_with_retries(
        self,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        secrets: tuple[str, ...] = (),
    ) -> tuple[bool, int, str | None]:
        max_attempts = max(1, self.settings.delivery_max_retries)
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.post(url, content=content, headers=headers)
            except httpx.HTTPError as exc:
                last_error = _sanitize_error(f"{type(exc).__name__}: {exc}", *secrets)
                if attempt < max_attempts and self._retry_delay_s > 0:
                    await asyncio.sleep(self._retry_delay_s * attempt)
                continue

            if 200 <= response.status_code < 300:
                return True, attempt, None

            last_error = _sanitize_error(f"HTTP {response.status_code}", *secrets)
            # Only retry on rate-limit or transient 5xx responses
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < max_attempts and self._retry_delay_s > 0:
                    await asyncio.sleep(self._retry_delay_s * attempt)
                continue
            return False, attempt, last_error

        return False, max_attempts, last_error or "delivery failed"
