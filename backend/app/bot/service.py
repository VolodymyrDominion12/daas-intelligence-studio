"""Deterministic Telegram subscriber bot command/callback service (SDD 015)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .client import StudioApiClient, StudioApiError, TelegramBotClient
from .state import BotSubscriberStore, SubscriberProfile

_CLEAR_TOKENS = {"all", "*", "clear", "none", "-", "скинути", "всі", "усі"}
_CPV_RE = re.compile(r"^\d{2,8}$")


class _BotUserError(ValueError):
    """Expected user-facing validation or state message."""


def _build_payment_url(template: str | None, chat_id: str) -> str | None:
    raw = (template or "").strip()
    if not raw:
        return None
    if "{chat_id}" in raw:
        candidate = raw.replace("{chat_id}", chat_id)
    else:
        parsed = urlparse(raw)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["chat_id"] = chat_id
        candidate = urlunparse(parsed._replace(query=urlencode(query)))
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return candidate


def _split_items(arg: str, *, allow_space: bool = False) -> list[str]:
    pattern = r"[,;\s]+" if allow_space else r"[,;]+"
    cleaned: list[str] = []
    for part in re.split(pattern, arg.strip()):
        token = part.strip()
        if token and token not in cleaned:
            cleaned.append(token)
    return cleaned


def _parse_budget(arg: str) -> tuple[float | None, float | None]:
    raw = arg.strip().lower().replace(" ", "")
    if not raw:
        raise _BotUserError(
            "Вкажіть бюджет. Приклад: /budget 500000 або /budget 500000-2000000 або /budget 0"
        )
    if raw in {"0", *_CLEAR_TOKENS}:
        return None, None
    if "-" in raw:
        parts = raw.split("-", 1)
        try:
            low = float(parts[0])
            high = float(parts[1])
        except ValueError as exc:
            raise _BotUserError(
                "Некоректний формат бюджету. Використовуйте числа, наприклад: /budget 500000-2000000"
            ) from exc
        if low < 0 or high < 0 or low > high:
            raise _BotUserError(
                "Некоректний діапазон бюджету: мінімум має бути >= 0 і <= максимуму."
            )
        return low, high
    try:
        amount = float(raw)
    except ValueError as exc:
        raise _BotUserError(
            "Некоректний формат бюджету. Використовуйте число, наприклад: /budget 500000 або /budget 0"
        ) from exc
    if amount < 0:
        raise _BotUserError("Бюджет не може бути від'ємним.")
    return amount, None


def _is_own_telegram_sub(sub: dict[str, Any], chat_id: str) -> bool:
    return sub.get("channel") == "telegram" and str(sub.get("target") or "") == str(chat_id)


def _format_filters(flt: dict[str, Any]) -> str:
    parts: list[str] = []
    cpv = flt.get("cpv_prefixes") or []
    regions = flt.get("regions") or []
    keywords = flt.get("keywords") or []
    excludes = flt.get("exclude_keywords") or []
    min_amt = flt.get("min_amount")
    max_amt = flt.get("max_amount")
    if cpv:
        parts.append(f"CPV: {', '.join(cpv)}")
    if regions:
        parts.append(f"Регіон: {', '.join(regions)}")
    if min_amt is not None or max_amt is not None:
        parts.append(f"Бюджет: {min_amt or 0}..{max_amt if max_amt is not None else '∞'}")
    if keywords:
        parts.append(f"Ключі: {', '.join(keywords)}")
    if excludes:
        parts.append(f"Стоп-слова: {', '.join(excludes)}")
    return "; ".join(parts) if parts else "без додаткових фільтрів"


class SubscriberBotService:
    """Routes Telegram updates to Studio REST API while isolating each chat_id."""

    def __init__(
        self,
        studio: StudioApiClient,
        store: BotSubscriberStore,
        telegram: TelegramBotClient | None = None,
        *,
        payment_url: str | None = None,
        default_digest_cron: str = "0 6 * * *",
    ) -> None:
        self.studio = studio
        self.store = store
        self.telegram = telegram
        self.payment_url = payment_url
        self.default_digest_cron = default_digest_cron

    async def handle_update(self, update: dict[str, Any]) -> bool:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            cb_id = str(callback.get("id") or "")
            msg = callback.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = chat.get("id")
            data = str(callback.get("data") or "").strip()
            if chat_id is None:
                return False
            cmd_text = self._callback_to_command(data)
            reply_text, markup = await self.handle_command(str(chat_id), cmd_text)
            if self.telegram is not None:
                if cb_id:
                    await self.telegram.answer_callback_query(cb_id)
                await self.telegram.send_message(
                    str(chat_id), reply_text, reply_markup=markup
                )
            return True

        message = update.get("message")
        if isinstance(message, dict):
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            text = str(message.get("text") or "").strip()
            if chat_id is None or not text:
                return False
            reply_text, markup = await self.handle_command(str(chat_id), text)
            if self.telegram is not None:
                await self.telegram.send_message(
                    str(chat_id), reply_text, reply_markup=markup
                )
            return True

        return False

    @staticmethod
    def _callback_to_command(data: str) -> str:
        if data.startswith("select:"):
            return f"/select {data.split(':', 1)[1]}"
        if data == "mode:instant":
            return "/mode instant"
        if data == "mode:digest":
            return "/mode digest"
        if data in {"pause", "resume", "flush", "status", "upgrade", "tasks"}:
            return f"/{data}"
        return "/help"

    async def handle_command(
        self, chat_id: str, text: str
    ) -> tuple[str, dict[str, Any] | None]:
        raw = text.strip()
        if not raw.startswith("/"):
            return self._help_text(), None

        parts = raw.split(None, 1)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        try:
            if cmd == "/start":
                return await self._cmd_start(chat_id)
            if cmd == "/help":
                return self._help_text(), None
            if cmd == "/tasks":
                return await self._cmd_tasks(chat_id)
            if cmd == "/select":
                return await self._cmd_select(chat_id, arg)
            if cmd == "/cpv":
                return await self._cmd_cpv(chat_id, arg)
            if cmd == "/region":
                return await self._cmd_region(chat_id, arg)
            if cmd == "/budget":
                return await self._cmd_budget(chat_id, arg)
            if cmd == "/keywords":
                return await self._cmd_keywords(chat_id, arg)
            if cmd == "/exclude":
                return await self._cmd_exclude(chat_id, arg)
            if cmd == "/mode":
                return await self._cmd_mode(chat_id, arg)
            if cmd == "/status":
                return await self._cmd_status(chat_id)
            if cmd == "/pause":
                return await self._cmd_toggle(chat_id, enabled=False)
            if cmd == "/resume":
                return await self._cmd_toggle(chat_id, enabled=True)
            if cmd == "/flush":
                return await self._cmd_flush(chat_id)
            if cmd == "/upgrade":
                return self._cmd_upgrade(chat_id), None
            return (
                f"Невідома команда {cmd}. Надішліть /help для переліку команд.",
                None,
            )
        except _BotUserError as exc:
            return f"⚠️ {exc}", None
        except StudioApiError as exc:
            return f"⚠️ {exc}", None

    def _help_text(self) -> str:
        return (
            "🤖 Бот моніторингу DaaS Intelligence Studio\n\n"
            "Основні команди:\n"
            "/start — запуск і 7-денний Free Trial\n"
            "/tasks — список потоків моніторингу\n"
            "/select <номер|id> — обрати потік\n"
            "/cpv <коди|all> — фільтр за CPV (напр. 4531, 4500)\n"
            "/region <міста/області|all> — фільтр регіону\n"
            "/budget <мін[-макс]|0> — поріг бюджету в грн\n"
            "/keywords <слова|clear> — обов'язкові слова\n"
            "/exclude <слова|clear> — стоп-слова\n"
            "/mode <instant|digest> — миттєві алерти чи щоденний дайджест\n"
            "/status — поточний тариф і підписки\n"
            "/pause / /resume — пауза та відновлення\n"
            "/flush — надіслати накопичений дайджест зараз\n"
            "/upgrade — тарифи та оплата"
        )

    def _plan_line(self, profile: SubscriberProfile) -> str:
        eff = self.store.effective_plan(profile)
        if eff == "trial" and profile.trial_ends_at:
            return f"Trial (тріал активний до {profile.trial_ends_at.date().isoformat()})"
        if eff in {"pro", "b2b"}:
            until = (
                f" до {profile.plan_expires_at.date().isoformat()}"
                if profile.plan_expires_at
                else ""
            )
            return f"{eff.upper()}{until}"
        return "Free (лише щоденний дайджест)"

    @staticmethod
    def _tasks_markup(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not tasks:
            return None
        rows = [
            [
                {
                    "text": f"{idx}. {t.get('title') or t.get('id')}",
                    "callback_data": f"select:{t.get('id')}",
                }
            ]
            for idx, t in enumerate(tasks[:8], start=1)
        ]
        return {"inline_keyboard": rows}

    async def _cmd_start(self, chat_id: str) -> tuple[str, dict[str, Any] | None]:
        profile, created = self.store.ensure_profile(chat_id)
        tasks = await self.studio.list_tasks()
        eff = self.store.effective_plan(profile)
        intro = (
            f"Вітаємо! Активовано {self.store.trial_days}-денний Free Trial (тариф: {eff})."
            if created and eff == "trial"
            else f"З поверненням! Ваш поточний тариф: {self._plan_line(profile)}."
        )
        if not tasks:
            return (
                f"{intro}\n\nНаразі у студії ще немає доступних потоків моніторингу.",
                None,
            )
        lines = [intro, "", "Оберіть потік кнопкою або через /select <номер>:"]
        for idx, task in enumerate(tasks[:10], start=1):
            lines.append(f"{idx}. {task.get('title') or task.get('id')} ({task.get('id')})")
        lines.append(
            "\nДалі налаштуйте фільтри: /cpv 4531, /region Київ, /budget 500000, /exclude канцтовари, /mode instant"
        )
        return "\n".join(lines), self._tasks_markup(tasks)

    async def _cmd_tasks(self, chat_id: str) -> tuple[str, dict[str, Any] | None]:
        self.store.ensure_profile(chat_id)
        tasks = await self.studio.list_tasks()
        if not tasks:
            return "У студії поки немає потоків моніторингу.", None
        lines = ["Доступні потоки моніторингу:"]
        for idx, task in enumerate(tasks[:10], start=1):
            lines.append(f"{idx}. {task.get('title') or task.get('id')} ({task.get('id')})")
        lines.append("\nВикористайте /select <номер або id>.")
        return "\n".join(lines), self._tasks_markup(tasks)

    async def _cmd_select(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        if not arg:
            raise _BotUserError("Вкажіть номер або ID задачі. Приклад: /select 1")
        tasks = await self.studio.list_tasks()
        chosen: dict[str, Any] | None = None
        if arg.isdigit():
            idx = int(arg)
            if 1 <= idx <= len(tasks):
                chosen = tasks[idx - 1]
        if chosen is None:
            for task in tasks:
                if str(task.get("id") or "") == arg:
                    chosen = task
                    break
        if chosen is None and len(arg) >= 4:
            matches = [t for t in tasks if str(t.get("id") or "").startswith(arg)]
            if len(matches) == 1:
                chosen = matches[0]
        if chosen is None:
            raise _BotUserError(f"Задачу «{arg}» не знайдено. Перегляньте список через /tasks.")

        task_id = str(chosen["id"])
        self.store.set_active_task(chat_id, task_id)
        title = str(chosen.get("title") or task_id)
        return (
            f"✅ Обрано потік: {title} ({task_id}).\n"
            "Тепер задайте фільтри: /cpv, /region, /budget, /keywords, /exclude, /mode instant|digest",
            None,
        )

    async def _upsert_filters(
        self,
        chat_id: str,
        updater: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        profile, _ = self.store.ensure_profile(chat_id)
        task_id = profile.active_task_id
        if not task_id:
            raise _BotUserError(
                "Спочатку оберіть задачу моніторингу: /tasks або /select <номер|id>."
            )

        subs = await self.studio.list_subscriptions(task_id)
        own = [s for s in subs if _is_own_telegram_sub(s, chat_id)]
        if own:
            current = own[0]
            filters = dict(current.get("filters") or {})
            updated_filters = updater(filters)
            return await self.studio.update_subscription(
                task_id, str(current["id"]), filters=updated_filters
            )

        default_mode = "instant" if self.store.can_use_instant(chat_id) else "digest_daily"
        base_filters: dict[str, Any] = {
            "delivery_mode": default_mode,
            "digest_cron": self.default_digest_cron,
        }
        updated_filters = updater(base_filters)
        return await self.studio.create_subscription(
            task_id,
            name=f"tg-{chat_id}",
            channel="telegram",
            target=str(chat_id),
            filters=updated_filters,
            enabled=True,
        )

    async def _cmd_cpv(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        if not arg:
            raise _BotUserError("Вкажіть CPV-префікси або all. Приклад: /cpv 4531, 4500")
        if arg.strip().lower() in _CLEAR_TOKENS:
            prefixes: list[str] = []
        else:
            prefixes = _split_items(arg, allow_space=True)
            invalid = [p for p in prefixes if not _CPV_RE.fullmatch(p)]
            if invalid:
                raise _BotUserError(
                    f"Некоректні CPV-коди: {', '.join(invalid)}. Очікуються 2-8 цифр (напр. 4531)."
                )
        sub = await self._upsert_filters(chat_id, lambda f: {**f, "cpv_prefixes": prefixes})
        val = ", ".join(prefixes) if prefixes else "усі категорії"
        return f"✅ CPV-фільтр оновлено ({sub.get('id')}): {val}", None

    async def _cmd_region(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        if not arg:
            raise _BotUserError("Вкажіть регіони через кому або all. Приклад: /region Київ, Львів")
        regions = [] if arg.strip().lower() in _CLEAR_TOKENS else _split_items(arg)
        sub = await self._upsert_filters(chat_id, lambda f: {**f, "regions": regions})
        val = ", ".join(regions) if regions else "вся Україна"
        return f"✅ Фільтр регіонів оновлено ({sub.get('id')}): {val}", None

    async def _cmd_budget(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        min_amt, max_amt = _parse_budget(arg)
        sub = await self._upsert_filters(
            chat_id, lambda f: {**f, "min_amount": min_amt, "max_amount": max_amt}
        )
        if min_amt is None and max_amt is None:
            return f"✅ Фільтр бюджету скинуто ({sub.get('id')}).", None
        span = f"{min_amt or 0:g}–{max_amt:g}" if max_amt is not None else f"від {min_amt:g}"
        return f"✅ Фільтр бюджету оновлено ({sub.get('id')}): {span} грн", None

    async def _cmd_keywords(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        if not arg:
            raise _BotUserError("Вкажіть ключові слова через кому або clear. Приклад: /keywords сервер, мережа")
        words = [] if arg.strip().lower() in _CLEAR_TOKENS else _split_items(arg)
        sub = await self._upsert_filters(chat_id, lambda f: {**f, "keywords": words})
        val = ", ".join(words) if words else "очищено"
        return f"✅ Ключові слова оновлено ({sub.get('id')}): {val}", None

    async def _cmd_exclude(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        if not arg:
            raise _BotUserError("Вкажіть стоп-слова через кому або clear. Приклад: /exclude канцтовари, папір")
        words = [] if arg.strip().lower() in _CLEAR_TOKENS else _split_items(arg)
        sub = await self._upsert_filters(chat_id, lambda f: {**f, "exclude_keywords": words})
        val = ", ".join(words) if words else "очищено"
        return f"✅ Стоп-слова оновлено ({sub.get('id')}): {val}", None

    async def _cmd_mode(self, chat_id: str, arg: str) -> tuple[str, dict[str, Any] | None]:
        self.store.ensure_profile(chat_id)
        mode_raw = arg.strip().lower()
        if mode_raw == "instant":
            target_mode = "instant"
        elif mode_raw in {"digest", "digest_daily"}:
            target_mode = "digest_daily"
        else:
            raise _BotUserError("Доступні режими: /mode instant або /mode digest")

        if target_mode == "instant" and not self.store.can_use_instant(chat_id):
            raise _BotUserError(
                "Режим instant доступний на планах Trial / Pro / B2B. "
                "Ваш поточний план Free обмежений щоденним дайджестом. Перейдіть на /upgrade."
            )

        sub = await self._upsert_filters(
            chat_id, lambda f: {**f, "delivery_mode": target_mode}
        )
        label = "миттєві сповіщення (instant)" if target_mode == "instant" else "щоденний дайджест (digest_daily)"
        return f"✅ Режим доставки оновлено ({sub.get('id')}): {label}", None

    async def _collect_own_subscriptions(
        self, chat_id: str, active_task_id: str | None
    ) -> list[tuple[str, dict[str, Any]]]:
        if active_task_id:
            subs = await self.studio.list_subscriptions(active_task_id)
            return [(active_task_id, s) for s in subs if _is_own_telegram_sub(s, chat_id)]
        tasks = await self.studio.list_tasks()
        collected: list[tuple[str, dict[str, Any]]] = []
        for task in tasks:
            tid = str(task.get("id") or "")
            if not tid:
                continue
            subs = await self.studio.list_subscriptions(tid)
            for s in subs:
                if _is_own_telegram_sub(s, chat_id):
                    collected.append((tid, s))
        return collected

    async def _cmd_status(self, chat_id: str) -> tuple[str, dict[str, Any] | None]:
        profile, _ = self.store.ensure_profile(chat_id)
        own_subs = await self._collect_own_subscriptions(chat_id, profile.active_task_id)
        lines = [
            f"📊 Статус підписника {chat_id}",
            f"Тариф: {self._plan_line(profile)}",
            f"Активний потік: {profile.active_task_id or 'не обрано (/tasks)'}",
        ]
        if not own_subs:
            lines.append("\nПідписок для цього чату ще немає.")
        else:
            lines.append(f"\nВаші підписки ({len(own_subs)}):")
            for tid, sub in own_subs:
                state = "активна" if sub.get("enabled") else "на паузі"
                flt = dict(sub.get("filters") or {})
                mode = flt.get("delivery_mode") or "instant"
                pending = sub.get("pending_digest_count") or 0
                lines.append(
                    f"• [{tid}] {sub.get('id')} — {state}, режим={mode}, черга={pending}\n"
                    f"  {_format_filters(flt)}"
                )
        return "\n".join(lines), None

    async def _cmd_toggle(
        self, chat_id: str, *, enabled: bool
    ) -> tuple[str, dict[str, Any] | None]:
        profile, _ = self.store.ensure_profile(chat_id)
        own_subs = await self._collect_own_subscriptions(chat_id, profile.active_task_id)
        if not own_subs:
            raise _BotUserError("У вас ще немає налаштованих підписок.")
        changed = 0
        for tid, sub in own_subs:
            await self.studio.update_subscription(tid, str(sub["id"]), enabled=enabled)
            changed += 1
        verb = "відновлено" if enabled else "призупинено"
        return f"✅ Підписки {verb}: {changed} шт.", None

    async def _cmd_flush(self, chat_id: str) -> tuple[str, dict[str, Any] | None]:
        profile, _ = self.store.ensure_profile(chat_id)
        own_subs = await self._collect_own_subscriptions(chat_id, profile.active_task_id)
        if not own_subs:
            raise _BotUserError("У вас ще немає налаштованих підписок для дайджесту.")
        total_items = 0
        details: list[str] = []
        for tid, sub in own_subs:
            res = await self.studio.flush_subscription(tid, str(sub["id"]))
            total_items += int(res.get("count") or 0)
            if res.get("detail"):
                details.append(str(res["detail"]))
        summary = f"📬 Дайджест оброблено (позицій: {total_items})."
        if details:
            summary += "\n" + "; ".join(details)
        return summary, None

    def _cmd_upgrade(self, chat_id: str) -> str:
        profile, _ = self.store.ensure_profile(chat_id)
        eff = self.store.effective_plan(profile)
        link = _build_payment_url(self.payment_url, str(chat_id))
        lines = [
            f"💳 Тарифні плани (поточний: {eff.upper()})",
            "• Free — щоденний дайджест",
            f"• Trial ({self.store.trial_days} днів) — повний доступ до фільтрів та instant-алертів",
            "• Pro — миттєві сповіщення 24/7, CPV/регіони/бюджет/стоп-слова",
            "• B2B — командний моніторинг, переможці Prozorro та пріоритетна доставка",
        ]
        if link:
            lines.append(f"\nПосилання на оплату:\n{link}")
        else:
            lines.append(
                "\nДля активації тарифу зверніться до оператора "
                f"(ID чату: {chat_id})."
            )
        return "\n".join(lines)
