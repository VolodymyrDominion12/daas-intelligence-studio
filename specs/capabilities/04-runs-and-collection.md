# 04 — Запуски, стійка черга, масовий збір

| | |
|:---|:---|
| **Код** | `backend/app/runtime.py` (`Studio`), `backend/app/pipeline/runner.py` (`PipelineRunner.collect`, `_process_job*`, `_process_api_list`, `_process_api_detail`, `_extract_page`, `_expand`, `_heal`, `_finish_dataset`), `backend/app/pipeline/scheduler.py` (`TaskScheduler`, `parse_cron`, `validate_cron`, `next_cron_after`), `backend/app/db/sqlite_store.py` (`runs`, `jobs`, `events`, `llm_calls`, `api_state`, `schedules`) |
| **API** | `POST /api/tasks/{id}/run`, `GET /api/tasks/{id}/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/cancel`, `GET /api/tasks/{id}/events[?after]`, `GET /api/tasks/{id}/events/stream`, `GET/PUT/DELETE /api/tasks/{id}/schedule`, `GET /api/schedules` |
| **CLI** | `daas collect <id> [--pages N] [--ack-high-risk --reason]`, `daas schedule set\|show\|list\|delete` |
| **Тести** | `tests/test_storage.py`, `tests/test_tombstones.py`, `tests/test_api_flow.py`, `tests/test_compliance.py`, `tests/test_open_api_source.py`, `tests/test_scheduler.py`, `tests/test_delivery.py`, `tests/test_proxy_resilience.py`, `tests/test_js_sidecar.py` |
| **Джерела** | ADR-02, `docs/03` §2, `docs/03` §3.3, `docs/03` §3.4 |
| **Статус** | as-built (P1-07) |

## 1. Призначення
Запуск (`calibrate/collect/enrich/snapshot`) — фонова asyncio-задача в процесі студії. Збір іде
через стійку чергу в SQLite з lease/heartbeat/retry; результат — нова версія датасету.

## 2. Вимоги — життєвий цикл запуску

### REQ-RUN-001 — Один активний запуск на задачу
Створення запуску з `exclusive=true` **ПОВИННО** перевіряти відсутність `queued/running`-запуску і
вставляти запуск **в одній транзакції** `BEGIN IMMEDIATE` (разом з audit-подією, якщо є). Конфлікт
→ `ActiveRunError` → HTTP 409. `force=true` знімає перевірку (лише calibrate/collect).
- **Перевірка:** `tests/test_compliance.py::test_exclusive_run_creation_is_atomic_with_audit`,
  `tests/test_api_flow.py::test_full_pipeline`

### REQ-RUN-002 — Фонове виконання і обробка винятків
Запуск **ПОВИНЕН** виконуватися як фонова задача всередині `run_scope(task_id, run_id,
acknowledged)`. Виняток → запуск `failed` з повідомленням (≤ 500 симв.) і подія `run_failed`;
`CancelledError` → `cancelled`.
- **Перевірка:** `tests/test_tombstones.py::test_failed_finish_rolls_back_removals`

### REQ-RUN-003 — Скасування
`POST /runs/{id}/cancel` **ПОВИНЕН** для `queued/running`: позначити запуск `cancelled`,
скасувати його задачі в черзі, записати `run_cancelled`, повернути задачу в `ready` і скасувати
asyncio-задачу. Невідомий запуск → 404.
- **Перевірка:** `tests/test_storage.py::test_queue_lease_retry_and_cancel` (рівень сховища);
  API — прогалина

### REQ-RUN-004 — Відновлення після перезапуску
На старті процесу всі `queued/running` запуски **ПОВИННІ** ставати `failed` («interrupted by
process restart») з подією `run_interrupted`; їхні задачі в черзі скасовуються, а tombstones/
промахи відкочуються (REQ-HIST-026).
- **Перевірка:** `tests/test_storage.py::test_queue_lease_retry_and_cancel`
  (`list_unfinished_runs`); сам `interrupt_stale_runs` — прогалина

## 3. Вимоги — черга

### REQ-RUN-010 — Lease, спроби, retry
`claim_jobs` **ПОВИНЕН** атомарно переводити `queued → leased` з `lease_until`, збільшувати
`attempts` і брати задачі **лише свого `run_id`**. `fail_job(retryable=True)` повертає в `queued`,
доки `attempts < max_attempts` (3), інакше `failed`; `retryable=False` → одразу `failed`.
`UNIQUE(run_id, url)` — URL не дублюється в межах запуску.
- **Перевірка:** `tests/test_storage.py::test_queue_lease_retry_and_cancel`,
  `tests/test_tombstones.py::test_claim_jobs_is_scoped_to_the_run`

### REQ-RUN-011 — Прострочені lease повертаються
Кожен тік воркера **ПОВИНЕН** викликати `release_stale_jobs` (lease прострочено → `queued`) і
`heartbeat` для своїх lease.
- **Перевірка:** — (прогалина)

## 4. Вимоги — збір

### REQ-RUN-020 — Старт збору
Збір **ПОВИНЕН** взяти наступний номер версії датасету, поставити seed-и в чергу з глибиною 0
(лише якщо черга запуску порожня) і записати `run_started`. Без seed-ів → помилка.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`

### REQ-RUN-021 — Цикл і ліміти
Цикл **ПОВИНЕН** брати до `min(6, залишок)` задач, обробляти їх паралельно і зупинятися, коли:
запуск скасовано/провалено; оброблено `limits.max_pages` (подія `limit_reached`); черга порожня;
200 холостих тіків (`queue stalled`). Після кожної пачки — оновлення `stats` і `spent_usd`.
Вичерпання `DAAS_MAX_LLM_CALLS_PER_RUN` пише `llm_limit` і вимикає LLM (збір триває).
- **Перевірка:** `tests/test_tombstones.py::test_page_limit_without_remaining_work_is_a_full_crawl`

### REQ-RUN-022 — Таксономія відмов завантаження
Результат завантаження **ПОВИНЕН** оброблятися так:
`robots` → задача `skipped` + `robots_blocked`; `policy` → `skipped` (подію вже записав guard);
`transient` → retry через `fail_job(retryable=True)`; інше (`blocked`, `permanent`, `unsafe`,
`empty`) → `failed` без retry; подія `fetch_failed` з `data.kind`. Виняток у обробці сторінки →
`job_failed`, задача `failed`, запуск триває («одна погана сторінка не вбиває запуск»).
- **Перевірка:** `tests/test_compliance.py::test_forbidden_website_is_skipped_during_collection`
  (`policy`); інші гілки — прогалина

### REQ-RUN-023 — Вибір схеми і самовідновлення
Для сторінки рушій **ПОВИНЕН** брати схему з `selector_cache(домен, auto:<entity>)`, інакше —
явні селектори зі spec, інакше (не офлайн) — перевчити на цій сторінці. Якщо схема є, а записів 0,
домен ще не «лікувався» в цьому запуску — позначити промах, перевчити схему і повторити екстракцію
без LLM (подія `selector_healed`).
- **Перевірка:** — (прогалина)

### REQ-RUN-024 — LLM у зборі лише за умов
LLM у зборі дозволено **лише** якщо `spec.extraction ∈ {auto, llm}`, роутер доступний, ліміт
викликів не вичерпано, бюджет дозволяє ще $0.001 і режим не офлайн.
- **Перевірка:** `tests/test_extraction.py::test_engine_without_llm_and_without_data_returns_empty`
  (частково)

### REQ-RUN-025 — Розширення фронтиру
Після сторінки рушій **ПОВИНЕН** додавати same-site посилання (≤ 10 для порожньої сторінки, ≤ 5
для сторінки з одним записом; сторінки з > 1 записом — це список, їх не розширюємо), лише якщо
`depth < limits.max_depth`, не виходячи за `max_pages` поставлених задач і **ніколи** не ставлячи
URL з обліковими даними.
- **Перевірка:** `tests/test_compliance.py::test_forbidden_website_is_skipped_during_collection`
  (частково)

### REQ-RUN-026 — Повторний збір не втрачає збагачення
Новий запис **ПОВИНЕН** зливатися з поточним станом: поля, які повернула сторінка, перемагають;
поля збагачення без нового значення переносяться; `provenance` зливається так само.
- **Сценарій:** GIVEN запис збагачено email і `tech_stack` WHEN повторний збір THEN email і
  `WordPress` у `tech_stack` лишаються.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`

### REQ-RUN-027 — Завершення: версія, маніфест, статус
Наприкінці збору/збагачення **ПОВИННО**: застосувати tombstones (08), записати снапшот і diff,
зберегти `dataset_versions` з маніфестом (`rows`, `pages`, `strategies`, `llm_calls`,
`llm_spend_usd`, `fill_rates`, `changes`, `full_crawl`, `removed`, `notes`, `sources`), виставити
статус: `succeeded`; `partial`, якщо є `failed`-задачі і рядки, або 0 рядків при завантажених
сторінках; `failed`, якщо 0 рядків і 0 сторінок. Задача → `done`/`failed`. Подія `run_finished`
(+ `changes_detected` і до 25 подій змін).
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`,
  `tests/test_tombstones.py::test_full_crawl_marks_missing_record_removed` (маніфест)

### REQ-RUN-028 — Журнал подій і SSE
`GET /events?after=N` **ПОВИНЕН** повертати події з `id > N` у порядку зростання (≤ 1000).
`/events/stream` — SSE (`id:` + `data:` JSON) з опитуванням ~0.6 с і keep-alive коментарем.
`/changes` повертає лише події типів `new_entity, removed_entity, field_changed, price_drop`.
- **Перевірка:** `tests/test_storage.py::test_events_and_llm_ledger` (`after_id`),
  `tests/test_api_flow.py::test_full_pipeline` (`/changes`); SSE — прогалина

### REQ-RUN-029 — Інкрементальна пагінація та деталізація API-записів
Для задач з `ApiSourceSpec` runner **ПОВИНЕН**:
- ставити `api_list` у чергу (в інкрементальному режимі — зі збереженого `api_state`, якщо є) і
  переходити по наступних сторінках у межах `limits.max_pages` без зациклення;
- завантажувати `api_detail` за `detail_url_template` через ту саму чергу з `PolicyGuardFetcher` і
  пропускати tombstones в інкрементальному режимі (`incremental=True`);
- зберігати курсор у `api_state` (`api_cursor_saved`) **лише** після успішного завершення прогону
  без помилкових або пропущених задач.
- **Перевірка:** `tests/test_open_api_source.py::test_api_collection_pagination_cursor_and_failure_safety`,
  `tests/test_open_api_source.py::test_sqlite_migration_v4_opens_v3_database`

### REQ-RUN-030 — Запуск збору API-задачі без CSS-калібрування
Для задачі з `spec.api_source is not None` `POST /api/tasks/{id}/run` і `Studio.collect` **ПОВИННІ**
дозволяти запуск збору зі статусу `draft` без попереднього калібрування і без `skip_calibration=true`,
не порушуючи правило 409 для звичайних HTML-задач.
- **Перевірка:** `tests/test_open_api_source.py::test_api_collection_pagination_cursor_and_failure_safety`

### REQ-RUN-040 — Конфігурація та валідація cron-розкладу на задачу
Система **ПОВИННА** дозволяти створювати, оновлювати, переглядати та видаляти cron-розклад
для задачі (5-польовий cron у UTC або `@hourly`, `@daily`, `@weekly`, прапори `enabled` і
`catch_up`, опційне підтвердження `high`-ризику з причиною ≥ 10 символів). Невалідний cron або
відсутня причина підтвердження **ПОВИННІ** відхилятися (HTTP 422 / 400); зміни пишуть події
`schedule_updated` / `schedule_deleted`.
- **Перевірка:** `tests/test_scheduler.py::test_cron_validation_and_schedule_crud_api`

### REQ-RUN-041 — Автоматичний запуск за розкладом і захист від накладання
Вбудований планувальник **ПОВИНЕН** запускати `collect` для увімкнених прострочених розкладів
(`next_run_at <= now`): пропускати активний запуск (`schedule_skipped`, `reason="active_run"`),
блокувати порушення комплаєнсу або брак калібрування (`schedule_blocked`, `level="warning"`) і в
обох випадках переносити `next_run_at > now` без зациклення; при успішному запуску — оновлювати
`last_run_at`/`last_run_id`, пересувати `next_run_at > now` і писати `schedule_triggered`.
На кожному тіку планувальник також викликає `flush_due_digests(now=current)` для планової
відправки накопичених підписок `digest_daily` (`REQ-DEL-005`).
- **Перевірка:** `tests/test_scheduler.py::test_scheduler_triggers_skips_active_and_respects_compliance`,
  `tests/test_delivery.py::test_digest_daily_queue_scheduler_flush_and_failure_safety`

### REQ-RUN-042 — Доганяючий прогін після простою (`catch_up`)
При запізненні більше ніж на 120 секунд: `catch_up=true` виконує **рівно один** доганяючий
запуск (`data.catch_up=true`) і пересуває `next_run_at > now`; `catch_up=false` пропускає слот із
подією `schedule_skipped` (`reason="missed_no_catchup"`) без створення запуску.
- **Перевірка:** `tests/test_scheduler.py::test_scheduler_catch_up_and_missed_slot_policy`

### REQ-RUN-050 — Реакція пайплайна на відкритий circuit breaker та fallback на API/дзеркало
Під час збору (`PipelineRunner`) система **ПОВИННА**:
- при відкритті circuit breaker на домені записувати подію `circuit_opened` (`warning`) один раз на перехід у `open`;
- якщо у політиці джерела (`SourcePolicy`) для домену задано дзеркальні/API `fallback_urls`, перемикати заблокований запит (один раз на URL, без зациклення) на дозволений `assert_logged_out` та `PolicyGuardFetcher` fallback URL і записувати подію `circuit_fallback`;
- задачі черги, пропущені через `circuit_open`, позначати як `skipped`, зберігаючи консервативний інваріант tombstones (`full_crawl=false`, існуючі записи не видаляються).
- **Перевірка:** `tests/test_proxy_resilience.py::test_pipeline_circuit_breaker_fallback_and_compliance_guard`

### REQ-RUN-060 — Події та діагностика JS-рендерингу в пайплайні і калібруванні
Під час калібрування (`Calibrator`) і збору (`PipelineRunner`) система **ПОВИННА**:
- при успішній автоматичній ескалації сторінки в JS-рендер (не з кешу) записувати подію `js_rendered` (`info`) з `url`, `engine` та причиною детекції;
- при fallback через недоступність або транзитну помилку JS-рендера записувати подію `js_render_fallback` (`warning`) і додавати попередження до `CalibrationReport.warnings` (як при fallback, так і при виявленні JS-оболонки у режимі `fetch_engine="http"`).
- **Перевірка:** `tests/test_js_sidecar.py::test_pipeline_events_metrics_api_doctor_and_sidecar_deploy`

## 5. Спостережувана поведінка / відкриті питання
- Після вичерпання `DAAS_MAX_LLM_CALLS_PER_RUN` подія `llm_limit` пишеться на **кожній** ітерації
  циклу (не один раз) — журнал засмічується. Ймовірно, має писатися один раз за запуск.

## 6. Відомі обмеження
- Перерваний прогін може залишити версії записів без рядка `dataset_versions`; наступний прогін
  використає той самий номер (`docs/03` §7 п.2).
- Запуски живуть у процесі; паралельний `daas serve` і CLI-запис — заборонено (один writer DuckDB).

## 7. Прогалини
REQ-RUN-003 (API), REQ-RUN-004 (`interrupt_stale_runs`), REQ-RUN-011, REQ-RUN-022 (гілки
`transient/blocked/robots`), REQ-RUN-023 (healing), SSE-стрім.
