# 013 — Задачі: Розширені фільтри доставки, режим дайджесту та вкладення для Telegram

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-OPS-010` (міграція v9 відкриває БД v8 із підписками) → `backend/tests/test_delivery.py::test_sqlite_migration_v9_opens_v8_database`
- [x] T011 [P] Тест `REQ-DEL-004` (стоп-слова, CPV-префікси, регіони, field_filters, PII-безпека) → `backend/tests/test_delivery.py::test_advanced_delivery_filters_cpv_region_exclude_and_fields`
- [x] T012 [P] Тест `REQ-DEL-005` (режим `digest_daily`, невалідний cron 422, черга через рестарт, `TaskScheduler.tick`, збій доставки, API `/flush`) → `backend/tests/test_delivery.py::test_digest_daily_queue_scheduler_flush_and_failure_safety`
- [x] T013 [P] Тест `REQ-DEL-006` (Telegram-форматування, XLSX `sendDocument` при > ліміту, маскування PII, захист від formula injection, частковий збій `sendDocument`) → `backend/tests/test_delivery.py::test_telegram_formatting_and_xlsx_attachment_over_limit`
- [x] T014 [P] Тест `REQ-OPS-030` (нові прапорці `daas notify add` і `daas notify flush`) → `backend/tests/test_delivery.py::test_cli_notify_advanced_filters_and_flush`

## Фаза 2 — Реалізація
- [x] T020 Налаштування `DAAS_DELIVERY_INLINE_LIMIT` у `backend/app/config.py` і `.env.example`
- [x] T021 Моделі `DeliveryMode`, розширені `DeliveryFilters` і поля дайджесту в `SubscriptionInfo` (`backend/app/models.py`)
- [x] T022 Сховище: SQLite міграція №9 + методи черги дайджестів та `next_digest_at` (`backend/app/db/sqlite_store.py`)
- [x] T023 Пайплайн: розширений `match_filters`, режим `digest_daily`, `flush_subscription`/`flush_due_digests`, покращений Telegram formatter + XLSX `sendDocument` (`backend/app/pipeline/delivery.py`)
- [x] T024 Планувальник і Runtime: інтеграція `flush_due_digests` у `TaskScheduler.tick()`, обчислення `next_digest_at` при CRUD підписок, `Studio.flush_subscription` (`backend/app/pipeline/scheduler.py`, `backend/app/runtime.py`)
- [x] T025 API і схеми: `SubscriptionView`, `POST /api/tasks/{id}/subscriptions/{sub_id}/flush` (`backend/app/api/schemas.py`, `backend/app/api/tasks.py`)
- [x] T026 CLI: прапорці в `daas notify add` і команда `daas notify flush` (`backend/app/cli.py`)
- [x] T027 UI (`REQ-UI-017`): `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/components/AutomationPanel.tsx`

## Фаза 3 — Перевірка
- [x] T030 `make test` · `make lint` · `make build` · `make specs-check` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/` (`00`, `04`, `08`, `11`, `12`, `13`) з посиланнями на тести
- [x] T041 Оновити `docs/06_DEVELOPMENT_AND_MONETIZATION_PLAN.md`
- [x] T042 Перенести `specs/changes/013-delivery-filters-and-digest/` → `specs/changes/archive/013-delivery-filters-and-digest/`
