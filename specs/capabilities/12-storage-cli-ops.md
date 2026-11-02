# 12 — Конфігурація, сховище, CLI, бекап, демо

| | |
|:---|:---|
| **Код** | `backend/app/config.py` (`Settings`, `Paths`), `backend/app/db/sqlite_store.py` (`MIGRATIONS`, `backup_to`, `overview`), `backend/app/storage/backup.py`, `backend/app/storage/__init__.py`, `backend/app/cli.py`, `backend/app/demo.py`, `backend/app/__init__.py`, `.env.example`, `Makefile`, `.github/workflows/ci.yml`, `.gitignore` |
| **Тести** | `tests/test_raw_cache_backup.py`, `tests/test_api_flow.py`, `tests/test_package_integrity.py`, `tests/test_storage.py` |
| **Джерела** | `docs/01` §2 (local-first), `docs/03` §2a (інцидент Фази 0), ADR-02 |
| **Статус** | as-built v0.1 |

## 1. Розкладка сховища

```text
<DAAS_STORAGE_DIR, за замовчуванням <repo>/storage>/
├── operational.sqlite      # tasks, runs, jobs, events, ledger, registry, calibrations, misses
├── analytics.duckdb        # record_versions, quarantine, current_records
├── raw_cache/<xx>/<sha256>.json.zst
├── snapshots/dataset=<slug>/version=<NNNN>/{part.parquet, manifest.json}
├── exports/<file> + <file>.manifest.json
├── backups/*.zip
├── llm_cache/
├── logs/
└── runtime.json            # API-токен (0600)
```

## 2. Вимоги — конфігурація

### REQ-OPS-001 — Усе через env `DAAS_*`
Налаштування **ПОВИННІ** читатися з env з префіксом `DAAS_` і файлів `<repo>/.env`,
`<repo>/backend/.env`; невідомі ключі ігноруються. Кожне нове налаштування **ПОВИННО** з'являтися в
`.env.example` з коментарем у тій самій зміні. Відносний `storage_dir` — відносно кореня репо.
- **Перевірка:** — (прогалина)

### REQ-OPS-002 — Офлайн-режим
`DAAS_OFFLINE=true` **ПОВИНЕН** вимикати LLM у зборі й первинне навчання схеми на сторінці, якщо
схеми немає (детерміноване «лікування» після промаху лишається); демо і тести працюють без мережі.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (офлайн-налаштування фікстури)

## 3. Вимоги — SQLite

### REQ-OPS-010 — Forward-only міграції
`OperationalStore` **ПОВИНЕН** працювати в WAL і застосовувати `MIGRATIONS` (зараз 1–3) по порядку
один раз; нова таблиця/колонка — лише новою міграцією з наступним номером, ніколи не редагуванням
старої.
- **Перевірка:** `tests/test_storage.py::test_queue_lease_retry_and_cancel` (схема працює);
  апгрейд старого файлу SQLite — прогалина

## 4. Вимоги — бекап

### REQ-OPS-020 — Узгоджений zip-бекап
`create_backup` **ПОВИНЕН** створювати zip з: `operational.sqlite` (через online backup API —
узгоджено при активному WAL), `analytics.duckdb` (після checkpoint), `snapshots/`,
`backup-manifest.json` (дата, версія, файли, інструкція відновлення); `raw_cache/` — лише з
`include_cache=true`. Запис через тимчасовий файл.
- **Перевірка:** `tests/test_raw_cache_backup.py::test_backup_bundle_contents`

### REQ-OPS-021 — Бекап під час роботи студії
`POST /api/maintenance/backup?include_cache` **ПОВИНЕН** робити бекап без зупинки студії і писати
подію `backup_created` (`task_id="system"`); збій → 500.
- **Перевірка:** `tests/test_api_flow.py::test_backup_endpoint`

## 5. Вимоги — CLI

### REQ-OPS-030 — Команди
`daas` **ПОВИНЕН** надавати: `init`, `serve [--host --port --reload]`, `demo [--serve --storage
--port]`, `doctor`, `task create|list|show`, `calibrate`, `collect [--pages]`, `enrich [--column]`,
`snapshot`, `export [--format --mask/--no-mask --limit]`, `duplicates [--threshold]`, `changes`,
`backup [--out --cache]`. `calibrate/collect` підтримують `--ack-high-risk --reason`.
- **Перевірка:** — (прогалина: немає тестів CLI через `typer.testing.CliRunner`)

### REQ-OPS-031 — Коди виходу
Запускові команди **ПОВИННІ** завершуватися: `2` — конфлікт запуску; `3` — комплаєнс (з підказкою
`--ack-high-risk --reason` для `needs_ack`); `1` — інша помилка.
- **Перевірка:** — (прогалина)

### REQ-OPS-032 — doctor
`daas doctor` **ПОВИНЕН** виводити JSON із перевірками: сховище доступне на запис, SQLite, DuckDB,
статистика кешу, `llm_ready`, пошук, чи зібрано фронтенд.
- **Перевірка:** — (прогалина)

## 6. Вимоги — демо

### REQ-OPS-040 — Офлайн-демо з живим джерелом
`daas demo` **ПОВИНЕН** згенерувати локальний сайт-каталог клінік (з robots.txt і сайтами клінік),
підняти його на loopback, прогнати калібрування → збір → збагачення → снапшот → XLSX з
налаштуваннями: offline, LLM вимкнено, приватна мережа дозволена, robots поважається.
`--serve` лишає студію і джерело запущеними.
- **Перевірка:** — (прогалина: покрито лише вручну; `test_full_pipeline` проходить той самий шлях
  на фікстурах)

## 7. Вимоги — цілісність пакета

### REQ-OPS-050 — Кожен модуль імпортується і не схований `.gitignore`
Пакет `app` **ПОВИНЕН** містити очікувані підпакети; кожен модуль `app.*` імпортується; жоден
`.py` під `backend/app` не ігнорується git-ом (урок інциденту Фази 0).
- **Перевірка:** `tests/test_package_integrity.py::test_package_has_expected_subpackages`,
  `tests/test_package_integrity.py::test_every_module_imports`,
  `tests/test_package_integrity.py::test_source_files_are_not_gitignored`

### REQ-OPS-051 — CI
CI **ПОВИНЕН** на кожен push/PR запускати: backend — `uv sync --extra dev`, `ruff check app tests`,
`pytest -q` (Python 3.12); frontend — `npm ci`, `npm run build` (Node 22).
- **Перевірка:** `.github/workflows/ci.yml` (конфігурація)

## 8. Відомі обмеження
- Одна студія на сховище: `daas serve` тримає DuckDB; CLI-команди, що пишуть, — лише після зупинки.
- DuckDB-схема — `CREATE IF NOT EXISTS` + `_migrate`; складні зміни потребують міграційного скрипта.
