# 08 — Історія записів, снапшоти, детекція змін, tombstones

| | |
|:---|:---|
| **Код** | `backend/app/db/duckdb_store.py` (`AnalyticsStore`), `backend/app/pipeline/runner.py` (`snapshot`, `_write_snapshot_and_diff`, `_emit_changes`, `_apply_tombstones`, `rollback_removals`), `backend/app/db/sqlite_store.py` (`dataset_versions`, `record_misses`) |
| **API** | `GET /api/tasks/{id}/records?q&sort&dir&limit&offset&f_<key>`, `GET /api/tasks/{id}/records/{key}`, `GET /api/tasks/{id}/dataset`, `POST /api/tasks/{id}/snapshot`, `GET /api/tasks/{id}/changes` |
| **CLI** | `daas snapshot <id>`, `daas changes <id>` |
| **Тести** | `tests/test_storage.py`, `tests/test_tombstones.py`, `tests/test_sql_safety.py`, `tests/test_api_flow.py` |
| **Джерела** | ADR-02, ADR-05, `docs/03` §3.3 (P1-03) |
| **Статус** | as-built (P1-03 ✅) |

## 1. Призначення
Історія, яку конкурент не спарсить заднім числом: append-only версії кожного запису, поточний стан,
Parquet-снапшоти на версію датасету і події змін між версіями. Видалення — лише консервативні
tombstones.

## 2. Модель зберігання (DuckDB)
- `record_versions(dataset_id, record_key, dataset_version, revision, run_id, source_url,
  content_hash, payload JSON, provenance JSON, observed_at, deleted)` — **лише INSERT** (виняток:
  відкат tombstones незавершеного запуску).
- `current_records` (view) — остання ревізія кожного ключа, без `deleted`.
- `quarantine` — підозрілі записи (заготовка, P1-08).
- Один процесний конекшн під lock (ADR-02).

## 3. Вимоги — історія і запити

### REQ-HIST-001 — Append-only і поточний стан
Кожне спостереження запису **ПОВИННО** додаватися новою ревізією; `current_records` показує останню
ревізію кожного ключа; старі ревізії доступні в `record_history`.
- **Перевірка:** `tests/test_storage.py::test_record_versions_current_view_and_diff`

### REQ-HIST-002 — Запити до записів безпечні щодо SQL
`query_records` **ПОВИНЕН**: шукати `q` по всьому payload (ILIKE); фільтрувати `f_<key>=значення`
(ILIKE по JSON-полю); сортувати за колонкою з урахуванням типу (числа — `DOUBLE`, дати —
`TIMESTAMP`, інше — `lower()`), `NULLS LAST`, з тай-брейком за `record_key` у тому самому напрямку;
пагінація `limit ≤ 500`, `offset`. Усі значення та JSON-шляхи — **зв'язані параметри**; ключі
колонок проходять `_safe_key` (лише `[A-Za-z0-9_]`), невідомий ключ сортування → порядок за
замовчуванням.
- **Перевірка:** `tests/test_sql_safety.py::test_quotes_in_filter_values_are_data`,
  `tests/test_sql_safety.py::test_unsafe_column_keys_are_rejected`,
  `tests/test_sql_safety.py::test_fill_rates_with_bound_paths`,
  `tests/test_api_flow.py::test_full_pipeline` (`f_city`)

### REQ-HIST-003 — Якість = заповненість
`field_fill_rates` **ПОВИНЕН** повертати % непорожніх значень кожної не-AI колонки в поточному стані.
Точність (precision/recall) у v0.1 не вимірюється (P1-01).
- **Перевірка:** `tests/test_sql_safety.py::test_fill_rates_with_bound_paths`

## 4. Вимоги — снапшоти і зміни

### REQ-HIST-010 — Снапшот = стан на версію
Снапшот версії `v` **ПОВИНЕН** містити рівно по одному рядку на ключ — останню ревізію з
`dataset_version ≤ v`, без tombstones — і записуватися в
`snapshots/dataset=<slug>/version=<NNNN>/part.parquet` + `manifest.json` (≤ 500 змін).
Шляхи передаються в DuckDB як параметри (лапки в шляху безпечні).
- **Перевірка:** `tests/test_storage.py::test_diff_detects_new_entities_and_price_drops`,
  `tests/test_tombstones.py::test_snapshot_contains_current_state_only`,
  `tests/test_sql_safety.py::test_files_with_quotes_in_path`

### REQ-HIST-011 — Diff між версіями
`diff_versions(from, to)` **ПОВИНЕН** повертати: `new_entity` (ключ з'явився), `removed_entity`
(зник), `field_changed` (для кожної колонки зі зміненим значенням; для цінових колонок —
`delta_pct`), і додатково `price_drop`, якщо `delta_pct ≤ −DAAS_PRICE_DROP_THRESHOLD_PCT` (5%).
Колонки збагачення, відсутні в обох версіях, ігноруються.
- **Перевірка:** `tests/test_storage.py::test_diff_detects_new_entities_and_price_drops`,
  `tests/test_api_flow.py::test_full_pipeline`

### REQ-HIST-012 — Події змін
Після збору/збагачення **ПОВИННА** писатися подія `changes_detected` (зведення за типами) і до 25
подій окремих змін (`price_drop` — `warning`). Окремий `POST /snapshot` перезаписує снапшот
поточної версії і рахує зміни, але **не** пише події змін.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`,
  `tests/test_tombstones.py::test_full_crawl_marks_missing_record_removed`

### REQ-HIST-013 — Снапшот без даних
`POST /snapshot` без жодної версії датасету **ПОВИНЕН** завершити запуск `failed` з подією
`run_failed` «Немає даних для снапшота».
- **Перевірка:** — (прогалина)

## 5. Вимоги — tombstones (P1-03)

> Принцип: хибне «видалено» гірше за запізніле.

### REQ-HIST-020 — Tombstone — це версія
Видалення **ПОВИННО** бути новою ревізією з `deleted=TRUE` і останнім payload; запис зникає з
поточного стану, снапшотів і diff (→ `removed_entity`), історія зберігається; повторна поява
запису — `new_entity`.
- **Перевірка:** `tests/test_tombstones.py::test_tombstone_hides_record_but_keeps_history`

### REQ-HIST-021 — Лише після повного проходу
Промахи рахуються **лише** якщо прохід повний: не скасовано, не `queue stalled`, немає `failed`/
`skipped` задач, і немає залишку в черзі після ліміту сторінок (ліміт без залишку — повний прохід).
Інакше — подія `tombstones_skipped`, `manifest.full_crawl=false`.
- **Перевірка:** `tests/test_tombstones.py::test_partial_crawl_never_removes`,
  `tests/test_tombstones.py::test_page_limit_without_remaining_work_is_a_full_crawl`

### REQ-HIST-022 — Лише записи з перевіреними джерелами
Кандидатом **ПОВИНЕН** бути лише запис, якого не бачили в цьому запуску, востаннє спостережений у
старшій версії, і **кожна** відома сторінка-джерело якого була завантажена свіжо (не з кешу) у
цьому запуску й дала записи.
- **Перевірка:** `tests/test_tombstones.py::test_unchecked_known_source_blocks_removal`,
  `tests/test_tombstones.py::test_cached_page_is_not_evidence`

### REQ-HIST-023 — Пільговий період
Промах **ПОВИНЕН** рахуватися раз на запуск; запис видаляється після
`DAAS_TOMBSTONE_AFTER_MISSES` (1) послідовних придатних промахів; поява запису скидає лічильник.
- **Перевірка:** `tests/test_tombstones.py::test_misses_count_once_per_run`,
  `tests/test_tombstones.py::test_grace_period_counts_only_eligible_misses`

### REQ-HIST-024 — Запобіжник масового видалення
Якщо кандидатів більше за `DAAS_TOMBSTONE_MAX_RATIO` (0.5) датасету, видалення **НЕ ПОВИННО**
відбутися; подія `tombstones_suspicious` (warning).
- **Перевірка:** `tests/test_tombstones.py::test_mass_removal_is_refused`

### REQ-HIST-025 — Оптимістична перевірка ревізії
Tombstone **ПОВИНЕН** ставитися лише якщо поточна ревізія ключа досі та, що бачили під час відбору
(запис, повторно спостережений паралельним запуском, не ховається).
- **Перевірка:** `tests/test_tombstones.py::test_tombstone_skips_records_reobserved_meanwhile`

### REQ-HIST-026 — Відкат незавершеного запуску
Якщо запуск не зафіксував версію датасету (виняток, перезапуск процесу), його tombstones і
лічильники промахів **ПОВИННІ** відкочуватися.
- **Перевірка:** `tests/test_tombstones.py::test_rollback_tombstones_of_unfinished_run`,
  `tests/test_tombstones.py::test_failed_finish_rolls_back_removals`

### REQ-HIST-027 — Успішне видалення
Видалені записи **ПОВИННІ** давати `stats.records_removed`, подію `records_removed` (≤ 50 ключів) і
`manifest.removed`.
- **Перевірка:** `tests/test_tombstones.py::test_full_crawl_marks_missing_record_removed`

## 6. Вимоги — міграції

### REQ-HIST-030 — Старі файли DuckDB відкриваються
`_migrate` **ПОВИНЕН** forward-only доводити старі файли до поточної схеми (зокрема додавати
`deleted`, обходячи заборону DuckDB на `ALTER` таблиці з індексом).
- **Перевірка:** `tests/test_tombstones.py::test_old_database_is_migrated`

## 7. Відкриті питання
- Невалідний ключ у `f_<key>` дає `ValueError` у сховищі → **HTTP 500** на API (немає мапінгу
  `ValueError → 400`). Має бути 400 «некоректна колонка» — потрібен тест на рівні API і фікс.
