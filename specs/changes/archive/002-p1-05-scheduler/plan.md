# 002 — План реалізації: Планувальник запусків за розкладом (`P1-05`)

> Plan описує **ЯК**. Посилається на [`spec.md`](./spec.md); кожна `REQ-*` має тут конкретне
> місце реалізації.

## 1. Підсумок рішення

Реалізуємо вбудований детермінований cron-планувальник `app/pipeline/scheduler.py` усередині
процесу `Studio` (працює на існуючому `asyncio` event loop, стартує та зупиняється в `lifespan`
FastAPI). Розклади зберігаються в новій SQLite-таблиці `schedules` (міграція №5), керуються
через `/api/tasks/{id}/schedule`, `/api/schedules` та `daas schedule ...`.

Зовнішні залежності (`apscheduler` тощо) не додаємо: легкий детермінований cron-парсер (~95 рядків
стандартного Python) виключає ризик несумісності версій, не змінює `pyproject.toml` і дає змогу
тестувати доганяння/пропуски миттєво через `await studio.scheduler.tick(now=...)`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` → `plan.md` → `tasks.md` → тести → код → sync `capabilities/`. |
| II. Детермінованість, LLM лише fallback | ✅ | Розклад і cron-парсер повністю детерміновані; 0 LLM. |
| III. Тести офлайн | ✅ | Інжектований `now` + `FixtureFetcher`; міграція v4→v5 тестується на локальному SQLite. |
| IV. Походження, append-only | ✅ | Планові запуски використовують стандартний `Studio.collect` із версіонуванням і diff. |
| V. Комплаєнс і PII | ✅ | Усі запуски планувальника проходять `_compliance_gate`; `forbidden` і непідтверджений `high` блокуються (`schedule_blocked`). |
| VI. Local-first, захищений API | ✅ | Один процес `daas serve` (один writer DuckDB); API під Bearer-токеном і Host-guard. |
| VII. Події для змін даних | ✅ | Нові події `schedule_updated`, `schedule_deleted`, `schedule_triggered`, `schedule_skipped`, `schedule_blocked` додані в `00-domain-model.md`. |
| VIII. Типізовані межі, міграції | ✅ | `ScheduleInfo` (`models.py`), `ScheduleRequest` (`api/schemas.py`), forward-only міграція SQLite №5 (`schedules`), оновлення `frontend/src/{api,types}.ts`. |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових бібліотек — стандартні `datetime`/`asyncio`. |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Налаштування | `backend/app/config.py`, `.env.example` | `scheduler_enabled: bool = True`, `scheduler_poll_seconds: float = 15.0` | `REQ-RUN-041`, `REQ-OPS-001` |
| Моделі | `backend/app/models.py` | `ScheduleInfo` (стан і параметри розкладу) | `REQ-RUN-040` |
| Сховище | `backend/app/db/sqlite_store.py` | Міграція №5 (`schedules`), методи `get_schedule`, `upsert_schedule`, `delete_schedule`, `list_schedules`, `list_due_schedules`, `mark_schedule_tick` | `REQ-RUN-040`…`042`, `REQ-OPS-010` |
| Планувальник | `backend/app/pipeline/scheduler.py` (новий) | `parse_cron`, `validate_cron`, `next_cron_after`, `TaskScheduler` (`tick`, `start`, `stop`, логіка `catch_up`, пропуск активних запусків і захист від комплаєнс-блокувань) | `REQ-RUN-040`…`042` |
| Runtime / App | `backend/app/runtime.py`, `backend/app/main.py` | `Studio.scheduler`, методи `set_schedule` / `delete_schedule` / `start_scheduler` / `stop_scheduler`, інтеграція в `lifespan` і `capabilities` | `REQ-RUN-040`, `REQ-RUN-041` |
| API | `backend/app/api/schemas.py`, `backend/app/api/tasks.py` | `ScheduleRequest`, `GET/PUT/DELETE /api/tasks/{task_id}/schedule`, `GET /api/schedules` | `REQ-RUN-040` |
| Frontend контракти | `frontend/src/api.ts`, `frontend/src/types.ts` | Типи `ScheduleInfo`, `ApiSourceSpec`, `"api_json"` у `Method`/`extraction`, методи `api.schedule`/`setSchedule`/`deleteSchedule`/`schedules` | `REQ-API-*` (сумісність контракту) |
| CLI | `backend/app/cli.py` | Група команд `daas schedule set | show | list | delete` | `REQ-OPS-030` |

## 4. Дані та міграції

- **SQLite міграція №5** у `MIGRATIONS` (`backend/app/db/sqlite_store.py`):
  ```sql
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
  CREATE INDEX idx_schedules_due ON schedules(enabled, next_run_at);
  ```
- **Сумісність:** БД з `user_version = 4` (і нижче) автоматично оновлюється до `5` у `_migrate()`.
  Додаємо явний тест відкриття БД версії 4.

## 5. Контракти

- **API:**
  - `GET /api/schedules` → `200 {items: ScheduleInfo[]}`
  - `GET /api/tasks/{id}/schedule` → `200 {schedule: ScheduleInfo | null}` (404, якщо задача не знайдена)
  - `PUT /api/tasks/{id}/schedule` (`ScheduleRequest`) → `200 {schedule: ScheduleInfo}` (422 при невалідному cron, 400 при невалідній умові `acknowledge_high_risk` без причини ≥ 10 симв.)
  - `DELETE /api/tasks/{id}/schedule` → `200 {deleted: bool}`
- **Події (`specs/capabilities/00-domain-model.md` §4):**
  - `schedule_updated` (`info`, `runtime`) — розклад створено/оновлено (`data.cron`, `data.enabled`, `data.next_run_at`).
  - `schedule_deleted` (`info`, `runtime`) — розклад видалено.
  - `schedule_triggered` (`info`, `scheduler`) — планувальник запустив збір (`data.run_id`, `data.cron`, `data.catch_up`).
  - `schedule_skipped` (`debug`/`info`, `scheduler`) — слот пропущено (`data.reason`: `active_run` або `missed_no_catchup`).
  - `schedule_blocked` (`warning`, `scheduler`) — запуск заблоковано комплаєнсом або браком калібрування (`data.error`).
- **Налаштування `DAAS_*` (`config.py` + `.env.example`):**
  - `DAAS_SCHEDULER_ENABLED=true`
  - `DAAS_SCHEDULER_POLL_SECONDS=15`

## 6. Ризики і відкат

| Ризик | Як помітимо | Мітигація / відкат |
|:---|:---|:---|
| Постійне зациклення при `forbidden` або помилці задачі | Кожні 15 с пишеться `schedule_blocked` | При `schedule_blocked` або `schedule_skipped` обов'язково пересуваємо `next_run_at = next_cron_after(cron, now)` |
| Шквал запусків після тривалого простою (`catch_up`) | Серія запусків за одну хвилину | Виконувати максимум 1 доганяючий запуск і стрибати `next_run_at` строго вперед (`> now`) |
| `asyncio` гонка між фоновим циклом і `TestClient` закриттям | `Task was destroyed but it is pending` або зависання тесту | Скасування задачі планувальника в `Studio.aclose()` з `suppress(asyncio.CancelledError)`; короткий `wait` та акуратне очікування завершення |
| Непідтримуваний cron-вираз дає нескінченний пошук `next_cron_after` | Зависання при `31 0 31 2 *` (31 лютого) | Обмежуємо горизонт пошуку 4 роками (включно з високосним 29 лютого) і відхиляємо недосяжний день+місяць на валідації (напр., 31 лютого) |

## 7. Тестова стратегія (без мережі)

1. `backend/tests/test_scheduler.py`:
   - `REQ-RUN-040`: валідація cron (`*/15 * * * *`, `0 9 * * 1-5`, `@hourly`, `@daily`), відхилення битого cron (`99 * * * *`, `0 0 31 2 *`) із HTTP 422, CRUD через API + перевірка `schedule_updated`/`schedule_deleted`.
   - `REQ-RUN-041`: `tick(now=...)` запускає API-задачу через `FixtureFetcher`, пересуває `next_run_at`, пише `schedule_triggered`; при активному запуску пропускає слот (`schedule_skipped`); при `tos_risk="forbidden"` блокує (`schedule_blocked`) і пересуває `next_run_at` без зациклення.
   - `REQ-RUN-042`: прострочка на 3 години з `catch_up=True` виконує рівно 1 запуск; з `catch_up=False` пропускає слот (`schedule_skipped`, `reason="missed_no_catchup"`).
   - **Міграція №5:** відкриття БД версії `user_version=4` і перевірка роботи `schedules`.
   - **CLI:** перевірка `daas schedule set/show/list/delete` через `typer.testing.CliRunner`.
