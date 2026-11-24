# 012 — План реалізації: Редактор spec задачі та schema diff після калібрування

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення

1. Розширимо доменні моделі в `app/models.py` моделями `SchemaFieldDiff`, `SchemaDiffReport`, `CalibrationHistoryEntry` і валідатором CSS-селекторів (`validate_css_selector` через `selectolax.parser.HTMLParser`) для `FieldSpec.selector`.
2. Додамо forward-only міграцію SQLite **v8** (`calibration_history`) у `app/db/sqlite_store.py`, яка автоматично переносить наявні рядки з `calibrations` у `version=1` і зберігає кожне наступне калібрування або відкат як нову версію, а також методи `list_calibration_history`, `get_calibration_entry`, `delete_selector`.
3. Навчимо `app/extraction/learner.py` і `app/pipeline/runner.py` (функція `apply_manual_selectors`) завжди враховувати ручні `FieldSpec.selector` / `FieldSpec.attr` поверх евристично вивченої чи кешованої схеми, а `app/pipeline/calibration.py` + `app/runtime.py` — обчислювати `compute_schema_diff` і виконувати `rollback_calibration` (відновлення звіту + синхронізація `selector_cache` + подія `calibration_rolled_back`).
4. Додамо ендпоінти `PATCH /api/tasks/{id}/columns/{key}`, `POST /api/tasks/{id}/columns/reorder`, `POST /api/tasks/{id}/calibration/rollback` та розширення `GET /api/tasks/{id}` і `GET /api/tasks/{id}/calibration` у `app/api/tasks.py` і `app/api/schemas.py`.
5. Оновимо `frontend/src/types.ts`, `api.ts`, `PlanPanel.tsx` (повний редактор колонок, перестановка, ручні селектори, додавання стандартних та AI-колонок), `CalibrationPanel.tsx` (відображення Schema Diff, списку версій та кнопки «Відкотити схему») і `TaskWorkspace.tsx`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` → `plan.md` → `tasks.md` → тести → код → `capabilities/` |
| II. Детермінованість, LLM лише fallback | ✅ | Ручні CSS-селектори та `compute_schema_diff` повністю детерміновані; ніякого `eval/exec` |
| III. Тести офлайн | ✅ | Усі тести в `tests/test_spec_editor_and_diff.py` на `FixtureFetcher` і локальних HTML |
| IV. Походження, append-only | ✅ | `record_versions` не змінюється; `calibration_history` також append-only |
| V. Комплаєнс і PII | ✅ | Оновлення спеки зберігає `assert_logged_out` на seed-ах; зміна `pii` одразу діє на експорт і LLM |
| VI. Local-first, захищений API | ✅ | Усі нові ендпоінти під `/api/*` з Bearer-токеном і Host-guard |
| VII. Події для змін даних | ✅ | Події `column_updated`, `columns_reordered`, `calibration_rolled_back` у `events` + каталог `00-domain-model.md` |
| VIII. Типізовані межі, міграції | ✅ | Pydantic v2 моделі (`UpdateColumnRequest`, `ReorderColumnsRequest`, `RollbackCalibrationRequest`, `SchemaDiffReport`), SQLite v8 |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових залежностей (використовується наявний `selectolax`) |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Моделі | `backend/app/models.py` | Валідація `FieldSpec.selector` (`validate_css_selector`), `SchemaFieldDiff`, `SchemaDiffReport`, `CalibrationHistoryEntry` | `REQ-PLAN-014`, `REQ-CAL-009` |
| Сховище | `backend/app/db/sqlite_store.py` | Міграція v8 `calibration_history` + backfill із `calibrations`; `save_calibration(..., source, spec_json)`, `list_calibration_history`, `get_calibration_entry`, `delete_selector` | `REQ-OPS-010`, `REQ-CAL-009` |
| Екстракція / Пайплайн | `backend/app/extraction/learner.py`, `backend/app/pipeline/calibration.py`, `backend/app/pipeline/runner.py`, `backend/app/runtime.py` | Пріоритет ручних селекторів у `learner` і `runner._extract_page` (`apply_manual_selectors`); `compute_schema_diff`, `Studio.update_column`, `Studio.reorder_columns`, `Studio.calibration_state`, `Studio.rollback_calibration`, оновлення `enrichment` у `update_task` | `REQ-PLAN-014`, `REQ-CAL-009`, `REQ-CAL-010` |
| API | `backend/app/api/schemas.py`, `backend/app/api/tasks.py` | `UpdateColumnRequest`, `ReorderColumnsRequest`, `RollbackCalibrationRequest`; роути `PATCH /columns/{key}`, `POST /columns/reorder`, `POST /calibration/rollback`, розширені `GET /tasks/{id}` і `GET /calibration` | `REQ-PLAN-014`, `REQ-API-013`, `REQ-CAL-009` |
| UI | `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/components/PlanPanel.tsx`, `frontend/src/components/CalibrationPanel.tsx`, `frontend/src/components/TaskWorkspace.tsx`, `frontend/src/styles.css` | Редактор колонок (додавання CSS/AI/enrichment, редагування `title/dtype/pii/required/method/selector/attr/prompt`, перестановка `↑/↓`), показ Schema Diff і кнопка відкату схеми | `REQ-UI-016` |
| Спеки і тести | `backend/tests/test_spec_editor_and_diff.py`, `backend/tests/test_entity_merge.py` (`>= 7`), `specs/capabilities/*.md`, `docs/03` | Повні офлайн-тести, міграція v7→v8, синхронізація спек | Усі |

## 4. Дані та міграції

- **SQLite Migration #8:**
  ```sql
  CREATE TABLE calibration_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      version INTEGER NOT NULL,
      run_id TEXT,
      report_json TEXT NOT NULL,
      spec_json TEXT NOT NULL DEFAULT '{}',
      source TEXT NOT NULL DEFAULT 'calibrate',
      created_at TEXT NOT NULL,
      UNIQUE(task_id, version)
  );
  CREATE INDEX idx_calibration_history_task ON calibration_history(task_id, version DESC);
  INSERT INTO calibration_history(task_id, version, run_id, report_json, spec_json, source, created_at)
  SELECT task_id, 1, run_id, report_json, '{}', 'legacy', created_at FROM calibrations;
  ```
- **Зворотна сумісність:** наявні БД з `user_version = 7` і таблицею `calibrations` мігруються автоматично; існуючі дані не губляться; DuckDB не змінюється. (У `tests/test_entity_merge.py` рядок `assert user_ver == 7` оновлюється на `>= 7`).

## 5. Контракти

- **API:**
  - `PATCH /api/tasks/{id}/columns/{key}` → `UpdateColumnRequest` (`title?`, `dtype?`, `description?`, `required?`, `pii?`, `method?`, `aliases?`, `selector?`, `attr?`, `prompt?`) → `200 {"task": ...}` / `404` / `400` / `422`.
  - `POST /api/tasks/{id}/columns/reorder` → `ReorderColumnsRequest` (`keys: list[str]`) → `200 {"task": ...}` / `400` / `404`.
  - `GET /api/tasks/{id}/calibration` → `{"calibration": ... | null, "version": int | null, "history": CalibrationHistoryEntry[], "diff": SchemaDiffReport | null}`.
  - `GET /api/tasks/{id}` → зберігає всі наявні поля + додає `calibration_version`, `calibration_history`, `calibration_diff`.
  - `POST /api/tasks/{id}/calibration/rollback` → `RollbackCalibrationRequest` (`version?: int | null`) → `200 {"calibration": ..., "version": int, "rolled_back_to": int, "task": ..., "diff": ..., "history": ...}` / `400` (немає попередньої версії) / `404` (задачу або версію не знайдено) / `409` (активний запуск).
- **Події (`00-domain-model.md` §4):**
  - `column_updated` (`info`, `api/tasks.py` / `runtime.Studio`)
  - `columns_reordered` (`info`, `api/tasks.py` / `runtime.Studio`)
  - `calibration_rolled_back` (`info`, `runtime.Studio`)

## 6. Ризики і відкат

- **Ризик:** користувач введе невалідний CSS-селектор і зламає збір. **Мітігація:** негайна валідація синтаксису `selectolax` у `FieldSpec._valid_selector` та в ендпоінтах перед збереженням + захист `try/except` у `extract_css`.
- **Ризик:** при перекалібруванні `selector_cache` перезапише стару робочу схему і відкат `calibrations` не поверне старі селектори. **Мітігація:** `rollback_calibration` явно синхронізує `selector_cache` (перезаписує або видаляє запис домену).
- **Ризик:** у `CalibrationHistoryEntry` сирі `samples` можуть містити великі обсяги, але не HTML (лише до 5 витягнутих полів/сторінку), проте для списку `history` в API повертаємо легкі метадані версії (`version`, `run_id`, `source`, `strategy`, `container`, `score`, `records_found`, `fields_count`, `created_at`) без повних `samples`.

## 7. Тестова стратегія

Новий модуль `backend/tests/test_spec_editor_and_diff.py` (повністю офлайн на `FixtureFetcher` і локальних HTML):
1. `test_sqlite_migration_v8_opens_v7_and_preserves_calibration` — створює БД v7 з таблицею `calibrations`, мігрує до v8 і перевіряє перенесення запису як `version = 1`.
2. `test_column_update_reorder_and_selector_validation` — додавання CSS-колонки, оновлення `title`/`dtype`/`pii`/`selector`/`attr`, перестановка колонок, події `column_updated` та `columns_reordered`, відхилення невалідного CSS-селектора та неповного списку ключів.
3. `test_manual_selectors_take_precedence_in_calibration_and_collect` — калібрування і збір із ручним селектором поверх авто-кешу (`selector_cache`), включно з відсутнім елементом на сторінці (не падає і не підставляє старий авто-селектор).
4. `test_calibration_history_schema_diff_and_rollback` — два послідовні калібрування з різною версткою v1→v2, перевірка `SchemaDiffReport` (контейнер, додані/видалені/змінені поля, покриття), відкат до v1, синхронізація `selector_cache`, збір на селекторах v1, перевірка відмови при <2 версій / неіснуючій версії / активному запуску.
5. `test_spec_editor_and_diff_api_contracts` — наскрізний HTTP API тест усіх нових ендпоінтів, Bearer-авторизації, кодів 200/400/404/409/422.
