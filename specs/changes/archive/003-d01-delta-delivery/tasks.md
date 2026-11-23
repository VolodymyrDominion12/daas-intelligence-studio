# 003 — Задачі: Канали доставки дельти та алертів (`D-01`)

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати [`spec.md`](./spec.md), [`plan.md`](./plan.md) і зачеплені `specs/capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-DEL-001` (CRUD підписок API, маскування `secret`, SSRF і logged-out валідація webhook, події `subscription_*`) → `backend/tests/test_delivery.py::test_subscription_crud_ssrf_and_secret_masking`
- [x] T011 [P] Тест `REQ-DEL-002` (фільтрація `event_types`/`keywords`/`min_amount`/`max_amount`, HMAC-підпис webhook, HTML-формат Telegram, безумовне маскування `pii="personal"`) → `backend/tests/test_delivery.py::test_delta_filtering_pii_redaction_and_hmac`
- [x] T012 [P] Тест `REQ-DEL-003` (ретраї при `503`, дедуплікація в `delivery_log`, санітування помилок без витоку токена, алерт власнику на `run_failed` та 0 записів) → `backend/tests/test_delivery.py::test_delivery_retries_dedup_and_owner_error_alerts`
- [x] T013 [P] Тест міграції SQLite №6 (відкриття БД із `user_version=5`) → `backend/tests/test_delivery.py::test_sqlite_migration_v6_opens_v5_database`
- [x] T014 [P] Тест CLI `daas notify add|list|test|delete` через `CliRunner` → `backend/tests/test_delivery.py::test_cli_notify_commands`

## Фаза 2 — Реалізація
- [x] T020 Налаштування `DAAS_TELEGRAM_BOT_TOKEN`, `DAAS_DELIVERY_TIMEOUT_S`, `DAAS_DELIVERY_MAX_RETRIES` у `backend/app/config.py` і `.env.example`
- [x] T021 Моделі `DeliveryFilters` та `SubscriptionInfo` у `backend/app/models.py`
- [x] T022 Сховище: міграція №6 (`subscriptions`, `delivery_log`) і CRUD/dedup методи у `backend/app/db/sqlite_store.py`
- [x] T023 Модуль `backend/app/pipeline/delivery.py` (`DeltaDispatcher`, фільтрація, маскування `personal` PII, HMAC, Telegram HTML, ретраї, owner-алерти)
- [x] T024 Інтеграція диспетчера в `backend/app/runtime.py` та `backend/app/pipeline/runner.py` (доставка після `_finish_dataset` і алерти в `_fail`)
- [x] T025 API і фронтенд-контракти: схеми та роути `/api/tasks/{id}/subscriptions*` у `backend/app/api/schemas.py`, `backend/app/api/tasks.py`; типи та методи у `frontend/src/types.ts`, `frontend/src/api.ts`
- [x] T026 CLI: група `daas notify` у `backend/app/cli.py`

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхроніція
- [x] T040 Оновити `specs/capabilities/` (`00`, `04`, `08`, `11`, `12`) і каталог подій (`subscription_*`, `delivery_*`)
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (позначити `D-01 ✅`)
- [x] T042 Перенести `specs/changes/003-d01-delta-delivery/` → `specs/changes/archive/` та оновити `specs/changes/README.md`
