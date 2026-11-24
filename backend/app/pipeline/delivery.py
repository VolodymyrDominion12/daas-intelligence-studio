"""Outbound delta and owner-alert delivery (Webhook & Telegram Bot API, D-01, 013)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from ..config import Settings
from ..models import ChangeEvent, DeliveryFilters, SubscriptionInfo, TaskInfo
from ..storage.exports import _defuse, _xlsx_cell
from ..utils import assert_public_url, iso, parse_number, safe_filename, utcnow
from .scheduler import next_cron_after

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


def _sanitize_change(change: ChangeEvent | dict[str, Any], personal: set[str]) -> dict[str, Any]:
    data = change.model_dump(mode="json") if isinstance(change, ChangeEvent) else dict(change)
    field = data.get("field")
    if field and field in personal:
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


def _as_text_candidates(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _match_value(candidate: str, pattern: str) -> bool:
    cand = candidate.casefold()
    pat = pattern.strip().casefold()
    if not pat:
        return False
    if pat.endswith("*"):
        return cand.startswith(pat[:-1])
    return cand == pat


def match_filters(
    change: ChangeEvent,
    payload: dict[str, Any] | None,
    filters: DeliveryFilters,
) -> bool:
    """Return True if ``change`` matches every active condition in ``filters``."""
    if filters.event_types and change.type not in filters.event_types:
        return False

    haystack = ""
    if filters.keywords or filters.exclude_keywords:
        parts = [
            change.record_key,
            str(change.field or ""),
            str(change.old or ""),
            str(change.new or ""),
        ]
        if payload:
            for value in payload.values():
                if isinstance(value, list):
                    parts.extend(str(item) for item in value if item is not None)
                elif value is not None:
                    parts.append(str(value))
        haystack = " ".join(parts).casefold()

    if filters.keywords and not any(
        kw.strip().casefold() in haystack for kw in filters.keywords if kw.strip()
    ):
        return False

    if filters.exclude_keywords and any(
        kw.strip().casefold() in haystack for kw in filters.exclude_keywords if kw.strip()
    ):
        return False

    if filters.cpv_prefixes:
        raw_cpv = None
        if payload:
            raw_cpv = payload.get("cpv") if payload.get("cpv") is not None else payload.get("cpv_code")
        candidates = [c.casefold() for c in _as_text_candidates(raw_cpv)]
        prefixes = [p.strip().casefold() for p in filters.cpv_prefixes if p.strip()]
        if not candidates or not any(
            cand.startswith(pref) for pref in prefixes for cand in candidates
        ):
            return False

    if filters.regions:
        region_parts: list[str] = []
        if payload:
            region_parts.extend(_as_text_candidates(payload.get("region")))
            region_parts.extend(_as_text_candidates(payload.get("city")))
        region_haystack = " ".join(region_parts).casefold()
        if not region_haystack or not any(
            reg.strip().casefold() in region_haystack for reg in filters.regions if reg.strip()
        ):
            return False

    if filters.field_filters:
        if not payload:
            return False
        for key, allowed in filters.field_filters.items():
            candidates = _as_text_candidates(payload.get(key))
            if not candidates or not any(
                _match_value(cand, pat) for pat in allowed for cand in candidates
            ):
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


def _format_amount_str(amount: Any, currency: Any = None) -> str:
    parsed = parse_number(amount)
    if parsed is not None:
        if parsed.is_integer():
            num_str = f"{int(parsed):,}".replace(",", " ")
        else:
            num_str = f"{parsed:,.2f}".replace(",", " ")
    else:
        num_str = str(amount)
    cur = str(currency or "").strip()
    return f"{num_str} {cur}".strip()


def _format_telegram_delta(
    task: TaskInfo,
    version: int | None,
    changes: list[dict[str, Any]],
    records_by_key: dict[str, dict[str, Any]],
    *,
    digest: bool = False,
    inline_limit: int = 10,
) -> str:
    labels = {
        "new_entity": "🟢 Новий запис",
        "price_drop": "📉 Ціна впала",
        "field_changed": "✏️ Зміна поля",
        "removed_entity": "🔴 Зник",
    }
    if digest:
        lines = [f"📬 <b>Дайджест: {html.escape(task.title)}</b> (змін: {len(changes)})"]
    else:
        ver_label = f"v{version}, " if version is not None else ""
        lines = [f"<b>{html.escape(task.title)}</b> ({ver_label}змін: {len(changes)})"]

    limit = max(1, inline_limit)
    for item in changes[:limit]:
        kind = labels.get(str(item.get("type", "")), str(item.get("type", "")))
        key = str(item.get("record_key", ""))
        rec = records_by_key.get(key) or {}
        headline = str(rec.get("title") or rec.get("name") or key)[:120]
        detail = f"\n{kind}: <b>{html.escape(headline)}</b>"
        amount = rec.get("amount") if rec.get("amount") is not None else rec.get("price")
        if amount is not None:
            detail += f" · {html.escape(_format_amount_str(amount, rec.get('currency')))}"
        region_val = rec.get("region") or rec.get("city")
        if region_val:
            detail += f" · 📍 {html.escape(str(region_val))}"
        cpv_val = rec.get("cpv")
        if cpv_val:
            cpv_text = (
                ", ".join(str(x) for x in cpv_val) if isinstance(cpv_val, list) else str(cpv_val)
            )
            detail += f" · CPV {html.escape(cpv_text)}"
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
    if len(changes) > limit:
        lines.append(
            f"\n📎 Показано {limit} із {len(changes)} змін — повний список у XLSX-вкладенні нижче."
        )
    text = "\n".join(lines)
    return text[:3900]


def _build_changes_xlsx_bytes(
    task: TaskInfo,
    changes: list[dict[str, Any]],
    records_by_key: dict[str, dict[str, Any]],
) -> bytes:
    """Build an in-memory XLSX workbook summarizing a delivery batch with formula-injection & PII protection."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    personal = _personal_keys(task)
    dtypes = {col.key: col.dtype for col in task.spec.columns}

    extra_cols: list[tuple[str, str]] = [(col.key, col.title or col.key) for col in task.spec.columns]
    seen_keys = {key for key, _ in extra_cols}
    for key, label in (("currency", "Валюта"), ("region", "Регіон"), ("cpv", "CPV")):
        if key not in seen_keys and any(
            (records_by_key.get(str(ch.get("record_key", ""))) or {}).get(key) is not None
            for ch in changes
        ):
            extra_cols.append((key, label))
            seen_keys.add(key)

    headers = [
        "Тип події",
        "Ключ запису",
        "Поле",
        "Було",
        "Стало",
        "Дельта %",
        *[title for _, title in extra_cols],
        "Посилання",
    ]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Зміни"
    sheet.append(headers)

    bold = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="1F3A5F")
    for cell in sheet[1]:
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for raw_ch in changes:
        ch = _sanitize_change(raw_ch, personal)
        rec_key = str(ch.get("record_key", ""))
        rec = _sanitize_payload(records_by_key.get(rec_key), personal)

        row_cells: list[Any] = [
            _defuse(str(ch.get("type") or ""), "string"),
            _defuse(rec_key, "string"),
            _defuse(str(ch.get("field") or ""), "string"),
            _xlsx_cell(ch.get("old"), dtypes.get(str(ch.get("field") or ""), "string")),
            _xlsx_cell(ch.get("new"), dtypes.get(str(ch.get("field") or ""), "string")),
            float(ch["delta_pct"]) if ch.get("delta_pct") is not None else None,
        ]
        for key, _ in extra_cols:
            row_cells.append(_xlsx_cell(rec.get(key), dtypes.get(key, "string")))
        row_cells.append(_defuse(str(ch.get("source_url") or ""), "url"))
        sheet.append(row_cells)

    sheet.freeze_panes = "B2"
    if changes:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(changes) + 1}"
    for index, title in enumerate(headers, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = min(max(len(title) + 4, 14), 48)

    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def _sanitize_error(message: str, *secrets: str | None) -> str:
    cleaned = message
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned[:400]


class DeltaDispatcher:
    """Delivers filtered change batches, queued digests and owner alerts to subscribers."""

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
        stats = {"sent": 0, "failed": 0, "skipped_dedup": 0, "filtered_out": 0, "queued": 0}
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

            if sub.filters.delivery_mode == "digest_daily":
                items = [
                    (dkey, safe_ch, safe_records.get(ch.record_key, {}))
                    for (ch, dkey), safe_ch in zip(matched, safe_changes, strict=True)
                ]
                added = self.store.enqueue_digest_items(
                    sub.id, task.id, version, run_id, items=items
                )
                stats["queued"] += added
                if added > 0:
                    self.store.add_event(
                        task.id,
                        "digest_queued",
                        f"Додано {added} змін у чергу дайджесту ({sub.name or sub.id})",
                        run_id=run_id,
                        level="info",
                        data={"subscription_id": sub.id, "count": added, "version": version},
                    )
                continue

            await self._deliver_batch(
                task=task,
                sub=sub,
                event_name="delta",
                version=version,
                run_id=run_id,
                safe_changes=safe_changes,
                safe_records=safe_records,
                dedup_keys=[dkey for _, dkey in matched],
                stats=stats,
            )
        return stats

    async def flush_subscription(
        self,
        task: TaskInfo,
        sub: SubscriptionInfo,
        *,
        run_id: str | None = None,
    ) -> tuple[bool, int, str]:
        """Deliver all pending queued changes for ``sub`` as a single digest batch."""
        pending = self.store.list_pending_digest(sub.id)
        if not pending:
            return True, 0, "Немає очікуючих змін для дайджесту"

        personal = _personal_keys(task)
        already_delivered_ids: list[int] = []
        deliverable_rows: list[dict[str, Any]] = []
        for row in pending:
            if self.store.is_delivered(sub.id, row["dedup_key"]):
                already_delivered_ids.append(row["id"])
            else:
                deliverable_rows.append(row)

        if already_delivered_ids:
            self.store.mark_digest_flushed(sub.id, already_delivered_ids)

        if not deliverable_rows:
            return True, 0, "Усі зміни дайджесту вже доставлено"

        safe_changes = [_sanitize_change(row["change"], personal) for row in deliverable_rows]
        safe_records: dict[str, dict[str, Any]] = {}
        for row in deliverable_rows:
            rec_key = str(row["change"].get("record_key", ""))
            if rec_key:
                safe_records[rec_key] = _sanitize_payload(row["record"], personal)
        dedup_keys = [str(row["dedup_key"]) for row in deliverable_rows]
        queue_ids = [int(row["id"]) for row in deliverable_rows]
        latest_version = max(int(row["dataset_version"]) for row in deliverable_rows)

        stats = {"sent": 0, "failed": 0, "skipped_dedup": 0, "filtered_out": 0, "queued": 0}
        ok, err = await self._deliver_batch(
            task=task,
            sub=sub,
            event_name="digest",
            version=latest_version,
            run_id=run_id,
            safe_changes=safe_changes,
            safe_records=safe_records,
            dedup_keys=dedup_keys,
            stats=stats,
        )
        if ok:
            self.store.mark_digest_flushed(sub.id, queue_ids)
            self.store.advance_subscription_digest(
                sub.id, next_digest_at=sub.next_digest_at, last_digest_at=utcnow()
            )
            return True, len(deliverable_rows), f"Надіслано дайджест ({len(deliverable_rows)} змін)"
        return False, 0, err or "Помилка доставки дайджесту"

    async def flush_due_digests(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Flush all enabled ``digest_daily`` subscriptions whose ``next_digest_at <= now``."""
        current = now or utcnow()
        current = current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)

        outcomes: list[dict[str, Any]] = []
        for sub in self.store.list_due_digest_subscriptions(iso(current)):
            next_slot = next_cron_after(sub.filters.digest_cron, current)
            task = self.store.get_task(sub.task_id)
            if task is None:
                self.store.advance_subscription_digest(sub.id, next_digest_at=next_slot)
                continue

            pending = self.store.list_pending_digest(sub.id)
            if not pending:
                self.store.advance_subscription_digest(sub.id, next_digest_at=next_slot)
                outcomes.append(
                    {
                        "subscription_id": sub.id,
                        "task_id": sub.task_id,
                        "action": "skipped_empty",
                        "next_digest_at": iso(next_slot),
                    }
                )
                continue

            ok, count, detail = await self.flush_subscription(task, sub)
            self.store.advance_subscription_digest(
                sub.id,
                next_digest_at=next_slot,
                last_digest_at=current if ok else None,
            )
            outcomes.append(
                {
                    "subscription_id": sub.id,
                    "task_id": sub.task_id,
                    "action": "flushed" if ok else "failed",
                    "count": count,
                    "detail": detail,
                    "next_digest_at": iso(next_slot),
                }
            )
        return outcomes

    async def _deliver_batch(
        self,
        *,
        task: TaskInfo,
        sub: SubscriptionInfo,
        event_name: str,
        version: int,
        run_id: str | None,
        safe_changes: list[dict[str, Any]],
        safe_records: dict[str, dict[str, Any]],
        dedup_keys: list[str],
        stats: dict[str, int],
    ) -> tuple[bool, str | None]:
        attachment_error: str | None = None
        attachment_attempts = 0
        if sub.channel == "webhook":
            body_dict: dict[str, Any] = {
                "event": event_name,
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
            is_digest = event_name == "digest"
            inline_limit = self.settings.delivery_inline_limit
            text = _format_telegram_delta(
                task,
                version,
                safe_changes,
                safe_records,
                digest=is_digest,
                inline_limit=inline_limit,
            )
            ok, attempts, err = await self._post_telegram(sub.target, text)
            if ok and len(safe_changes) > inline_limit:
                xlsx_bytes = _build_changes_xlsx_bytes(task, safe_changes, safe_records)
                filename = f"{safe_filename(task.title or task.id)}-changes-{len(safe_changes)}.xlsx"
                caption = f"{task.title[:120]} — {len(safe_changes)} змін"
                doc_ok, attachment_attempts, attachment_error = await self._post_telegram_document(
                    sub.target,
                    filename=filename,
                    content_bytes=xlsx_bytes,
                    caption=caption,
                )
                if doc_ok:
                    attachment_error = None

        self._finalize_batch(
            sub=sub,
            run_id=run_id,
            dedup_keys=dedup_keys,
            count=len(safe_changes),
            ok=ok,
            attempts=attempts,
            error=err,
            task_id=task.id,
            stats=stats,
        )
        if ok and attachment_error is not None:
            self.store.add_event(
                task.id,
                "delivery_failed",
                f"Текст у Telegram доставлено, але не вдалося надіслати XLSX-вкладення ({sub.name or sub.id}): {attachment_error}",
                run_id=run_id,
                level="warning",
                data={
                    "subscription_id": sub.id,
                    "channel": sub.channel,
                    "attachment": True,
                    "attempts": attachment_attempts,
                    "error": attachment_error,
                },
            )
        return ok, err

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

    async def _post_telegram_document(
        self,
        chat_id: str,
        *,
        filename: str,
        content_bytes: bytes,
        caption: str = "",
    ) -> tuple[bool, int, str | None]:
        token = (self.settings.telegram_bot_token or "").strip()
        if not token:
            return False, 1, "Не налаштовано DAAS_TELEGRAM_BOT_TOKEN"

        url = f"https://api.telegram.org/bot{token}/sendDocument"
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption[:1024]
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return await self._send_with_retries(
            url,
            data=data,
            files={"document": (filename, content_bytes, mime)},
            secrets=(token,),
        )

    async def _send_with_retries(
        self,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> tuple[bool, int, str | None]:
        max_attempts = max(1, self.settings.delivery_max_retries)
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                if files is not None:
                    response = await self._client.post(url, data=data, files=files)
                else:
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
