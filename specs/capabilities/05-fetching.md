# 05 — Завантаження, ввічливість, SSRF, raw-кеш

| | |
|:---|:---|
| **Код** | `backend/app/scraping/fetcher.py` (`HttpFetcher`, `PlaywrightFetcher`, `FetchResult`, `build_fetcher`), `backend/app/scraping/politeness.py` (`RobotsGate`, `DomainRateLimiter`, `classify_status`), `backend/app/scraping/page.py`, `backend/app/storage/raw_cache.py`, `backend/app/utils.py` (`assert_public_url`, `canonical_url`) |
| **API** | `POST /api/maintenance/purge-cache?older_than_days=N` |
| **Тести** | `tests/test_utils.py`, `tests/test_compliance.py`, `tests/test_raw_cache_backup.py`, `tests/conftest.py` (`FixtureFetcher`) |
| **Джерела** | ADR-03, ADR-09, `docs/02` §2.1 |
| **Статус** | as-built v0.1 |

## 1. Призначення
Отримати HTML публічної сторінки ввічливо (robots, rate limit, User-Agent), безпечно (SSRF, ліміт
розміру, лише HTML/XML) і дешево (zstd-кеш). Будь-який `Fetcher` у студії обгорнутий
`PolicyGuardFetcher` (06-compliance).

## 2. Контракт `FetchResult`
`page: PageContent | None`, `kind ∈ {ok, transient, blocked, permanent, robots, unsafe, disabled,
empty, policy}`, `status`, `error`. `page is None` ⇔ відмова (крім `transient` зі статусом, де
`page` несе `retry-after`).

## 3. Вимоги

### REQ-FETCH-001 — SSRF-захист до запиту і на кожному редиректі
Перед кожним запитом і кожним кроком редиректу **ПОВИННО** перевірятися: схема `http/https`, є хост,
хост не з блок-листа (`localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, `metadata.google.internal`,
`*.localhost`), не приватна IP і **не резолвиться** в приватну IP. Порушення → `kind="unsafe"`.
`DAAS_ALLOW_PRIVATE_NETWORK=true` (демо, тести) вимикає лише перевірку приватних адрес.
- **Перевірка:** `tests/test_utils.py::test_ssrf_guard_blocks_private_targets`,
  `tests/test_utils.py::test_ssrf_guard_blocks_non_http_schemes`,
  `tests/test_utils.py::test_ssrf_guard_allows_public_urls`

### REQ-FETCH-002 — Редиректи вручну, з guard-ом політик
Редиректи **ПОВИННІ** оброблятися вручну (≤ 4 кроки); кожен крок проходить SSRF-перевірку і
`redirect_guard` (встановлюється `PolicyGuardFetcher`) **до** запиту. Заборонений крок →
`kind="policy"`, цільовий URL не запитується.
- **Перевірка:** `tests/test_compliance.py::test_http_fetcher_checks_every_redirect_hop`

### REQ-FETCH-003 — robots.txt (RFC 9309)
При `DAAS_RESPECT_ROBOTS=true` (за замовчуванням) **ПОВИННО**: кешувати robots.txt на хост (6 год);
404/410 та інші 4xx → дозволено все; 5xx або недоступність → заборонено все; заборонений URL →
`kind="robots"`. `Crawl-delay` **ПОВИНЕН** знижувати RPS домену.
- **Перевірка:** — (прогалина: `FixtureFetcher` оминає `RobotsGate`; потрібен тест із
  `httpx.MockTransport`)

### REQ-FETCH-004 — Rate limit
Ліміти **ПОВИННІ** діяти одночасно: глобальний семафор (`DAAS_MAX_CONCURRENCY`, 4), семафор на домен
(`DAAS_PER_DOMAIN_CONCURRENCY`, 1) і мінімальний інтервал між запитами до домену
(`1 / DAAS_PER_DOMAIN_RPS`, за замовчуванням 1 rps).
- **Перевірка:** — (прогалина)

### REQ-FETCH-005 — Класифікація статусів і retry
`401/403/407/451` → `blocked` (без retry); `408/425/429/5xx` → `transient`; інші `4xx` →
`permanent`. Транзитні помилки й мережеві винятки **ПОВИННІ** повторюватися до
`DAAS_MAX_RETRIES` (2) разів з експоненційною паузою (`1.5·2^n`, ≤ 8 с для винятків) або за
`Retry-After` (≤ 15 с).
- **Перевірка:** — (прогалина)

### REQ-FETCH-006 — Лише HTML, обмежений розмір
Відповідь **ПОВИННА** відхилятися як `permanent`, якщо `Content-Type` не містить `html`/`xml`;
тіло обрізається до 6 МБ; порожнє тіло → `empty`.
- **Перевірка:** — (прогалина)

### REQ-FETCH-007 — Raw-кеш (bronze layer)
Кожна успішна сторінка **ПОВИННА** зберігатися zstd-стиснутим JSON під sha256 від запитаного URL і
(якщо інший) від `final_url`, атомарним записом, з індексом у SQLite `raw_pages`. Читання з кешу
дозволене лише якщо запис молодший за `DAAS_CACHE_TTL_HOURS` (24 год) і не `force_refresh`;
повернута сторінка має `from_cache=True`. Пошкоджений запис — промах, файл видаляється.
- **Перевірка:** `tests/test_raw_cache_backup.py::test_put_get_roundtrip_and_index`,
  `tests/test_raw_cache_backup.py::test_max_age_and_purge`,
  `tests/test_raw_cache_backup.py::test_corrupt_entry_is_a_miss`

### REQ-FETCH-008 — Очищення кешу (retention)
`purge(older_than_days=N)` **ПОВИНЕН** видаляти записи, старші за N днів, і їхній індекс;
`N=None`/`0` через API — весь кеш. Подія `cache_purged` (`task_id="system"`).
- **Перевірка:** `tests/test_raw_cache_backup.py::test_max_age_and_purge`,
  `tests/test_api_flow.py::test_task_editing_and_column_management` (endpoint)

### REQ-FETCH-009 — Опційний JS-рушій
`DAAS_FETCH_ENGINE=playwright` **МОЖЕ** вмикати `PlaywrightFetcher`; якщо Playwright не
встановлено — попередження і fallback на HTTP. Playwright не входить у базові залежності.
- **Перевірка:** — (прогалина)

### REQ-FETCH-010 — Тести без мережі
Тести **ПОВИННІ** використовувати `FixtureFetcher` (локальні HTML з `tests/fixtures/`) або
`httpx.MockTransport`; жоден тест не відкриває мережеве з'єднання (Конституція, ст. III).
- **Перевірка:** `tests/test_learner.py::test_fixture_files_exist`

## 4. Спостережувана поведінка / відкриті питання
- `Crawl-delay` зберігається під `urlsplit(url).hostname`, а `wait_turn` шукає RPS під
  `normalize_domain(url)` (без `www.`). Для хостів `www.*` затримка з robots.txt, схоже, не
  застосовується. Ймовірний баг → тест + фікс.
- `canonical_url` прибирає query, але кеш ключується сирим URL — сторінки, що відрізняються лише
  query, кешуються окремо. Бажано?

## 5. Прогалини
REQ-FETCH-003…006, 009 — немає тестів реального `HttpFetcher` з `httpx.MockTransport`.
