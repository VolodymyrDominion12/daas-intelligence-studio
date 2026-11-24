# 012 — Задачі: Редактор spec задачі та schema diff після калібрування

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`
- [x] T002 Спроєктувати міграцію v8 у `sqlite_store.py` і оновити `test_entity_merge.py` (`user_ver >= 7`)

## Фаза 1 — Тести (пишуться до реалізації і мають падати)
- [x] T010 [P] Тест `REQ-OPS-010` (міграція v7→v8 зі збереженням калібрувань) → `tests/test_spec_editor_and_diff.py::test_sqlite_migration_v8_opens_v7_and_preserves_calibration`
- [x] T011 [P] Тест `REQ-PLAN-014` (оновлення, перестановка, ручні селектори і валідація помилок) → `tests/test_spec_editor_and_diff.py::test_column_update_reorder_and_selector_validation`
- [x] T012 [P] Тест `REQ-CAL-010` (пріоритет ручних CSS-селекторів у калібруванні й зборі) → `tests/test_spec_editor_and_diff.py::test_manual_selectors_take_precedence_in_calibration_and_collect`
- [x] T013 [P] Тест `REQ-CAL-009` (історія калібрувань, Schema Diff, відкат, синхронізація `selector_cache`, відмови) → `tests/test_spec_editor_and_diff.py::test_calibration_history_schema_diff_and_rollback`
- [x] T014 [P] Тест `REQ-API-013` (API-контракти редактора і diff/rollback) → `tests/test_spec_editor_and_diff.py::test_spec_editor_and_diff_api_contracts`

## Фаза 2 — Реалізація
- [x] T020 Моделі й валідатор CSS-селекторів у `backend/app/models.py` (`FieldSpec`, `SchemaFieldDiff`, `SchemaDiffReport`, `CalibrationHistoryEntry`)
- [x] T021 Сховище SQLite: міграція v8 `calibration_history`, методи історії/селекторів у `backend/app/db/sqlite_store.py`
- [x] T022 Екстракція і пайплайн: `learner.py` (ручні селектори), `runner.py` (`apply_manual_selectors`), `calibration.py` (`compute_schema_diff`, збереження `spec_json`), `runtime.py` (`update_column`, `reorder_columns`, `calibration_state`, `rollback_calibration`)
- [x] T023 API і схеми запитів/відповідей у `backend/app/api/schemas.py` та `backend/app/api/tasks.py`
- [x] T024 UI: `frontend/src/types.ts`, `frontend/src/api.ts`, `PlanPanel.tsx`, `CalibrationPanel.tsx`, `TaskWorkspace.tsx`, `styles.css` (`REQ-UI-016`)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/*.md` (00, 01, 02, 11, 12, 13) і `specs/README.md`
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (статус `P1-10`, §3.7, §6) та `specs/changes/README.md`
- [x] T042 Позначити `spec.md` (`done`) і перемістити `specs/changes/012-p1-10-spec-editor-schema-diff/` → `specs/changes/archive/`
