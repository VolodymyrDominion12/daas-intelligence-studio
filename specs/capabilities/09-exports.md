# 09 — Експорт дата-продукту (CSV / XLSX / Parquet / JSON + маніфест)

| | |
|:---|:---|
| **Код** | `backend/app/storage/exports.py`, `backend/app/db/duckdb_store.py` (`export_rows`, `write_tabular_export`), `backend/app/api/data.py` (`/exports`), `backend/app/compliance.py` (`source_note`), `backend/app/utils.py` (`mask_value`, `mask_email`, `mask_phone`, `redact_pii`) |
| **API** | `POST /api/tasks/{id}/exports {format, mask_pii, limit}`, `GET /api/exports`, `GET /api/exports/{filename}` |
| **CLI** | `daas export <id> --format csv|xlsx|parquet|json [--mask/--no-mask] [--limit N]` |
| **Тести** | `tests/test_storage.py`, `tests/test_api_flow.py`, `tests/test_sql_safety.py`, `tests/test_utils.py`, `tests/test_compliance.py` |
| **Джерела** | `docs/02` §5.4, `docs/03` §2a (F0-01) |
| **Статус** | as-built v0.1 |

## 1. Призначення
Експорт — **дата-продукт**, а не дамп: поточний стан датасету + походження кожного рядка +
маніфест (схема, якість, свіжість, джерела, ліцензія, sha256) + гарантоване маскування PII.

## 2. Вимоги

### REQ-EXP-001 — Формати
Експорт **ПОВИНЕН** підтримувати рівно `csv`, `xlsx`, `parquet`, `json`; інший формат → 400
(API) / `ValueError` (функція). Без датасету → 404.
- **Перевірка:** `tests/test_storage.py::test_exports_csv_xlsx_parquet_json_and_masking`

### REQ-EXP-002 — Колонки і походження
Кожен рядок **ПОВИНЕН** містити `record_key`, колонки датасету в порядку spec, і `_source_url`,
`_observed_at`. `limit` обмежує кількість рядків.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (`record_key` у CSV)

### REQ-EXP-003 — Маскування PII
- Колонки з `pii="personal"` **ПОВИННІ** маскуватися **завжди**, навіть при `mask_pii=false`.
- `business_contact` маскуються при `mask_pii=true` (за замовчуванням).
- Колонка без мітки, але з `dtype` `email`/`phone`, вважається `business_contact`.
- Маски: email → `b***@domain`; телефон → `+380***33`; рядки → `redact_pii` (`[email]`, `[phone]`);
  списки маскуються поелементно.
- **Перевірка:** `tests/test_storage.py::test_exports_csv_xlsx_parquet_json_and_masking`,
  `tests/test_api_flow.py::test_full_pipeline`, `tests/test_utils.py::test_pii_masking`

### REQ-EXP-004 — Маніфест
Поруч із файлом **ПОВИНЕН** з'являтися `<file>.manifest.json` з: `dataset_id`, `dataset_name`,
`task_id`, `version`, `format`, `file`, `rows`, `generated_at`, `generator`, `freshness`, `quality`,
`columns` (key/title/dtype/pii/method), `provenance_columns`, `masked_pii`, `masked_columns`,
`sources`, `license`, `bytes`, `sha256`.
- **Перевірка:** `tests/test_storage.py::test_exports_csv_xlsx_parquet_json_and_masking`
  (наявність файлу маніфесту); склад полів — прогалина

### REQ-EXP-005 — Специфіка форматів
- **CSV:** UTF-8 з BOM (Excel відкриває кирилицю).
- **XLSX:** аркуш «Дані» (рядок 1 — людські назви, рядок 2 — ключі, закріплення, автофільтр),
  «Маніфест», «Колонки».
- **Parquet:** типізовані колонки через DuckDB (ZSTD).
- **JSON:** масив рядків.
- **Перевірка:** `tests/test_storage.py::test_exports_csv_xlsx_parquet_json_and_masking`,
  `tests/test_sql_safety.py::test_files_with_quotes_in_path`

### REQ-EXP-006 — Джерела з живого реєстру
`manifest.sources` **ПОВИНЕН** будуватися `source_note` з **живого** реєстру:
`домен (tos_risk/license, дата перевірки | not reviewed)` для кожного унікального домену seed-ів.
- **Перевірка:** `tests/test_compliance.py::test_source_note_uses_live_registry`

### REQ-EXP-007 — Захист від formula injection
Текстові значення, що починаються з `=`, `+`, `-`, `@`, **ПОВИННІ** експортуватися в CSV/XLSX з
префіксом `'` (крім колонок `phone`, `number`, `integer`).
- **Перевірка:** — (прогалина: поведінка задокументована в `docs/03` §2a, тесту немає)

### REQ-EXP-008 — Атомарний запис, безпечні імена
Файл **ПОВИНЕН** писатися у тимчасовий і атомарно перейменовуватися; ім'я
`<safe_name>-v<version>-<UTC stamp>-<6 hex>.<fmt>`. Завантаження `GET /api/exports/{filename}`
**ПОВИННО** відхиляти імена з шляхом (400) і файли поза каталогом експортів (404). Список
експортів — до 100 найновіших, без `.manifest.json`.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (завантаження); path traversal —
  прогалина

### REQ-EXP-009 — Подія експорту
Успішний експорт **ПОВИНЕН** писати `export_created` (файл, рядки, позначка про маскування);
збій запису → 500 «Експорт не вдався: …».
- **Перевірка:** — (прогалина)

## 3. Прогалини
REQ-EXP-007 (formula injection), REQ-EXP-008 (path traversal), REQ-EXP-009, склад маніфесту.
