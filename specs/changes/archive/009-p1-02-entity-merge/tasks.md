# 009 — Задачі: Entity Resolution v2 (merge/unmerge дублікатів + UI-черга)

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`
- [x] T002 Міграція схеми SQLite #7 (`entity_links`) + перевірка відкриття старого файлу БД

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест міграції старого SQLite-файлу v6 → v7 → `backend/tests/test_entity_merge.py::test_old_sqlite_migrates_to_entity_links`
- [x] T011 [P] Тест політик `merge_records`, `Provenance(method="merge")` та `find_incremental_duplicates` → `backend/tests/test_entity_merge.py::test_deterministic_merge_policies_and_incremental_dedupe`
- [x] T012 [P] Тест API злиття, dismiss, роз'єднання без втрати append-only історії та кодів 400/404/409 → `backend/tests/test_entity_merge.py::test_merge_dismiss_and_reversible_unmerge_api`
- [x] T013 [P] Тест інкрементального збору з канонічним редіректом → `backend/tests/test_entity_merge.py::test_incremental_collect_redirects_merged_key_to_canonical`
- [x] T014 [P] Тест CLI `daas merge` / `daas unmerge` / `daas duplicates` → `backend/tests/test_entity_merge.py::test_cli_merge_and_unmerge`

## Фаза 2 — Реалізація
- [x] T020 Моделі (`app/models.py`: `MergePolicy`, `EntityLinkInfo`, `DuplicateCandidate`) та схеми API (`app/api/schemas.py`)
- [x] T021 Дедуплікація та політики (`app/enrichment/dedupe.py`: `merge_records`, `find_incremental_duplicates`, blocking і фільтрація)
- [x] T022 Сховище (`app/db/sqlite_store.py` міграція #7 + методи `entity_links`, `app/db/duckdb_store.py` читання ревізій)
- [x] T023 Сервіс і пайплайн (`app/runtime.py`, `app/pipeline/runner.py`) + події (`entity_merged`, `entity_unmerged`, `duplicate_dismissed`)
- [x] T024 HTTP API (`app/api/data.py`) та CLI (`app/cli.py`)
- [x] T025 UI (`frontend/src/{types.ts,api.ts,styles.css}`, `MiscPanels.tsx`, `TaskWorkspace.tsx`) зі станами порожньо / завантаження / помилка

## Фаза 3 — Перевірка
- [x] T030 `make specs-check test lint build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{00-domain-model,07-enrichment,08-history-and-changes,11-api-and-security,13-frontend}.md` (з посиланнями на тести)
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (`P1-02 ✅`, §3.2, §6, §7 п.8) і `specs/changes/README.md`
- [x] T042 Перенести `specs/changes/009-p1-02-entity-merge/` → `specs/changes/archive/`
