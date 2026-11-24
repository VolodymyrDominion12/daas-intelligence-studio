# 010 — Proxy provider і domain circuit breaker

| Поле | Значення |
|:---|:---|
| Беклог | `P1-06` (`docs/03` §3.4, §6) |
| Статус | done |
| Зачіпає спеки | `capabilities/00-domain-model.md`, `capabilities/05-fetching.md`, `capabilities/04-runs-and-collection.md`, `capabilities/11-api-and-security.md`, `capabilities/12-storage-cli-ops.md`, `capabilities/13-frontend.md` |
| Автор / дата | AI agent · 2026-11-24 |

> Spec описує **ЩО** і **НАВІЩО**. Ніяких назв функцій, таблиць чи бібліотек — це для `plan.md`.

## 1. Проблема і мета
Зараз студія ходить у мережу з єдиного IP без пулу проксі, без обліку здоров'я/вартості каналу та без захисту домену від серійних блокувань (403/407/429/CAPTCHA). При масовому зборі це призводить до багаторазових марних запитів у заблоковане джерело й відсутності автоматичного перемикання на дзеркало або API-fallback.

Мета: додати абстракцію провайдера проксі (один проксі або пул, sticky session на домен, оцінка здоров'я, cooldown, облік трафіку й вартості без витоку облікових даних), детерміновану детекцію антибот-банів та domain circuit breaker (автоматична пауза домену після N блокувань із перемиканням на налаштований API/mirror fallback, спостережуваністю у `/api/metrics`, API, CLI та UI).

## 2. Користувацькі сценарії
1. **Як** оператор студії, **я хочу** підключити один або кілька проксі через змінні оточення з sticky-прив'язкою до домену й автоматичним виключенням нездорових проксі, **щоб** рівномірно та безпечно збирати відкриті сайти без розкриття паролів проксі в логах або UI.
2. **Як** власник регулярного моніторингу, **я хочу**, щоб після N поспіль блокувань (HTTP 401/403/407/451 або CAPTCHA/challenge-сторінка) circuit breaker ставив домен на паузу й перемикав запит на налаштоване дзеркало чи публічний API, **щоб** не «довбати» заблокований сервер і зберегти результативність запуску.
3. **Як** DevOps/аналітик, **я хочу** бачити здоров'я пулу проксі, витрачений трафік/вартість і відкриті circuit breakers у `/api/metrics`, CLI та UI (із кнопкою ручного скидання паузи), **щоб** контролювати інфраструктуру збору.

## 3. Вимоги (дельта до `capabilities/`)

### ADDED

#### REQ-FETCH-020 — Провайдер проксі: пул, sticky-сесії, health-score та облік витрат
Система **ПОВИННА**:
- підтримувати роботу як без проксі (прямі запити за замовчуванням), так і з одним або пулом проксі (`DAAS_PROXY_URLS`) зі стратегіями вибору (`sticky_domain` | `round_robin` | `health_first`);
- у режимі `sticky_domain` утримувати здоровий проксі за доменом протягом `DAAS_PROXY_STICKY_TTL_S`, а при отриманні блокування чи помилки проксі знижувати його `health_score`, скидати sticky-прив'язку, переводити проксі в тимчасовий cooldown (`DAAS_PROXY_COOLDOWN_S`) після серії відмов і ротувати наступний здоровий проксі;
- вести облік переданих байтів і оціненої вартості (`DAAS_PROXY_COST_PER_GB_USD`) на кожен проксі й сумарно;
- **НЕ ПОВИННА** розкривати `username`/`password` з URL проксі у відповідях API, CLI, подіях або UI.
- **Сценарій:** GIVEN пул із двох проксі у стратегії `sticky_domain` WHEN перший запит проходить успішно THEN наступний запит того ж домену використовує той самий проксі; WHEN перший проксі повертає блок/помилку THEN його `health_score` падає і запит ротується на другий проксі, а `bytes_transferred`/`cost_usd` фіксуються без витоку паролів.
- **Відмова:** GIVEN не налаштовано жодного проксі або всі проксі тимчасово в cooldown THEN система коректно працює напряму або повідомляє стан без падіння.
- **Перевірка:** `tests/test_proxy_resilience.py::test_proxy_provider_sticky_health_cost_and_redaction`

#### REQ-FETCH-021 — Детекція банів і domain circuit breaker
Система **ПОВИННА**:
- класифікувати як блокування (`blocked`) не лише HTTP-статуси `401/403/407/451`, а й детерміновані антибот/CAPTCHA-маркери у тілі відповіді (Cloudflare challenge, reCAPTCHA/hCaptcha, DataDome тощо на невеликій сторінці);
- підтримувати для кожного домену автомат станів circuit breaker (`closed` → `open` → `half_open` → `closed`): після `DAAS_CIRCUIT_BREAKER_THRESHOLD` блокувань поспіль домен переходить у `open` на `DAAS_CIRCUIT_BREAKER_COOLDOWN_S`;
- під час `open` негайно відхиляти нові мережеві запити до домену (`kind="circuit_open"`, локальний cache-hit залишається доступним) і після завершення паузи пропускати один пробний запит (`half_open`), який або закриває ланцюг при успіху, або знову відкриває при блокуванні;
- зберігати можливість ручного скидання стану circuit breaker для домену.
- **Сценарій:** GIVEN поріг `threshold=2` WHEN домен повертає 2 блокування поспіль THEN circuit переходить в `open`, наступний запит блокується локально без мережевого виклику (`kind="circuit_open"`); WHEN минає cooldown і пробний запит (`half_open`) успішний THEN circuit повертається у `closed`.
- **Відмова:** GIVEN пробний запит у стані `half_open` отримує `blocked` THEN circuit миттєво повертається в `open` на новий період cooldown.
- **Перевірка:** `tests/test_proxy_resilience.py::test_http_fetcher_ban_detection_and_circuit_breaker_lifecycle`

#### REQ-RUN-050 — Реакція пайплайна на відкритий circuit breaker та fallback на API/дзеркало
Під час збору (`PipelineRunner`) система **ПОВИННА**:
- при відкритті circuit breaker на домені записувати подію `circuit_opened` (`warning`) один раз на перехід у `open`;
- якщо у політиці джерела (`SourcePolicy`) для домену задано дзеркальні/API `fallback_urls`, перемикати заблокований запит (один раз на URL, без зациклення) на дозволений `assert_logged_out` та `PolicyGuardFetcher` fallback URL і записувати подію `circuit_fallback`;
- задачі черги, пропущені через `circuit_open`, позначати як `skipped`, зберігаючи консервативний інваріант tombstones (`full_crawl=false`, існуючі записи не видаляються).
- **Сценарій:** GIVEN домен із налаштованим дзеркалом у політиці джерела WHEN домен перевищує поріг блокувань THEN пайплайн пише `circuit_opened`, ставить запит на дзеркало (`circuit_fallback`), успішно витягує дані й не пошкоджує наявні записи.
- **Відмова:** GIVEN дзеркало заборонене в реєстрі джерел (`forbidden`) або містить токен у URL WHEN формується fallback THEN комплаєнс-захист відхиляє небезпечний fallback.
- **Перевірка:** `tests/test_proxy_resilience.py::test_pipeline_circuit_breaker_fallback_and_compliance_guard`

#### REQ-API-011 — API, метрики та CLI для статусу проксі, fallback-дзеркал і circuit breakers
Система **ПОВИННА** надавати захищені Bearer-токеном інтерфейси та CLI:
- `GET /api/resilience` — поточний статус пулу проксі (здоров'я, cooldown, байти, вартість, без секретів) та список станів circuit breaker по доменах;
- `POST /api/resilience/circuits/{domain}/reset` — ручне скидання circuit breaker домену з подією `circuit_reset`;
- `PUT /api/sources/{domain}` — підтримка поля `fallback_urls` (з валідацією `http/https` і `logged-out`);
- `GET /api/metrics` — експорт Prometheus-метрик (`daas_proxy_pool_size`, `daas_proxy_healthy`, `daas_proxy_bytes_total`, `daas_proxy_cost_usd_total`, `daas_circuit_open_domains`, `daas_blocked_total{reason="circuit_open"}`);
- CLI команди `daas proxy-status` та `daas circuit-reset <domain>`.
- **Сценарій:** GIVEN активні проксі та відкритий circuit на домені WHEN клієнт викликає `/api/resilience` і `/api/metrics`, а потім `/api/resilience/circuits/{domain}/reset` THEN статус, метрики та скидання працюють синхронно й без витоку паролів проксі.
- **Відмова:** GIVEN fallback URL із обліковими даними (`?token=...`) у `PUT /api/sources/{domain}` THEN API повертає 400/422.
- **Перевірка:** `tests/test_proxy_resilience.py::test_resilience_api_metrics_validation_and_cli`

#### REQ-UI-015 — Відображення пулу проксі, circuit breakers і fallback-дзеркал у вкладці «Джерела»
`SourcesPanel` у веб-інтерфейсі **ПОВИНЕН** бути синхронним із API-контрактом (`types.ts`, `api.ts`) і показувати:
- блок «Проксі та стійкість краулера» зі станом проксі (redacted host/port, health %, запити, байти, вартість USD) та списком доменних circuit breakers із кнопкою «Скинути паузу»;
- поле редагування `fallback_urls` (API / дзеркала на випадок блокування) у формі політики домену;
- стани завантаження, порожніх налаштувань та показ помилок (`toast`).
- **Сценарій:** GIVEN вкладка «Джерела» WHEN користувач переглядає відкритий circuit breaker і натискає «Скинути паузу» або зберігає `fallback_urls` THEN стан домену оновлюється без перезавантаження сторінки.
- **Відмова:** GIVEN помилка валідації дзеркала або мережева помилка API THEN UI показує toast з причиною помилки.
- **Перевірка:** `npm run typecheck`, `npm run build` (прогалина JS-рендер тестів за `13-frontend.md` §4)

## 4. Нефункціональні вимоги
- Усі тести виконуються повністю офлайн (`httpx.MockTransport`, `FixtureFetcher`), без реальних мережевих чи проксі-з'єднань.
- Жодна нова runtime-залежність не додається (`httpx` підтримує proxy-параметри з коробки).
- Облікові дані у proxy URL ніколи не потрапляють у логи, події, API або Prometheus-мітки.
- Комплаєнс-захист (`PolicyGuardFetcher`, SSRF, `logged-out`, `robots.txt`) зберігається без жодних винятків для проксі-запитів та fallback-дзеркал.

## 5. Поза скоупом
- Автоматична купівля/ротація платних API-провайдерів проксі (BrightData/Oxylabs API) — керується статичним/env пулом.
- JS-детекція порожнього DOM і Playwright-сайдкар (`P1-07`).

## 6. Відкриті питання
- Немає: архітектура `ProxyProvider` і domain circuit breaker визначена в `docs/03` §3.4 та конституції.

## 7. Критерії приймання
- [x] Усі REQ (`REQ-FETCH-020`, `REQ-FETCH-021`, `REQ-RUN-050`, `REQ-API-011`, `REQ-UI-015`) мають тести і вони зелені
- [x] Нові змінні `DAAS_*` задокументовані в `.env.example` і `specs/capabilities/12-storage-cli-ops.md`
- [x] `make specs-check test lint build` зелені
- [x] `capabilities/*.md` та `docs/03_IMPLEMENTATION_PLAN.md` оновлені після злиття
