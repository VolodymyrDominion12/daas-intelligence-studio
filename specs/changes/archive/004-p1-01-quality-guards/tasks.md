# 004 — Задачі: Автоматичні перевірки якості та офлайн gold-set eval

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`
- [x] T002 Створити офлайн gold-фікстури `backend/tests/fixtures/gold/{prozorro_gold,clinics_gold}.json`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест REQ-QUAL-001 (здоровий прогін + падіння рядків + порожня обов'язкова колонка) → `backend/tests/test_quality.py::test_post_run_quality_guards_pass_and_warn`
- [x] T011 [P] Тест REQ-QUAL-001 (алерт власнику `quality_warning` і стійкість до збою доставки) → `backend/tests/test_quality.py::test_quality_warning_sends_owner_alert`
- [x] T012 [P] Тест REQ-QUAL-002 (gold eval API JSON та HTML + регресійний поріг + `manifest.eval`) → `backend/tests/test_quality.py::test_gold_eval_api_json_and_html_regression_gate`
- [x] T013 [P] Тест REQ-QUAL-002 (`POST /api/tasks/{id}/eval` + CLI `daas eval` коди виходу 0/1) → `backend/tests/test_quality.py::test_eval_endpoint_and_cli_exit_codes`

## Фаза 2 — Реалізація
- [x] T020 Налаштування `DAAS_QUALITY_*`, `DAAS_EVAL_MIN_F1` (`app/config.py`, `.env.example`)
- [x] T021 Моделі (`QualityCheckIssue`, `QualityChecksReport`, `EvalFieldMetrics`, `EvalReport` у `app/models.py`)
- [x] T022 Сховище (`OperationalStore.update_latest_version_manifest` у `app/db/sqlite_store.py`)
- [x] T023 Модуль якості й eval (`app/pipeline/quality.py`: `evaluate_dataset_quality`, `load_gold_set`, `run_gold_eval`)
- [x] T024 Інтеграція в раннер і студію (`app/pipeline/runner.py`, `app/runtime.py`)
- [x] T025 API (`app/api/schemas.py`, `app/api/tasks.py`)
- [x] T026 CLI (`daas eval` у `app/cli.py`)
- [x] T027 Фронтенд типи, API-клієнт і компактний бейдж у `TaskWorkspace.tsx` (`frontend/src/*`)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{00-domain-model,08-history-and-changes,11-api-and-security,12-storage-cli-ops}.md`
- [x] T041 Оновити `docs/03` (статус, беклог, обмеження)
- [x] T042 Перенести `specs/changes/004-p1-01-quality-guards/` → `specs/changes/archive/` і оновити `specs/changes/README.md`
