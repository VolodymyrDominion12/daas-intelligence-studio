# 007 — План реалізації: Карантин підозрілих записів у пайплайні та UI

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Замість мовчазного відкидання частково пошкоджених або невалідних за типом кандидатів у `ExtractionEngine` та `api_json`, вони повертаються у структурованому списку з українською причиною та записуються в таблицю DuckDB `quarantine` (розширену колонкою `id` через forward-only міграцію). У `AnalyticsStore`, `Studio`, HTTP API (`api/data.py`) і веб-інтерфейсі (`QuarantinePanel.tsx`) додаються операції перегляду, підтвердження в `record_versions` та відхилення елементів карантину з аудитом у журналі подій.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `007-p1-08-quarantine/{spec,plan,tasks}.md` створені до коду |
| II. Детермінованість, LLM лише fallback | ✅ | Валідація карантину повністю детермінована (правила типів, повноти, впевненості) |
| III. Тести офлайн | ✅ | `tests/test_quarantine.py` повністю офлайн |
| IV. Походження, append-only | ✅ | Підтвердження з карантину додає append-only версію в `record_versions` з `Provenance(method="manual")` |
| V. Комплаєнс і PII | ✅ | Нових мережевих шляхів немає; події не містять сирих PII-значень |
| VI. Local-first, захищений API | ✅ | Нові `/api/tasks/{id}/quarantine*` захищені тим самим Bearer-middleware |
| VII. Події для змін даних | ✅ | Додаються `record_quarantined`, `quarantine_approved`, `quarantine_dismissed` до каталогу `00-domain-model.md` |
| VIII. Типізовані межі, міграції | ✅ | Pydantic-моделі запитів/відповідей + DuckDB `_migrate()` для `quarantine.id` + тест старого файлу |
| IX. Простота, без зайвих залежностей | ✅ | Без нових runtime-залежностей |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Моделі | `app/extraction/base.py`, `app/api/schemas.py` | Поле `quarantined` у `ExtractionOutcome` | `REQ-EXT-006`, `REQ-HIST-040` |
| Екстракція | `app/extraction/engine.py`, `app/extraction/api_json.py` | Виправлення `_is_usable`; повернення відхилених/некоректних за типом кандидатів; `extract_json_records_with_quarantine` | `REQ-EXT-006`, `REQ-HIST-040` |
| Сховище | `app/db/duckdb_store.py`, `app/db/sqlite_store.py` | Міграція `quarantine.id`, методи `list_quarantine`, `count_quarantine`, `get_quarantine_item`, `delete_quarantine_item`; оновлення `row_count` версії при approve | `REQ-HIST-040`, `REQ-HIST-041` |
| Пайплайн / Сервіс | `app/pipeline/runner.py`, `app/runtime.py` | Спрямування підозрілих записів у `quarantine`, `stats.records_quarantined`, методи `Studio.list_quarantine`, `approve_quarantine`, `dismiss_quarantine` | `REQ-HIST-040`, `REQ-HIST-041` |
| API | `app/api/data.py`, `app/api/tasks.py` | Ендпоінти `GET/POST approve/DELETE /tasks/{id}/quarantine*`; `quarantine_count` у `GET /tasks/{id}` | `REQ-HIST-041` |
| UI | `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/components/QuarantinePanel.tsx`, `TaskWorkspace.tsx`, `styles.css` | Типи, API-клієнт, вкладка «Карантин» із бейджем і діями approve/dismiss | `REQ-UI-013` |

## 4. Дані та міграції
- DuckDB `quarantine`: додається колонка `id VARCHAR` у `SCHEMA` та через `AnalyticsStore._migrate()` (якщо `id` відсутній в `information_schema.columns`). Для існуючих рядків з `id IS NULL` виставляється `uuid.uuid4().hex[:16]`. На `quarantine` немає індексів, тому `ALTER TABLE` безпечний.
- Тест відкриття старого файлу DuckDB (таблиця `quarantine` без колонки `id` з рядком даних) у `tests/test_quarantine.py::test_old_duckdb_quarantine_is_migrated`.

## 5. Контракти
- Нові API ендпоінти:
  - `GET /api/tasks/{task_id}/quarantine?limit=50&offset=0` → `{ items, total, limit, offset }`
  - `POST /api/tasks/{task_id}/quarantine/{item_id}/approve` → `{ record, row_count }` (або `404`)
  - `DELETE /api/tasks/{task_id}/quarantine/{item_id}` → `{ deleted: true }` (або `404`)
- Нові події в `00-domain-model.md`:
  - `record_quarantined` (`warning`, `pipeline/runner.py`): підозрілий запис спрямовано в карантин (`data.reason`, `data.url`)
  - `quarantine_approved` (`info`, `runtime.py`): запис підтверджено з карантину в датасет (`data.item_id`, `data.record_key`)
  - `quarantine_dismissed` (`info`, `runtime.py`): запис відхилено з карантину (`data.item_id`)

## 6. Ризики і відкат
- **Ризик:** Надмірне карантинування валідних записів у існуючих тестах або в проді.
  - *Мітигація:* Карантинимо лише те, що раніше або відкидалося в `_is_usable`, або має явну помилку приведення не-порожнього значення типу (`coerce_value`), або `< 2` валідних полів (за `>= 2` джерельних колонок), або `mean_confidence < 0.4`. Записи без `required` поля без помилки типу лишаються в основному потоці (як і вимагає `REQ-QUAL-001`). Увесь існуючий тестовий набір (248 тестів) підтвердить відсутність регресій.
- **Відкат:** Нова логіка ізольована в хелпері валідації та API-ендпоінтах, схема `quarantine.id` зворотно сумісна.

## 7. Тестова стратегія
- `backend/tests/test_quarantine.py` (повністю офлайн):
  1. `test_old_duckdb_quarantine_is_migrated`: відкриття DuckDB без `quarantine.id` і backfill ідентифікатора.
  2. `test_extraction_usability_counts_after_coercion_and_flags_quarantine`: `REQ-EXT-006` — HTML-елемент з битим типом (`price="невідомо"`) або без ідентифікуючого поля відсіюється і потрапляє у `quarantined` з причиною.
  3. `test_runner_quarantines_suspect_html_and_api_records`: наскрізний збір HTML та `api_json` — підозрілі записи йдуть у `quarantine` (`stats.records_quarantined`, подія `record_quarantined`), здорові (включно з пропущеним опційним полем) лишаються в `current_records`.
  4. `test_quarantine_api_list_approve_dismiss_and_404`: API-тести `GET /quarantine`, `POST /approve` (з'являється в `/records`, росте `row_count`, подія `quarantine_approved`), `DELETE` (зникає, подія `quarantine_dismissed`), 404 на невідомому `item_id` чи `task_id`.
- Фронтенд перевіряється через `npm run typecheck` і `npm run build`.
