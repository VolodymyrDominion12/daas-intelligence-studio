# 002 — Задачі: Планувальник запусків за розкладом (`P1-05`)

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати [`spec.md`](./spec.md), [`plan.md`](./plan.md) і зачеплені `specs/capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-RUN-040` (парсер/валідація cron, недосяжні дати типу 31 лютого, API `PUT/GET/DELETE /schedule` і події `schedule_updated`/`schedule_deleted`) → `backend/tests/test_scheduler.py::test_cron_validation_and_schedule_crud_api`
- [x] T011 [P] Тест `REQ-RUN-041` (запуск за розкладом через `tick(now)`, пропуск при активному запуску → `schedule_skipped`, блокування `forbidden` → `schedule_blocked`) → `backend/tests/test_scheduler.py::test_scheduler_triggers_skips_active_and_respects_compliance`
- [x] T012 [P] Тест `REQ-RUN-042` (`catch_up=True` запускає 1 доганяючий прогін і пересуває `next_run_at > now`; `catch_up=False` пропускає прострочений слот) → `backend/tests/test_scheduler.py::test_scheduler_catch_up_and_missed_slot_policy`
- [x] T013 [P] Тест міграції SQLite №5 (відкриття БД із `user_version=4`) → `backend/tests/test_scheduler.py::test_sqlite_migration_v5_opens_v4_database`
- [x] T014 [P] Тест CLI `daas schedule set/show/list/delete` через `CliRunner` → `backend/tests/test_scheduler.py::test_cli_schedule_commands`

## Фаза 2 — Реалізація
- [x] T020 Конфігурація `DAAS_SCHEDULER_ENABLED`, `DAAS_SCHEDULER_POLL_SECONDS` у `backend/app/config.py` і `.env.example`
- [x] T021 Модель `ScheduleInfo` у `backend/app/models.py`
- [x] T022 Сховище: міграція №5 (`schedules`) і CRUD/due-методи у `backend/app/db/sqlite_store.py`
- [x] T023 Модуль `backend/app/pipeline/scheduler.py`: cron-парсер (`parse_cron`, `validate_cron`, `next_cron_after`) і `TaskScheduler` (`tick`, `start`, `stop`)
- [x] T024 Інтеграція планувальника в `backend/app/runtime.py` (`Studio.scheduler`, `set_schedule`, `delete_schedule`, `start_scheduler`, `stop_scheduler`, `capabilities`) і `backend/app/main.py` (`lifespan`)
- [x] T025 API і фронтенд-контракти: `ScheduleRequest`, ендпоінти в `backend/app/api/schemas.py` і `backend/app/api/tasks.py`; типи та методи у `frontend/src/types.ts` і `frontend/src/api.ts`
- [x] T026 CLI: група `daas schedule` у `backend/app/cli.py`

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Оновити `specs/capabilities/` (`00-domain-model.md`, `04-runs-and-collection.md`, `11-api-and-security.md`, `12-storage-cli-ops.md`), додати `pipeline/scheduler.py` у «Код» і нові події в каталог
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (позначити `P1-05 ✅`)
- [x] T042 Перенести `specs/changes/002-p1-05-scheduler/` → `specs/changes/archive/` та оновити `specs/changes/README.md`
