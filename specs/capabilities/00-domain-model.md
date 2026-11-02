# 00 — Доменна модель і каталог подій

| | |
|:---|:---|
| **Код** | `backend/app/models.py`, `backend/app/api/schemas.py`, `backend/app/utils.py` (`record_key_for`, `coerce_value`) |
| **Тести** | `tests/test_utils.py`, `tests/test_api_flow.py` |
| **Джерела** | `docs/02` §4 (модель даних), ADR-05, ADR-07 |
| **Статус** | as-built v0.1 |

## 1. Сутності

| Сутність | Модель | Де живе | Призначення |
|:---|:---|:---|:---|
| Задача | `TaskInfo` + `TaskSpec` | SQLite `tasks.spec_json` | Що збираємо: колонки, seed-и, ліміти, збагачення |
| Колонка | `FieldSpec` | у `TaskSpec.columns` | Ключ, тип, PII-клас, метод отримання |
| Запуск | `RunInfo` + `RunStats` | SQLite `runs` | Один прогін `calibrate / collect / enrich / snapshot` |
| Подія | `EventInfo` | SQLite `events` | Аудит і живий журнал (SSE) |
| Датасет | `DatasetInfo` | SQLite `datasets`, `dataset_versions` | Версії, маніфести, якість |
| Запис | `ExtractedRecord` | DuckDB `record_versions` | Payload + `Provenance` на кожне поле |
| Схема селекторів | `CssSchema` | SQLite `selector_cache` | Результат калібрування на `domain + entity` |
| Звіт калібрування | `CalibrationReport` | SQLite `calibrations` | Прев'ю, покриття, попередження |
| Політика джерела | `SourcePolicy` | SQLite `source_registry` | `tos_risk`, ліцензія, нотатки |
| Зміна | `ChangeEvent` | події + `snapshots/…/manifest.json` | `new_entity`, `removed_entity`, `field_changed`, `price_drop` |

`dataset_id` = `task.dataset_id or task.id` (у v0.1 один датасет на задачу).

## 2. Вимоги

### REQ-DM-001 — Ключ колонки — snake_case
`FieldSpec.key` **ПОВИНЕН** нормалізуватися (`strip`, lower, `-`/пробіл → `_`) і відповідати
`^[a-z][a-z0-9_]{1,40}$`; інакше — помилка валідації (HTTP 422 на межі API).
- **Перевірка (частково):** `tests/test_api_flow.py::test_task_editing_and_column_management` —
  лише валідний ключ і дубль колонки (409); нормалізація й відмова — прогалина

### REQ-DM-002 — Унікальні колонки, хоча б одна
`TaskSpec` **ПОВИНЕН** відхиляти дублікати ключів колонок і порожній список колонок.
- **Перевірка:** — (прогалина: немає прямого юніт-тесту валідатора)

### REQ-DM-003 — Нормалізація seed URL
`TaskSpec.seeds` **ПОВИНЕН** відкидати порожні значення, додавати `https://` до URL без схеми,
канонізувати (`canonical_url`: lower-case хост без `www.`, без query і кінцевого `/`) і прибирати
дублікати зі збереженням порядку.
- **Перевірка (частково):** `tests/test_utils.py::test_url_helpers` — лише `canonical_url`;
  валідатор `seeds` — прогалина

### REQ-DM-004 — Стабільний ключ запису (natural key)
`record_key_for(payload)` **ПОВИНЕН** повертати детермінований ключ:
1. `d:<authority>` — якщо є `website`/`domain` і хост **не** приватний/loopback;
2. інакше `n:<slug(name city)>-<hash6>` з нормалізованих назви й міста;
3. інакше `h:<hash16>` від payload.
Приватні хости не є ідентичністю компанії (демо-сайти на одному хості не злипаються).
- **Перевірка:** `tests/test_utils.py::test_record_keys_are_stable_and_domain_first`

### REQ-DM-005 — Приведення типів
`coerce_value(value, dtype)` **ПОВИНЕН** приводити значення до `DType`
(`number/integer/boolean/url/email/phone/datetime/json/array/string`) і повертати `None`, якщо
значення непридатне (а не кидати виняток).
- **Перевірка:** `tests/test_utils.py::test_coerce_value_respects_dtype`,
  `tests/test_utils.py::test_parse_number`, `tests/test_utils.py::test_parse_bool`

### REQ-DM-006 — Походження на рівні поля
Кожен `ExtractedRecord` **ПОВИНЕН** нести `source_url` і `provenance[field]` з `method`,
`confidence ∈ [0,1]`, `observed_at`.
- **Перевірка (частково):** `tests/test_api_flow.py::test_full_pipeline` (метод `name`),
  `tests/test_extraction.py::test_engine_uses_jsonld_before_css` (метод усіх полів)

### REQ-DM-007 — Колонки-AI не є колонками джерела
Колонка з `method="llm"` або непорожнім `prompt` (`FieldSpec.is_ai`) **НЕ ПОВИННА** потрапляти в
`TaskSpec.source_columns()`, у схему LLM-екстракції сторінки і в розрахунок заповненості.
- **Перевірка:** — (прогалина)

### REQ-DM-008 — Агрегація статистики запуску
`RunStats.merge` **ПОВИНЕН** сумувати числові поля і перезаписувати нечислові непорожніми.
- **Перевірка:** — (прогалина)

## 3. Статуси

**Задача (`TaskStatus`):** `draft` → (`calibrate`/`collect`/`enrich`) `running` →
`ready` (калібрування дало рядки) | `draft` (калібрування порожнє) | `done` (є записи) |
`failed` (збір без записів). Скасування → `ready`.

**Запуск (`RunStatus`):** `queued` → `running` → `succeeded` | `partial` (є відмови або 0 рядків при
завантажених сторінках; калібрування з 0 рядків) | `failed` | `cancelled`.
Після перезапуску процесу всі `queued/running` → `failed` («interrupted by process restart»).

## 4. Каталог подій (контракт журналу)

Нові типи додаються сюди в тій самій зміні, що їх вводить (Конституція, ст. VII).

| Тип | Рівень | Хто пише | Коли |
|:---|:---|:---|:---|
| `task_created`, `plan_ready` | info | `runtime.Studio.create_task` | Створено задачу; `data.spec` — план |
| `task_updated`, `column_added` | info | `api/tasks.py` | Редагування задачі/колонок |
| `run_created` | info | `runtime.Studio` | Запуск поставлено |
| `run_started`, `run_finished` | info/warning | `pipeline/runner.py` | Початок/кінець запуску; `data.version`, `rows`, … |
| `run_failed` | error | runner, `Studio._guard` | Виняток у запуску |
| `run_cancelled` | warning | `Studio.cancel` | Скасовано користувачем |
| `run_interrupted` | warning | `Studio.interrupt_stale_runs` | Процес перезапущено посеред запуску |
| `calibration_started`, `page_fetched`, `fetch_error`, `discovery`, `schema_learned`, `llm_fallback`, `calibration_finished` | різні | `pipeline/calibration.py` | Хід калібрування |
| `page_extracted` | debug | runner | Сторінка дала записи |
| `no_records` | warning/debug | runner | Сторінка без записів |
| `fetch_failed`, `job_failed` | warning/error | runner | Відмова завантаження / виняток у задачі черги |
| `robots_blocked` | warning | runner | Пропущено через robots.txt |
| `limit_reached`, `llm_limit` | warning | runner | Ліміт сторінок / LLM-викликів |
| `selector_healed` | info | runner | Схему перегенеровано після промаху |
| `records_removed`, `tombstones_skipped`, `tombstones_suspicious` | info/debug/warning | runner | Tombstones (див. 08) |
| `changes_detected`, `new_entity`, `removed_entity`, `field_changed`, `price_drop` | info/warning | runner | Зміни між версіями |
| `compliance_blocked` | error/warning | `Studio._compliance_gate`, `PolicyGuardFetcher` | Заблоковано політикою (URL редагований) |
| `compliance_override` | warning | `OperationalStore.create_run` (audit) | Підтверджено запуск по `high`-джерелах |
| `export_created` | info | `api/data.py` | Створено файл експорту |
| `backup_created`, `cache_purged` | info | `api/data.py` (`task_id="system"`) | Обслуговування |

## 5. Прогалини
- REQ-DM-002, REQ-DM-007, REQ-DM-008 — немає тестів; REQ-DM-001, REQ-DM-003, REQ-DM-006 — частково.
- Каталог подій не перевіряється автоматично (кандидат: тест, що всі `add_event(..., "<type>")` є в
  цьому списку).
