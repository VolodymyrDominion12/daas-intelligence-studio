# 015 — Задачі: Тонкий Telegram-бот підписника

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, `capabilities/{08,11,12}*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-DEL-008` (бот працює поверх REST API студії, ізоляція `chat_id`, обробка відмови API) → `backend/tests/test_subscriber_bot.py::test_bot_uses_studio_rest_api_and_isolates_chat_ids`
- [x] T011 [P] Тест `REQ-DEL-009` (7-денний Free Trial, обмеження `free` на `instant`, `/upgrade` URL) → `backend/tests/test_subscriber_bot.py::test_bot_free_trial_upgrade_and_plan_enforcement`
- [x] T012 [P] Тест `REQ-DEL-010` (self-service фільтри CPV/регіон/бюджет/стоп-слова/режим, `/pause`, `/resume`, `/flush` і невалідні аргументи) → `backend/tests/test_subscriber_bot.py::test_bot_filter_setup_pause_resume_and_flush`
- [x] T013 [P] Тест `REQ-DEL-011` (`/telegram/webhook`, `/billing/webhook` HMAC, `poll_bot_once`, CLI `daas bot grant|status`) → `backend/tests/test_subscriber_bot.py::test_bot_webhooks_polling_and_cli_commands`

## Фаза 2 — Реалізація
- [x] T020 Налаштування `DAAS_BOT_*` у `backend/app/config.py` і `.env.example`
- [x] T021 Моделі та JSON-сховище профілів підписників → `backend/app/bot/state.py`
- [x] T022 REST-клієнт до студії та Telegram-клієнт → `backend/app/bot/client.py`
- [x] T023 Сервіс команд, фільтрів, тріалу та тарифів → `backend/app/bot/service.py`
- [x] T024 HTTP webhook/billing сервіс і long-polling → `backend/app/bot/app.py`
- [x] T025 CLI підгрупа `daas bot serve|status|grant` → `backend/app/cli.py`

## Фаза 3 — Перевірка
- [x] T030 `make specs-check test lint build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{08,11,12}*.md` (з посиланнями на тести та модулями `app/bot/*`)
- [x] T041 Оновити `docs/06_DEVELOPMENT_AND_MONETIZATION_PLAN.md` (§5 SDD `015`)
- [x] T042 Перенести `specs/changes/015-telegram-subscriber-bot/` → `specs/changes/archive/`
