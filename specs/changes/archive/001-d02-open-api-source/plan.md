# 001 — План реалізації: Конектор відкритого JSON API (`D-02`)

> Plan описує **ЯК**. Посилається на [`spec.md`](./spec.md); кожна `REQ-*` має тут конкретне
> місце реалізації.

## 1. Підсумок рішення

Додаємо в `TaskSpec` опційну декларативну конфігурацію `ApiSourceSpec` (профіль для
OpenProcurement — `Prozorro` та `Prozorro.Продажі`, а також будь-які інші публічні REST JSON API)
і окремий детермінований модуль JSON-екстракції `app/extraction/api_json.py`. Завантаження JSON
йде через існуючий `HttpFetcher` + `PolicyGuardFetcher` із прапором `allow_json=True`, тож
зберігаються SSRF-захист, редирект-guard, `robots.txt`, реєстр джерел, rate-limit, retry і raw-кеш.

Пагінація і деталізація працюють поверх уже наявної стійкої черги `jobs` (використовуючи її
колонку `kind`: `api_list` / `api_detail`). Стан інкрементального курсора (`api_state` у SQLite,
міграція №4) оновлюється лише після успішної фіксації версії датасету без помилок спискових
сторінок; в інкрементальному режимі tombstones вимикаються (`full_crawl=False`), щоб не видаляти
активні незмінені тендери.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | Ведемо `spec.md` → `plan.md` → `tasks.md` → тести → код → sync `capabilities/`. |
| II. Детермінованість, LLM лише fallback | ✅ | Декларативний dot-path мапінг (`api_json`), 0 викликів LLM на плануванні пресету та зборі. |
| III. Тести офлайн | ✅ | JSON-фікстури OpenProcurement у `backend/tests/fixtures/` + `FixtureFetcher` / `httpx.MockTransport`. |
| IV. Походження, append-only | ✅ | Кожне поле має `Provenance(method="api_json", confidence=0.98, verified=True, source_url=final_url)`; запис іде в DuckDB append-only; стабільний ключ `api:<id>`. |
| V. Комплаєнс і PII | ✅ | `PolicyGuardFetcher` обгортає кожен API-запит (список + деталі + пагінація), `assert_logged_out` перевіряє `seeds` і `detail_url_template`, `pii="personal"` у профілі відсутнє. |
| VI. Local-first, захищений API | ✅ | Усе локально в SQLite/DuckDB/RawCache; жодних зовнішніх сервісів. |
| VII. Події для змін даних | ✅ | Нова подія `api_cursor_saved` (і `json_parse_failed`) фіксуються в `events` та каталозі `00-domain-model.md`. |
| VIII. Типізовані межі, міграції | ✅ | `ApiSourceSpec` у `app/models.py`, опційне поле у `CreateTaskRequest`; forward-only міграція №4 SQLite (`api_state`); старі бази відкриваються без помилок. |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових залежностей (вистачає стандартної бібліотеки `json` + `urllib.parse` + існуючий `httpx`). |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Моделі | `backend/app/models.py` | `Method` += `"api_json"`; `TaskSpec.extraction` += `"api_json"`; модель `ApiSourceSpec`; збереження query у `seeds` (`drop_query=False`), коли задано `api_source`; перевірка `assert_logged_out` для `detail_url_template` | `REQ-DM-009` |
| Сховище | `backend/app/db/sqlite_store.py` | Міграція №4: таблиця `api_state(task_id, next_cursor, next_url, last_modified, updated_at)`; методи `get_api_state`, `save_api_state`, `reset_api_state` | `REQ-RUN-029` |
| Fetching | `backend/app/scraping/fetcher.py`, `backend/app/compliance.py` | Параметр `allow_json: bool = False` у `Fetcher.fetch`, `HttpFetcher._attempt`, `PolicyGuardFetcher.fetch`; `Accept: application/json` та допуск `application/json` / `+json` у `Content-Type` при `allow_json=True`; JSON-тіло зберігається в `PageContent.html` і `text` без HTML-парсингу | `REQ-FETCH-006`, `REQ-FETCH-011` |
| Екстракція | `backend/app/extraction/api_json.py` (новий) | Dot-path / `a|b` резолвер, `extract_json_list_targets(...)` (список ID + URL пагінації + курсор), `extract_json_records(...)` → `ExtractedRecord` з ключем `api:<id>` та `Provenance(method="api_json")` | `REQ-EXT-010` |
| Планування | `backend/app/presets.py`, `backend/app/pipeline/planner.py` | Профіль `openprocurement` (Prozorro + Prozorro.Продажі): розпізнавання за seed-доменом (`public.api.openprocurement.org`, `prozorro.gov.ua`, `prozorro.sale`) або ключовими словами (`prozorro api`, `openprocurement`, `prozorro.продажі api`), формування колонок без `pii="personal"` і `ApiSourceSpec`; збереження `api_source` при merge з LLM | `REQ-PLAN-012` |
| Пайплайн | `backend/app/runtime.py`, `backend/app/pipeline/runner.py` | Пропуск перевірки калібрування у `Studio.collect` при `spec.api_source is not None`; у `PipelineRunner.collect` — старт зі збереженого курсора (коли `incremental=True`), обробка `kind="api_list"` (`force_refresh=True`, постановка `api_detail` і наступної сторінки пагінації в межах `max_pages`) та `kind="api_detail"`, збереження курсора лише після успішної фіксації версії без failed/skipped list-сторінок, вимкнення tombstones у інкрементальному режимі | `REQ-RUN-029`, `REQ-RUN-030`, `REQ-CAL-008` |
| API / CLI | `backend/app/api/schemas.py`, `backend/app/api/tasks.py`, `backend/app/cli.py`, `backend/app/runtime.py` | Опційне поле `api_source: ApiSourceSpec | None = None` у `CreateTaskRequest` та `Studio.create_task(...)`; опція `--api-source` (JSON-файл або шлях) або авто-визначення за пресетом у `daas task create`; підказка `next: daas collect <id>` замість `calibrate` для API-задач | `REQ-DM-009`, `REQ-PLAN-012`, `REQ-RUN-030` |

## 4. Дані та міграції

- **SQLite міграція №4** у `MIGRATIONS` (`backend/app/db/sqlite_store.py`):
  ```sql
  CREATE TABLE api_state (
      task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
      next_cursor TEXT,
      next_url TEXT,
      last_modified TEXT,
      updated_at TEXT NOT NULL
  );
  ```
- **Зворотна сумісність:**
  - Існуючі задачі в `tasks.spec_json` без поля `api_source` валідуються з `api_source=None` (значення за замовчуванням).
  - Існуючі SQLite БД версії `user_version=3` автоматично мігрують на `4` у `_migrate()`. Додаємо тест відкриття БД версії 3.
  - DuckDB схема не змінюється.

## 5. Контракти

- **API (`POST /api/tasks`):** `CreateTaskRequest` приймає додаткове опційне поле `api_source: ApiSourceSpec | None = None` (зворотно сумісне: старі запити без цього поля працюють як раніше).
- **API (`POST /api/tasks/{id}/run`):** `RunRequest` без змін; для задач із `spec.api_source is not None` не повертає HTTP 409 при відсутності калібрування (`REQ-RUN-030`). Опційно підтримує `full_refresh`? Ні — у 001 не додаємо зайвих прапорів до `RunRequest`, режим контролюється через `spec.api_source.incremental`.
- **Нові події в журналі** (додати в каталог `specs/capabilities/00-domain-model.md`):
  - `api_cursor_saved` (`info`, `runner`) — збережено інкрементальний курсор/URL пагінації після успішної фіксації версії (`data.cursor`, `data.next_url`).
  - `json_parse_failed` (`warning`, `runner`) — не вдалося розібрати JSON-відповідь API (`data.url`, `data.error`).
- **Налаштування `DAAS_*`:** нових env-змінних не потрібно.

## 6. Ризики і відкат

| Ризик | Як помітимо | Мітигація / відкат |
|:---|:---|:---|
| `TaskSpec._clean_seeds` обрізає query-параметри у API seed-а (`?descending=1`) | Юніт-тест `ApiSourceSpec` на seed із query-параметрами | Нормалізувати `seeds` з `drop_query=False`, коли `api_source is not None` (але `assert_logged_out` все одно перевіряє query на токени!) |
| Пагінація зациклиться на однаковому `next_page.uri` / курсорі | Тест пагінації з однаковим курсором на 2-й сторінці | Зупиняти постановку наступної сторінки, якщо `next_url == current_url` або список елементів порожній, або `next_url` уже був поставлений у цьому запуску (`UNIQUE(run_id, url)` уже захищає, плюс явна перевірка курсора) |
| Стрибок курсора вперед при частковій помилці 429/5xx | Негативний тест: 1-ша сторінка ОК, 2-га дає 500 → курсор не змінився | Оновлювати `api_state` лише після `_finish_dataset` і лише якщо жодна `api_list`-задача не завершилась зі статусом `failed`/`skipped` |
| Інкрементальний API-прогін запускає tombstones і видаляє незмінені лоти | Тест: повторний інкрементальний прогін з 1 новим лотом не видаляє старі | Для `api_source.incremental=True` `full_crawl` завжди `False`, tombstones пропускаються |
| `FixtureFetcher` у наявних тестах приймає лише `(url, *, force_refresh=False)` | Помилка `TypeError: unexpected keyword argument 'allow_json'` | Оновити `FixtureFetcher.fetch(..., allow_json: bool = False)` і в `PolicyGuardFetcher` передавати `allow_json` лише якщо сигнатура внутрішнього fetcher-а його приймає (або оновити `Fetcher` Protocol і `FixtureFetcher`) |

## 7. Тестова стратегія (без мережі)

1. **Фікстури в `backend/tests/fixtures/`:**
   - `prozorro_list_page1.json` — сторінка 1 OpenProcurement (2 елементи, `next_page` з `offset` і `uri`).
   - `prozorro_list_page2.json` — сторінка 2 OpenProcurement (порожній `data: []` або 1 елемент + кінець пагінації).
   - `prozorro_tender_001.json`, `prozorro_tender_002.json` — деталі тендера / аукціону (ідентифікатор, назва, замовник, ЄДРПОУ, CPV, регіон, сума, валюта, статус, `dateModified`, `tenderPeriod.endDate`).
   - `broken_api.json` — невалідний JSON для тесту відмови.
2. **Офлайн-тести (`backend/tests/test_open_api_source.py`):**
   - `REQ-DM-009`: валідна `TaskSpec` з `api_source` зберігає query у seed-URL; невалідна (порожній `field_map` або токен у `detail_url_template`) відхиляється.
   - `REQ-PLAN-012`: `Planner` будує OpenProcurement-план із промпту без LLM і без `pii="personal"`.
   - `REQ-FETCH-006`, `REQ-FETCH-011`: `HttpFetcher` з `httpx.MockTransport` — `allow_json=True` приймає `application/json`, без `allow_json` відхиляє як `permanent`; `PolicyGuardFetcher` блокує `forbidden` API-домен і приватний редирект.
   - `REQ-EXT-010`: `extract_json_records` витягає поля, ставить ключ `api:<id>`, `Provenance(method="api_json")`, а на битому JSON повертає 0 записів із попередженням.
   - `REQ-RUN-029`, `REQ-RUN-030`, `REQ-CAL-008`: повний офлайн E2E прогін через `Studio` (`POST /api/tasks` → `POST /api/tasks/{id}/run` без калібрування) по двох сторінках списку + деталях; збереження курсора після успішного прогону; повторний прогін з нового курсора ловить `price_drop` / `field_changed` і НЕ видаляє старі записи (`tombstones`); негативний тест — при 500 на 2-й сторінці курсор не зсувається.
   - **Міграція №4:** відкриття штучно створеної SQLite БД з `user_version=3` і перевірка, що `api_state` створюється і працює без втрати старих задач.
