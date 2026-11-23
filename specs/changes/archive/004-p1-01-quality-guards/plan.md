# 004 — План реалізації: Автоматичні перевірки якості та офлайн gold-set eval

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Додамо легкий детермінований модуль `backend/app/pipeline/quality.py` із двома точками входу:
1. `evaluate_dataset_quality(...)` — запускається в `PipelineRunner._finish_dataset` перед фіксацією
   `dataset_versions.manifest`, оцінює падіння кількості рядків і заповненість `required` не-AI
   колонок, зберігає `QualityChecksReport` у `manifest.quality_checks`, пише подію `quality_warning`
   та надсилає алерт власнику через `DeltaDispatcher.dispatch_alert(..., kind="quality_warning")`.
2. `run_gold_eval(...)` + `load_gold_set(...)` — повністю офлайновий eval-харнес, який підтримує як
   `api_json` (`extract_json_records`), так і HTML (`ExtractionEngine.extract(..., allow_llm=False)`),
   рахує precision/recall/F1/coverage/`llm_fallback_pct`/`usd_per_1k_records`, оновлює
   `manifest.eval` останньої версії через `OperationalStore.update_latest_version_manifest` і
   викликається через `Studio.evaluate_task`, `POST /api/tasks/{id}/eval` та `daas eval`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` і `plan.md` створені до коду |
| II. Детермінованість, LLM лише fallback | ✅ | Пороги й gold eval повністю детерміновані (`allow_llm=False`) |
| III. Тести офлайн | ✅ | Фікстури у `tests/fixtures/gold/`, жодного мережевого виклику |
| IV. Походження, append-only | ✅ | `record_versions` не змінюється; `quality_warning` не відкочує валідні дані |
| V. Комплаєнс і PII | ✅ | Події `eval_completed` та `quality_warning` не містять значень PII-полів |
| VI. Local-first, захищений API | ✅ | `POST /api/tasks/{id}/eval` під Bearer/Host guard; заборона LFI (`gold_preset` regex) |
| VII. Події для змін даних | ✅ | Додаємо `quality_warning` і `eval_completed` у каталог `00-domain-model.md` §4 |
| VIII. Типізовані межі, міграції | ✅ | Нові Pydantic-моделі; без міграції SQLite (використовується `manifest_json`) |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових runtime-залежностей, використовуємо stdlib + Pydantic |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Налаштування | `app/config.py`, `.env.example` | `quality_max_drop_ratio`, `quality_min_required_fill_pct`, `eval_min_f1` | REQ-QUAL-001, REQ-QUAL-002 |
| Моделі | `app/models.py` | `QualityCheckIssue`, `QualityChecksReport`, `EvalFieldMetrics`, `EvalReport` | REQ-QUAL-001, REQ-QUAL-002 |
| Сховище | `app/db/sqlite_store.py` | `update_latest_version_manifest(dataset_id, patch)` | REQ-QUAL-002 |
| Пайплайн | `app/pipeline/quality.py`, `app/pipeline/runner.py`, `app/runtime.py` | `evaluate_dataset_quality`, `load_gold_set`, `run_gold_eval`, інтеграція в `_finish_dataset` і `Studio.evaluate_task` | REQ-QUAL-001, REQ-QUAL-002 |
| API | `app/api/schemas.py`, `app/api/tasks.py` | `GoldCaseInput`, `EvalRequest`, `POST /api/tasks/{id}/eval` | REQ-QUAL-002 |
| CLI | `app/cli.py` | `daas eval --task <id> --gold <path> [--min-f1 ...]` | REQ-QUAL-002 |
| UI / Клієнт | `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/components/TaskWorkspace.tsx` | Типи якості/eval, `api.evalTask`, компактний бейдж у заголовку задачі | REQ-QUAL-001, REQ-QUAL-002 |

## 4. Дані та міграції
Міграція SQLite не потрібна: таблиця `dataset_versions` вже зберігає довільний JSON у колонці
`manifest_json`. Метод `OperationalStore.update_latest_version_manifest(dataset_id, patch)` атомарно
оновлює словник маніфесту останньої версії (зберігаючи існуючі ключі включно з `quality_checks`).

## 5. Контракти
- **Нові налаштування (`DAAS_*` + `.env.example`):**
  - `DAAS_QUALITY_MAX_DROP_RATIO=0.5` (`[0.0, 1.0]`)
  - `DAAS_QUALITY_MIN_REQUIRED_FILL_PCT=80.0` (`[0.0, 100.0]`)
  - `DAAS_EVAL_MIN_F1=0.8` (`[0.0, 1.0]`)
- **Події (`00-domain-model.md` §4):**
  - `quality_warning` (`warning`, `pipeline/runner.py`) — аномальне падіння рядків або порожнє обов'язкове поле
  - `eval_completed` (`info`/`warning`, `runtime.Studio`) — підсумок gold-set eval без сирих PII-значень
- **HTTP API (`11-api-and-security.md`):**
  - `POST /api/tasks/{task_id}/eval` → тіло `EvalRequest(cases=..., gold_preset=..., min_f1=...)` → `200 {"report": EvalReport}`; `404` якщо задачі немає; `400` якщо невалідний пресет / порожній набір; `422` при помилці Pydantic.
- **CLI (`12-storage-cli-ops.md`):**
  - `daas eval --task <id> --gold <path> [--min-f1 0.8]` — виводить звіт, код виходу `1` при `not report.passed` або відсутній задачі/файлі.

## 6. Ризики і відкат
- Хибні спрацювання порогів якості на маленьких вибірках або змінній кількості записів: пороги
  винесено в env-налаштування (`DAAS_QUALITY_MAX_DROP_RATIO`, `DAAS_QUALITY_MIN_REQUIRED_FILL_PCT`),
  а `quality_warning` є неблокуючим попередженням (не зриває збереження датасету).
- Безпека API-ендпоінту `/eval`: строгий білий список символів у `gold_preset` + резолв виключно в
  безпечних базових директоріях виключає читання довільних файлів.

## 7. Тестова стратегія
- Нові фікстури `backend/tests/fixtures/gold/prozorro_gold.json` і
  `backend/tests/fixtures/gold/clinics_gold.json` (посилаються на наявні офлайн-фікстури).
- Новий файл `backend/tests/test_quality.py`:
  1. `test_post_run_quality_guards_pass_and_warn`: здоровий прогін (`passed=true`), падіння рядків
     >50% (`row_count_drop` + `quality_warning`), порожня обов'язкова колонка (`required_field_empty`),
     збереження валідних записів попри попередження.
  2. `test_quality_warning_sends_owner_alert`: доставка алерту `kind="quality_warning"` через
     `DeltaDispatcher` (`httpx.MockTransport`) підписнику з `notify_on_error=true`, а також перевірка,
     що збій доставки алерту не ламає фіксацію версії.
  3. `test_gold_eval_api_json_and_html_regression_gate`: gold eval для `api_json` і для HTML-задачі,
     збереження `manifest.eval`, подія `eval_completed`, падіння нижче порогу при зламаному селекторі.
  4. `test_eval_endpoint_and_cli_exit_codes`: `POST /api/tasks/{id}/eval` (200 pass, 400 path
     traversal / empty cases, 404 unknown task) + CLI `daas eval` (exit 0 на проходженні, exit 1 на
     регресії).
