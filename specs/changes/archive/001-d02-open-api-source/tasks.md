# 001 — Задачі: Конектор відкритого JSON API (`D-02`)

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати [`spec.md`](./spec.md), [`plan.md`](./plan.md) і зачеплені `specs/capabilities/*.md`
- [x] T002 [P] Створити офлайн JSON-фікстури OpenProcurement у `backend/tests/fixtures/` (`prozorro_list_page1.json`, `prozorro_list_page2.json`, `prozorro_tender_001.json`, `prozorro_tender_002.json`, `broken_api.json`)

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-DM-009` (валідний `ApiSourceSpec`, збереження query в `seeds`, відхилення порожнього `field_map` або токенів у `detail_url_template`) → `backend/tests/test_open_api_source.py::test_api_source_spec_validation_and_logged_out`
- [x] T011 [P] Тест `REQ-PLAN-012` (офлайн-шаблон Prozorro/Prozorro.Продажі без LLM і без `pii="personal"`) → `backend/tests/test_open_api_source.py::test_openprocurement_preset_builds_non_pii_api_spec`
- [x] T012 [P] Тест `REQ-FETCH-006`, `REQ-FETCH-011` (`HttpFetcher` з `httpx.MockTransport`: `allow_json=True` приймає JSON, `allow_json=False` відхиляє; `PolicyGuardFetcher` блокує `forbidden` API-домен і приватний редирект) → `backend/tests/test_open_api_source.py::test_fetcher_json_mode_and_compliance_guard`
- [x] T013 [P] Тест `REQ-EXT-010` (декларативний JSON-екстрактор: ключ `api:<id>`, `Provenance(method="api_json")`, битий JSON → 0 записів + попередження) → `backend/tests/test_open_api_source.py::test_json_extractor_maps_fields_and_handles_broken_json`
- [x] T014 [P] Тест `REQ-RUN-029`, `REQ-RUN-030`, `REQ-CAL-008` (E2E `POST /api/tasks` → `POST /api/tasks/{id}/run` без калібрування, пагінація + деталі + збереження курсора + повторний інкрементальний запуск із `price_drop` без помилкових tombstones; відмова — 500 на 2-й сторінці не зсуває курсор) → `backend/tests/test_open_api_source.py::test_api_collection_pagination_cursor_and_failure_safety`
- [x] T015 [P] Тест міграції №4 SQLite (відкриття БД із `user_version=3` і створення `api_state`) → `backend/tests/test_open_api_source.py::test_sqlite_migration_v4_opens_v3_database`

## Фаза 2 — Реалізація
- [x] T020 Моделі `ApiSourceSpec`, `Method += "api_json"`, `TaskSpec` валідатори у `backend/app/models.py`
- [x] T021 Сховище: міграція №4 (`api_state`) та методи `get_api_state` / `save_api_state` у `backend/app/db/sqlite_store.py`
- [x] T022 Завантажувач: підтримка `allow_json` у `backend/app/scraping/fetcher.py`, `backend/app/compliance.py`, `backend/tests/conftest.py` (`FixtureFetcher`)
- [x] T023 Екстрактор JSON: реалізувати `backend/app/extraction/api_json.py`
- [x] T024 Пресети і планувальник: профіль OpenProcurement у `backend/app/presets.py` і `backend/app/pipeline/planner.py`
- [x] T025 Пайплайн: підтримка `api_source` у `backend/app/runtime.py` та `backend/app/pipeline/runner.py` (без обов'язкового калібрування, `api_list`/`api_detail`, безпечний курсор, події `api_cursor_saved` і `json_parse_failed`, без хибних tombstones в інкрементальному режимі)
- [x] T026 API і CLI: `CreateTaskRequest.api_source` у `backend/app/api/schemas.py` / `backend/app/api/tasks.py` і підтримка у `backend/app/cli.py`

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Оновити `specs/capabilities/` (`00-domain-model.md`, `01-task-planning.md`, `02-calibration.md`, `03-extraction.md`, `04-runs-and-collection.md`, `05-fetching.md`), додати `extraction/api_json.py` у «Код» і нові події в каталог
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (позначити `D-02 ✅`)
- [x] T042 Перенести `specs/changes/001-d02-open-api-source/` → `specs/changes/archive/` та оновити `specs/changes/README.md`
