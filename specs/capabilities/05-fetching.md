# 05 — Завантаження, ввічливість, SSRF, raw-кеш

| | |
|:---|:---|
| **Код** | `backend/app/scraping/fetcher.py` (`HttpFetcher`, `SidecarPlaywrightFetcher`, `PlaywrightFetcher`, `FetchResult`, `build_fetcher`), `backend/app/scraping/js_render.py` (`detect_js_shell`, `JsRenderStats`, `PlaywrightSidecarClient`, `build_sidecar_client`), `backend/app/scraping/resilience.py` (`ProxyProvider`, `NoopProxyProvider`, `PoolProxyProvider`, `build_proxy_provider`, `DomainCircuitBreaker`, `detect_ban_marker`, `redact_proxy_url`), `backend/app/scraping/politeness.py` (`RobotsGate`, `DomainRateLimiter`, `classify_status`), `backend/app/scraping/page.py`, `backend/app/storage/raw_cache.py`, `backend/app/utils.py` (`assert_public_url`, `canonical_url`) |
| **API** | `POST /api/maintenance/purge-cache?older_than_days=N`, `GET /api/resilience`, `POST /api/resilience/circuits/{domain}/reset` |
| **Тести** | `tests/test_utils.py`, `tests/test_compliance.py`, `tests/test_raw_cache_backup.py`, `tests/test_open_api_source.py`, `tests/test_proxy_resilience.py`, `tests/test_js_sidecar.py`, `tests/conftest.py` (`FixtureFetcher`) |
| **Джерела** | ADR-03, ADR-09, `docs/02` §2.1, `docs/03` §3.4 |
| **Статус** | as-built (P1-07) |

## 1. Призначення
Отримати HTML публічної сторінки ввічливо (robots, rate limit, User-Agent), безпечно (SSRF, ліміт
розміру, лише HTML/XML), стійко до блокувань (пул проксі з health-score і domain circuit breaker)
і дешево (zstd-кеш). Будь-який `Fetcher` у студії обгорнутий `PolicyGuardFetcher` (06-compliance).

## 2. Контракт `FetchResult`
`page: PageContent | None`, `kind ∈ {ok, transient, blocked, permanent, robots, unsafe, disabled,
empty, policy, circuit_open}`, `status`, `error`. `page is None` ⇔ відмова (крім `transient` зі
статусом, де `page` несе `retry-after`).

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

### REQ-FETCH-006 — Допустимий Content-Type та обмежений розмір
За замовчуванням відповідь **ПОВИННА** відхилятися як `permanent`, якщо `Content-Type` не містить
`html`/`xml`; при явному `allow_json=True` (режим JSON API) дозволяється також `application/json`
та `application/*+json`. Тіло обрізається до 6 МБ; порожнє тіло → `empty`.
- **Перевірка:** `tests/test_open_api_source.py::test_fetcher_json_mode_and_compliance_guard`
  (JSON дозволено при `allow_json=True` і відхилено без нього; ліміт 6 МБ — прогалина)

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

### REQ-FETCH-009 — Опційний JS-рушій і сайдкар без залежностей у ядрі
`DAAS_FETCH_ENGINE=playwright|auto` **ПОВИНЕН** підтримувати зовнішній Playwright-сайдкар
(`DAAS_PLAYWRIGHT_SIDECAR_URL`) у пріоритеті перед опційним локальним `PlaywrightFetcher` (`[browser]`
extra); якщо жоден JS-рендерер не налаштовано/не встановлено — попередження і безпечний fallback
на HTTP. Playwright не входить у базові залежності ядра.
- **Перевірка:** `tests/test_js_sidecar.py::test_playwright_sidecar_enforces_ssrf_policy_and_circuit_breaker`

### REQ-FETCH-010 — Тести без мережі
Тести **ПОВИННІ** використовувати `FixtureFetcher` (локальні HTML з `tests/fixtures/`) або
`httpx.MockTransport`; жоден тест не відкриває мережеве з'єднання (Конституція, ст. III).
- **Перевірка:** `tests/test_learner.py::test_fixture_files_exist`

### REQ-FETCH-011 — Підтримка JSON-відповідей під тим самим комплаєнс-захистом
У режимі `allow_json=True` завантажувач **ПОВИНЕН** без винятків зберігати перевірки SSRF,
редирект-guard, реєстру джерел (`PolicyGuardFetcher`), logged-out, `robots.txt`, rate-limit,
обмеження розміру (6 МБ) і raw-кешу.
- **Перевірка:** `tests/test_open_api_source.py::test_fetcher_json_mode_and_compliance_guard`

### REQ-FETCH-020 — Провайдер проксі: пул, sticky-сесії, health-score та облік витрат
Система **ПОВИННА**:
- підтримувати роботу як без проксі (прямі запити за замовчуванням), так і з одним або пулом проксі (`DAAS_PROXY_URLS`) зі стратегіями вибору (`sticky_domain` | `round_robin` | `health_first`);
- у режимі `sticky_domain` утримувати здоровий проксі за доменом протягом `DAAS_PROXY_STICKY_TTL_S`, а при отриманні блокування чи помилки проксі знижувати його `health_score`, скидати sticky-прив'язку, переводити проксі в тимчасовий cooldown (`DAAS_PROXY_COOLDOWN_S`) після серії відмов і ротувати наступний здоровий проксі;
- вести облік переданих байтів і оціненої вартості (`DAAS_PROXY_COST_PER_GB_USD`) на кожен проксі й сумарно;
- **НЕ ПОВИННА** розкривати `username`/`password` з URL проксі у відповідях API, CLI, подіях або UI.
- **Перевірка:** `tests/test_proxy_resilience.py::test_proxy_provider_sticky_health_cost_and_redaction`

### REQ-FETCH-021 — Детекція банів і domain circuit breaker
Система **ПОВИННА**:
- класифікувати як блокування (`blocked`) не лише HTTP-статуси `401/403/407/451`, а й детерміновані антибот/CAPTCHA-маркери у тілі відповіді (Cloudflare challenge, reCAPTCHA/hCaptcha, DataDome тощо на невеликій сторінці);
- підтримувати для кожного домену автомат станів circuit breaker (`closed` → `open` → `half_open` → `closed`): після `DAAS_CIRCUIT_BREAKER_THRESHOLD` блокувань поспіль домен переходить у `open` на `DAAS_CIRCUIT_BREAKER_COOLDOWN_S`;
- під час `open` негайно відхиляти нові мережеві запити до домену (`kind="circuit_open"`, локальний cache-hit залишається доступним) і після завершення паузи пропускати один пробний запит (`half_open`), який або закриває ланцюг при успіху, або знову відкриває при блокуванні;
- зберігати можливість ручного скидання стану circuit breaker для домену.
- **Перевірка:** `tests/test_proxy_resilience.py::test_http_fetcher_ban_detection_and_circuit_breaker_lifecycle`

### REQ-FETCH-030 — Детермінована детекція порожнього JS-DOM і режим `auto`
Система **ПОВИННА**:
- надавати детермінований детектор `detect_js_shell`, який класифікує HTML як порожню SPA-оболонку, коли видимого тексту менше за `DAAS_JS_MIN_TEXT_CHARS`, відсутні JSON-LD і антибот-маркери бану та наявні ознаки SPA (`#root`/`#app`/`#__next`/`#__nuxt`/`app-root`, `<noscript>` із вимогою JS або модульні JS-бандли);
- у режимі `DAAS_FETCH_ENGINE=auto` спершу виконувати HTTP-запит і ескалювати до JS-рендера лише при виявленні JS-оболонки (ігноруючи JSON API `allow_json=True`), кешуючи в `RawCache` вже відрендерений DOM;
- при недоступності або транзитній помилці JS-рендера в режимі `auto` повертати вихідний HTTP-результат із позначкою fallback (`x-daas-js-fallback`) без аварійного падіння запуску.
- **Перевірка:** `tests/test_js_sidecar.py::test_detect_js_shell_heuristics`,
  `tests/test_js_sidecar.py::test_auto_fetch_escalates_js_shell_caches_and_falls_back`

### REQ-FETCH-031 — Playwright-сайдкар, безпека редиректів і інтеграція з проксі/circuit breaker
Система **ПОВИННА**:
- забезпечувати легкий HTTP-клієнт `PlaywrightSidecarClient` (`DAAS_PLAYWRIGHT_SIDECAR_URL`, `DAAS_PLAYWRIGHT_WAIT_UNTIL`, `DAAS_PLAYWRIGHT_WAIT_MS`) та ізольований Docker-шаблон сайдкара (`deploy/playwright-sidecar/`) з профілем `playwright` у `docker-compose.prod.yml`;
- для кожного рендера зберігати pre-fetch (`assert_public_url`, `robots.txt`, `DomainCircuitBreaker`, `DomainRateLimiter`, `PolicyGuardFetcher`) та post-render перевірки `final_url` (SSRF, `redirect_guard`, `logged-out`), класифікацію статусів, детекцію банів `detect_ban_marker`, облік проксі `ProxyProvider` і `DomainCircuitBreaker`.
- **Перевірка:** `tests/test_js_sidecar.py::test_playwright_sidecar_enforces_ssrf_policy_and_circuit_breaker`,
  `tests/test_js_sidecar.py::test_js_api_metrics_doctor_and_sidecar_deploy`

## 4. Спостережувана поведінка / відкриті питання
- `Crawl-delay` зберігається під `urlsplit(url).hostname`, а `wait_turn` шукає RPS під
  `normalize_domain(url)` (без `www.`). Для хостів `www.*` затримка з robots.txt, схоже, не
  застосовується. Ймовірний баг → тест + фікс.
- `canonical_url` прибирає query, але кеш ключується сирим URL — сторінки, що відрізняються лише
  query, кешуються окремо. Бажано?

## 5. Прогалини
REQ-FETCH-003…005, 006 (ліміт 6 МБ) — немає тестів реального `HttpFetcher` з `httpx.MockTransport`.
