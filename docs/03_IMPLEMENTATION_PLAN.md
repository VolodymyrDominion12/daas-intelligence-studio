# План реалізації та поточний статус

> Документ описує, **як** концепція з [`01_CONCEPT_AND_ARCHITECTURE.md`](./01_CONCEPT_AND_ARCHITECTURE.md)
> реалізується з урахуванням покращень із [`02_CONCEPT_REVIEW_AND_IMPROVEMENTS.md`](./02_CONCEPT_REVIEW_AND_IMPROVEMENTS.md).
> Статус: **v0.1 — вертикальний зріз працює end-to-end** (план → калібрування → збір → збагачення →
> снапшот → експорт → UI).

---

## 1. Стратегія

1. **Walking skeleton first.** Один наскрізний шлях, який реально працює й перевіряється, цінніший за
   п'ять напівготових модулів.
2. **Детермінованість за замовчуванням.** CSS/JSON-LD — основа; LLM — каскадний fallback із бюджетом.
3. **Кожен етап завершується демо + тестами.** `daas demo` проходить увесь пайплайн офлайн, без мережі.
4. **Дані продаються разом із походженням.** Кожне поле має `method`, `confidence`, `source_url`;
   експорт супроводжується маніфестом і ліцензією.
5. **P0-вимоги з ревʼю вбудовані в архітектуру, а не в TODO:** robots, ToS-реєстр, SSRF-захист,
   PII-мінімізація, бюджет LLM, захист від prompt injection.

---

## 2. Що вже реалізовано (v0.1)

| Можливість | Код | Як перевірено |
|:---|:---|:---|
| План задачі з промпту (LLM + офлайн-шаблони) | `app/pipeline/planner.py`, `app/presets.py` | `test_api_flow.py`, офлайн-демо |
| Калібрування: 2-4 сторінки → CSS-схема | `app/extraction/learner.py`, `app/pipeline/calibration.py` | `test_learner.py` (каталог + сторінка деталі) |
| JSON-LD-екстракція (schema.org) | `app/extraction/jsonld.py` | `test_extraction.py` |
| CSS-екстракція + fallback по `itemprop`/класах | `app/extraction/css.py`, `app/extraction/engine.py` | `test_extraction.py`, `test_learner.py` |
| LLM-fallback із захистом від injection | `app/llm/guard.py`, `app/extraction/llm_extractor.py` | `test_llm.py` |
| Каскад LLM, кеш, бюджет, ledger | `app/llm/router.py`, `app/llm/providers.py` | `test_llm.py` |
| Черга з lease, retry, cancel, resume-після-краху | `app/db/sqlite_store.py` | `test_storage.py` |
| Append-only історія + `current_records` | `app/db/duckdb_store.py` | `test_storage.py` |
| Снапшоти Parquet + diff (new/field_changed/price_drop) | `app/pipeline/runner.py` | `test_storage.py`, `test_api_flow.py` |
| Waterfall-збагачення: аудит сайту, контакти, технології | `app/enrichment/site_audit.py`, `tech_detect.py` | `test_api_flow.py` |
| AI-колонки з промптом | `app/enrichment/ai_column.py` | інтеграція через API/UI (потребує LLM) |
| Пошук офіційного сайту (SearXNG) | `app/enrichment/domain_resolver.py`, `app/discovery.py` | потребує `DAAS_SEARXNG_URL` |
| Дедуплікація: кандидати + blocking | `app/enrichment/dedupe.py` | `test_utils.py` |
| Експорт CSV/XLSX/Parquet/JSON + маніфест | `app/storage/exports.py` | `test_storage.py`, `test_api_flow.py` |
| PII: маскування, редагування перед cloud LLM, personal завжди маскується | `app/utils.py`, `app/storage/exports.py` | `test_utils.py`, `test_storage.py` |
| Root-robots.txt, rate limit, retry taxonomy, SSRF guard | `app/scraping/*`, `app/utils.py` | `test_utils.py` |
| REST API + SSE + Bearer/Host guard + CSP | `app/api/*`, `app/main.py` | `test_api_flow.py` |
| UI: Studio / Data Grid / Calibration / Changes / Export / Sources | `frontend/src/**` | headless Chrome (скріншоти в `docs/assets/`) |
| Офлайн-демо з живим джерелом | `app/demo.py`, `daas demo --serve` | ручний прогін + скріншоти |
| Міграції SQLite, бекап, retention purge | `app/db/sqlite_store.py`, `daas backup`, `/api/maintenance/purge-cache` | частково тестами |

**Стан якості на v0.1:** 55 тестів (pytest, повністю офлайн), `ruff` без зауважень, frontend
`tsc` + `vite build` без помилок. Час демо-пайплайну: кілька секунд на 4 записи.

### Критерії приймання v0.1 (виконані)

- [x] Від промпту до валідного XLSX без ручного коду — у демо.
- [x] Калібрування показує прев'ю та збережену схему (container + селектори + coverage).
- [x] Масовий збір не викликає LLM, якщо є детермінована схема.
- [x] Повторний збір **не втрачає** збагачені поля (регресійний тест).
- [x] Кожен запис має походження; експорт має маніфест і ліцензію.
- [x] LLM-бюджет, кеш, ліміт викликів на прогін; витрати видно в UI.
- [x] Локальний API закритий токеном; приватні/loopback цілі заблоковані для зовнішніх URL.
- [x] Тести не ходять у мережу.

---

## 3. Етап 2 (P1): від «працює» до «можна продавати» (2-3 тижні)

### 3.1. Якість даних і eval-харнес (найвищий пріоритет)

**Проблема:** зараз є лише «заповненість полів», а не точність. Без цього не можна ні гарантувати
якість, ні призначати ціну.

- `gold/<task-type>/` — 20-50 розмічених сторінок на тип джерела.
- `daas eval --task <id> --gold <path>` → precision/recall/F1 по полях, coverage, % LLM-fallback,
  вартість на 1000 записів.
- Метрики зберігаються в `dataset_versions.manifest` → бейдж у UI («якість 96% на gold v3»).
- CI-регресія: зміна learner/промптів не може погіршити F1 нижче порогу.

**Готово, коли:** демо-датасет має eval-метрику; є тест, що падає при деградації витягування.

### 3.2. Entity Resolution v2

- Таблиця `entity_links` (pairwise score, rule, reviewer) + canonical record.
- Merge policy: яке джерело перемагає при конфлікті (свіжість / довіра / повнота).
- UI-черга «схвальні дублікати» з кнопками merge/unmerge (оборотність обов'язкова).
- Інкрементальний режим: новий запис порівнюється з canonical-кандидатами, а не повний batch.
- Splink — окремим batch-джобом для великих датасетів, результати матеріалізуються в `entity_links`.

**Готово, коли:** можна злити 2 записи, побачити merged payload, роз'єднати без втрати історії.

### 3.3. Розклад і моніторинг

- APScheduler у процесі воркера: `cron` на задачу (щодня/щотижня), пропуск якщо run активний.
- Tombstone-семантика: повний прогін знає очікуваний набір URL; записи, яких не зустріли, отримують
  `removed_at`, а тригери `removed_entity` стають справжніми.
- Ноутіфікації: Telegram/email/webhook на `price_drop`, `new_entity`, `run_failed`.
- Retry-політика для розкладу: доганяючий прогін після пропуску.

**Готово, коли:** щоденний прогін створює нову версію, а дельта містить коректні `removed_entity`.

### 3.4. Стійкість краулера

- Абстракція `ProxyProvider` (один/пул, sticky session, health-score, ban detection, cost accounting).
- Circuit breaker на домен: після N `blocked` — пауза й fallback на API/дзеркало.
- `PlaywrightFetcher` як сайдкар-контейнер; детекція «порожній HTML без контенту» → JS-рендер.
- Пагінація: `?page=N`, `next`-links, `sitemap.xml` як frontier; пріоритети URL.
- Cookie/consent-банер: детерміновані правила + optional LLM-підказка (без кліків наосліп).

### 3.5. Комплаєнс-енфорсмент (зараз реєстр є, блокування — ні)

- `tos_risk=forbidden` → hard stop на рівні `Studio.collect`, з audit-записом.
- `tos_risk=high` → явне підтвердження користувача (`force=true` + причина).
- `ColumnSpec.pii` перевіряється перед експортом: `personal` не потрапляє у файл навіть при
  `mask_pii=false` (вже зроблено) і не йде у cloud LLM (вже зроблено через redaction).
- DSAR: `daas dsar --email ... --delete` (пошук по всіх версіях + raw cache + snapshots).
- Retention job: TTL для raw cache і PII-полів, журнал видалень.
- Audit export: хто/коли/які джерела/які поля.

### 3.6. Спостережуваність і витрати

- JSON-логи у `storage/logs/` з ротацією; `run_id` у кожному рядку.
- `/api/metrics` (Prometheus text): pages, blocked, records, llm_usd, queue depth.
- Атрибуція витрат планування до задачі (зараз plan-виклик іде до створення task).
- Дашборд: витрати на 1000 записів, hit-rate кешу, середня кількість LLM-fallback на прогін.

### 3.7. UX-борг

- Редактор spec: перейменування/видалення/перестановка колонок, зміна типу, ручні селектори.
- Schema diff після калібрування (що змінилося) + кнопка «відкотити».
- Вкладка «Карантин»: записи, що не пройшли валідацію, з причиною.
- «Поясни цей рядок»: чому саме такі значення (provenance + сторінка + селектор).
- Кнопка «Затвердити та запустити» після прев'ю (двофазний flow у явному вигляді).

---

## 4. Етап 3 (P2): масштаб і джерела

| Напрям | Зміст | Залежність |
|:---|:---|:---|
| Discovery | SearXNG (є), OpenStreetMap/Overpass, sitemap, Open Data API, Brave/Bing (опційно), реєстр вартості запитів | P1 eval |
| Важкі рушії | Crawl4AI, Playwright, Camoufox, Browser-Use — окремі extras/сайдкари, capability flags, без залежностей у ядрі | P1 proxy |
| Семантика | LanceDB за інтерфейсом `SemanticIndex`; дефолт — SQLite FTS5 / DuckDB FTS | P1 |
| Дедуплікація на масштабі | Splink batch поверх DuckDB + матеріалізація рішень | P1 етап 2 |
| Дані | Версіювання схеми датасету, підпис маніфесту (sha256), інкрементальні Parquet-партиції | P1 |
| Продуктивність | Паралельні воркери-процеси, read-only DuckDB для API, кеш гарячих запитів | P1 |

---

## 5. Етап 4 (P3): монетизація

1. **Окремий Data API сервіс** (не локальний інстанс): FastAPI + Postgres/S3, API-ключі, метеринг,
   rate limit, OpenAPI, SLA, версії датасету.
2. **Клієнтський портал:** каталог продуктів, прев'ю 20 рядків (PII замасковано), підписка, usage.
3. **Білінг:** Stripe (підписка + кредити на оновлення), ліцензійні ключі, податкові вимоги.
4. **Data contract:** схемна версія, changelog, гарантія свіжості, процедура deprecation.
5. **Маркетплейс** готових дата-продуктів (каталог + семпл + ціна за оновлення).

---

## 6. Беклог із пріоритетами

| ID | Задача | Розмір | Пріоритет | Залежності |
|:---|:---|:---:|:---:|:---|
| P1-01 | Gold-set eval + метрики в маніфесті | M | P0 | — |
| P1-02 | Merge/unmerge дублікатів + UI-черга | L | P0 | P1-01 |
| P1-03 | Tombstones + full-crawl diff | M | P0 | P1-05 |
| P1-04 | Комплаєнс-енфорсмент (forbidden/high) | S | P0 | — |
| P1-05 | APScheduler + cron + ноутіфікації | M | P1 | P1-03 |
| P1-06 | Proxy provider + circuit breaker | M | P1 | — |
| P1-07 | JS-детекція + Playwright сайдкар | M | P1 | P1-06 |
| P1-08 | Карантин у UI + причина | S | P1 | P1-01 |
| P1-09 | Атрибуція plan-витрат + /metrics | S | P1 | — |
| P1-10 | Редактор spec + schema diff | M | P2 | — |
| P2-01 | Discovery: sitemap/OSM/Open Data | M | P2 | — |
| P2-02 | FTS5 + семантичний пошук | M | P2 | — |
| P2-03 | Splink batch ER | L | P2 | P1-02 |
| P2-04 | Багатопроцесний воркер + read-only API | L | P2 | P1-05 |
| P3-01 | Data API сервіс + ключі + метеринг | L | P3 | P2-04 |
| P3-02 | Портал + Stripe | L | P3 | P3-01 |
| P3-03 | Data contract + SLA | M | P3 | P1-01 |

---

## 7. Відомі обмеження v0.1 (чесно)

1. **Видалені записи не детектуються.** Снапшот кумулятивний; `removed_entity` з'явиться лише після
   tombstone-механізму (P1-03).
2. **Перерваний прогін** може залишити record versions без рядка `dataset_versions` (часткові дані).
   Записи не втрачаються, але версія не фіксується; наступний прогін використає той самий номер.
3. **Витрати планування** не атрибутуються задачі (задача створюється після виклику LLM).
4. **Якість = заповненість**, а не точність. Precision/recall немає (P1-01).
5. **Калібрування** зберігає одну схему на `domain + entity`; сайти з кількома шаблонами потребують
   signature на шаблон (P1-10).
6. **Проксі/ротації немає** — один IP, per-domain rate limit.
7. **Планувальника немає** — запуск вручну/через API.
8. **Дедуплікація** — лише кандидати, без злиття.
9. **LLM локально** не завантажується автоматично (немає pull для Ollama); хмарний — за env.
   Планування/збір через CLI не можна запускати паралельно з `daas serve`: DuckDB має одного
   writer'а. Бекап під час роботи — `POST /api/maintenance/backup`.
10. **Тяжкі рушії** (Playwright/Camoufox/Crawl4AI) не входять у базові залежності.
11. **Один користувач**, без ролей; API — локальний, з єдиним токеном.
12. **Excel** — базове форматування; складні звіти не генеруються.
13. **Локалізація** — UI українською, без перемикача мов.
14. **Немає візуальних регресійних тестів** (перевірено headless Chrome вручну).
15. **DuckDB-схема** ініціалізується `CREATE IF NOT EXISTS`; для зміни колонок потрібен migration-скрипт.

---

## 8. Тестова стратегія

- **Жодних мережевих тестів.** `FixtureFetcher` віддає локальні HTML-файли з `backend/tests/fixtures/`.
- **Одиничні:** парсер, learner (каталог + деталь), JSON-LD, coerce/PII/SSRF, dedupe, LLM-парсинг.
- **Інтеграційні:** SQLite-черга (lease/retry/cancel), DuckDB (версії/diff/snapshot).
- **E2E:** API-флоу від створення задачі до CSV/XLSX з маскуванням, включно з повторним збором.
- **Регресійні:** збереження збагачених полів, стабільність record key, порядок запусків.
- **Майбутнє:** gold-set eval як окремий набір тестів; контрактні тести провайдерів (mock-сервер).

Команди:

```bash
cd backend
uv sync --extra dev
uv run pytest -q          # 55 tests, offline
uv run ruff check app tests
```

---

## 9. Definition of Done для кожної фічі

1. Типізовані межі (Pydantic) і обробка помилок із причиною.
2. Подія в журналі для всього, що змінює дані.
3. Міграція схеми (SQLite/DuckDB), якщо додається стан.
4. Мінімум один тест (офлайн) на щасливий шлях і один — на відмову.
5. Оновлений UI-стан (порожній/помилка/завантаження).
6. Запис у цьому документі або в `02` (ADR), якщо рішення архітектурне.
7. `ruff`, `pytest`, `npm run build` зелені.

---

## 10. Структура коду (v0.1)

```text
backend/
├── app/
│   ├── api/            # tasks.py, data.py, schemas.py, deps.py
│   ├── db/             # sqlite_store.py (черга/стан), duckdb_store.py (історія/експорт)
│   ├── enrichment/     # site_audit, domain_resolver, ai_column, tech_detect, dedupe
│   ├── extraction/     # jsonld, css, learner, engine, llm_extractor, synonyms
│   ├── llm/            # providers, router, prompts, guard, parsing
│   ├── pipeline/       # planner, calibration, runner
│   ├── scraping/       # fetcher, politeness (robots/rate), page (markdown/links)
│   ├── storage/        # raw_cache (zstd), exports (csv/xlsx/parquet/json)
│   ├── cli.py          # serve/init/doctor/demo/task/calibrate/collect/enrich/export/backup
│   ├── config.py       # Settings (env DAAS_*), Paths, runtime token
│   ├── demo.py         # офлайн-сайт + живий демо-сервер
│   ├── main.py         # FastAPI factory, auth, CSP, SPA
│   ├── models.py       # TaskSpec, CssSchema, ExtractedRecord, …
│   ├── presets.py      # офлайн-шаблони колонок
│   ├── runtime.py      # Studio: композиція + життєвий цикл запусків
│   └── utils.py        # coerce, PII, SSRF, record_key, телефон/ціна
└── tests/              # 7 модулів, fixtures/*.html
frontend/
└── src/                # App, Sidebar, TaskWorkspace, DataGrid, Calibration, Export, …
```

---

## 11. Як перевірити зараз

```bash
cd backend
uv sync --extra dev
uv run pytest -q
uv run ruff check app tests

cd ../frontend
npm install && npm run build     # UI віддається з FastAPI

cd ../backend
uv run daas demo --serve         # офлайн-демо + студія на http://127.0.0.1:8000
```

Очікувано: 4 записи, версія 2, XLSX у `storage/exports/`, UI з даними, калібруванням і змінами.

---

## 12. Ризики

| Ризик | Ймовірність | Вплив | Мітигація |
|:---|:---:|:---:|:---|
| Антибот блокує джерела | висока | високий | провайдери API/Open Data, проксі, circuit breaker, health-score |
| Юридичні претензії через дані | середня | критичний | реєстр ToS, robots, PII-мінімізація, ліцензія в експорті, DSAR |
| LLM-витрати з'їдають маржу | середня | високий | детермінований шлях, бюджет, кеш, ліміт, метрика $/1000 |
| Галюцинації в AI-колонках | середня | середній | confidence, provenance, cross-check, карантин |
| Верстка змінюється | висока | середній | self-healing, повторне калібрування, алерти |
| Дані втрачаються при збої | низька | високий | append-only, WAL, бекапи, тести |
| Вузьке місце — пошук джерел | висока | високий | discovery-провайдери, каталог готових дата-продуктів |
