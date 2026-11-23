# 007 — Задачі: Карантин підозрілих записів у пайплайні та UI

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест міграції старого DuckDB файлу без `quarantine.id` → `backend/tests/test_quarantine.py::test_old_duckdb_quarantine_is_migrated`
- [x] T011 [P] Тест `REQ-EXT-006` + `REQ-HIST-040` (HTML + API JSON маршрутизація підозрілих записів у карантин) → `backend/tests/test_quarantine.py::test_extraction_usability_counts_after_coercion_and_flags_quarantine`, `test_runner_quarantines_suspect_html_and_api_records`
- [x] T012 [P] Тест `REQ-HIST-041` (API list/approve/dismiss + події + 404) → `backend/tests/test_quarantine.py::test_quarantine_api_list_approve_dismiss_and_404`

## Фаза 2 — Реалізація
- [x] T020 Сховище: `duckdb_store.py` (міграція `quarantine.id`, методи списку/читання/видалення/підрахунку) + `sqlite_store.py` (`update_version_row_count`)
- [x] T021 Екстракція: `extraction/base.py`, `extraction/engine.py` (`_is_usable` + `outcome.quarantined`), `extraction/api_json.py` (`extract_json_records_with_quarantine`)
- [x] T022 Пайплайн і сервіс: `pipeline/runner.py` (запис у `quarantine`, `stats.records_quarantined`, подія `record_quarantined`, `manifest.quarantined`) + `runtime.py` (`list_quarantine`, `approve_quarantine`, `dismiss_quarantine` з подіями)
- [x] T023 HTTP API: `api/data.py` (`GET/POST approve/DELETE /tasks/{id}/quarantine*`) + `api/tasks.py` (`quarantine_count`)
- [x] T024 UI: `frontend/src/types.ts`, `frontend/src/api.ts`, `QuarantinePanel.tsx`, `TaskWorkspace.tsx`, `styles.css` (стани завантаження/порожньо/помилка, approve/dismiss)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{00-domain-model,03-extraction,08-history-and-changes,11-api-and-security,13-frontend}.md`
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (`P1-08 ✅`)
- [x] T042 Перенести `specs/changes/007-p1-08-quarantine/` → `specs/changes/archive/` та оновити `specs/changes/README.md`
