# 011 — JS-детекція порожнього DOM і Playwright-сайдкар

| Поле | Значення |
|:---|:---|
| Беклог | `P1-07` (`docs/03` §3.4, §4, §6) |
| Статус | done |
| Зачіпає спеки | `capabilities/00-domain-model.md`, `capabilities/02-calibration.md`, `capabilities/04-runs-and-collection.md`, `capabilities/05-fetching.md`, `capabilities/11-api-and-security.md`, `capabilities/12-storage-cli-ops.md`, `capabilities/13-frontend.md` |
| Автор / дата | AI agent · 2026-11-24 |

> Spec описує **ЩО** і **НАВІЩО**. Ніяких назв функцій, таблиць чи бібліотек — це для `plan.md`.

## 1. Проблема і мета
Сучасні каталоги й сайти компаній дедалі частіше будуються як SPA (React, Vue, Next.js, Nuxt, Angular) і при звичайному HTTP-запиті повертають лише порожню HTML-оболонку (`<div id="root"></div>`, `<noscript>Enable JavaScript</noscript>`, JS-бандли без видимого тексту). Зараз режим `auto` у `DAAS_FETCH_ENGINE` не реалізований, а єдиний JS-завантажувач вимагає локального встановлення важкого браузерного рушія прямо в ядро студії та не враховує SSRF на фінальному URL, бани, проксі і circuit breaker.

Мета: реалізувати детерміновану детекцію порожнього JS-DOM, автоматичну ескалацію `HTTP → JS-рендер` у режимі `auto`, підтримку ізольованого Playwright-сайдкар контейнера через легкий HTTP-контракт без важких залежностей у ядрі, збереження всіх гарантій комплаєнсу/SSRF/проксі/circuit breaker та спостережуваність у подіях, метриках, API, CLI та UI.

## 2. Користувацькі сценарії
1. **Як** оператор студії, **я хочу** увімкнути `DAAS_FETCH_ENGINE=auto`, **щоб** прості HTML-сторінки завантажувалися швидким HTTP-клієнтом, а порожні SPA-оболонки автоматично дорендерювалися через Playwright-сайдкар без зайвого навантаження на всі URL.
2. **Як** адміністратор VPS, **я хочу** піднімати Playwright в окремому контейнері-сайдкарі через профіль Docker Compose без роздування базового образу студії й без додаткових runtime-залежностей у ядрі, **щоб** базовий деплой лишався легким, а JS-рендер вмикався за потреби.
3. **Як** інженер з комплаєнсу та надійності, **я хочу**, щоб сторінки після JS-рендера проходили перевірки SSRF, `logged-out`, реєстру джерел, детекції банів, `DomainCircuitBreaker`, `ProxyProvider` та `RawCache`, а при недоступності сайдкара система не падала й фіксувала зрозумілий fallback.

## 3. Вимоги (дельта до `capabilities/`)

### ADDED

#### REQ-FETCH-030 — Детермінована детекція порожнього JS-DOM і режим `auto`
Система **ПОВИННА**:
- надавати детермінований детектор JS-оболонки HTML, який класифікує сторінку як таку, що потребує JS-рендера, коли одночасно: видимого тексту менше за `DAAS_JS_MIN_TEXT_CHARS`, відсутні структуровані JSON-LD дані та антибот-маркери бану, і присутні характерні ознаки SPA (порожні кореневі контейнери `#root`/`#app`/`#__next`/`#__nuxt`/`app-root`, `<noscript>` із вимогою увімкнути JavaScript або JS-бандли модулів/фреймворків);
- при `DAAS_FETCH_ENGINE=auto` спочатку виконувати звичайний HTTP-запит; лише у разі виявлення JS-оболонки автоматично ескалувати запит до JS-рендера і зберігати в `raw_cache` уже відрендерений HTML;
- не застосовувати JS-детекцію та ескалацію до JSON API-запитів (`allow_json=True`);
- якщо JS-рендер недоступний або зазнав транзитної помилки в режимі `auto`, повертати початковий HTTP-результат із позначкою fallback без падіння запуску.
- **Сценарій:** GIVEN `DAAS_FETCH_ENGINE=auto` і налаштований сайдкар WHEN сайт віддає `<div id="root"></div>` по HTTP, а звичайний SSR-сайт віддає повний каталог THEN SSR-сайт не викликає сайдкар взагалі, а SPA-сторінка автоматично дорендерюється через сайдкар і дає повний контент у `RawCache`.
- **Відмова:** GIVEN `DAAS_FETCH_ENGINE=auto`, сайт віддає SPA-оболонку, а сайдкар повертає 503 або мережеву помилку THEN система фіксує fallback і повертає HTTP-сторінку без аварійного падіння.
- **Перевірка:** `tests/test_js_sidecar.py::test_detect_js_shell_heuristics`, `tests/test_js_sidecar.py::test_auto_fetch_escalates_js_shell_caches_and_falls_back`

#### REQ-FETCH-031 — Playwright-сайдкар, безпека редиректів і інтеграція з проксі/circuit breaker
Система **ПОВИННА**:
- підтримувати підключення до зовнішнього Playwright-сайдкара (`DAAS_PLAYWRIGHT_SIDECAR_URL`) з налаштуваннями `DAAS_PLAYWRIGHT_WAIT_UNTIL` і `DAAS_PLAYWRIGHT_WAIT_MS` без додавання `playwright` у базові залежності ядра;
- у режимі `DAAS_FETCH_ENGINE=playwright` використовувати сайдкар (якщо задано URL) або локальний optional `playwright`, а за відсутності обох — писати попередження і переходити на HTTP (REQ-FETCH-009);
- для кожного рендера через сайдкар зберігати pre-fetch перевірки (`assert_public_url`, `robots.txt`, `DomainCircuitBreaker`, `DomainRateLimiter`, `PolicyGuardFetcher`) та post-render перевірки отриманого `final_url` (SSRF, `redirect_guard`, `logged-out`), класифікацію статусів (`401/403/407/451` → `blocked`), детекцію антибот-банів `detect_ban_marker` і облік `ProxyProvider` без розкриття паролів проксі;
- надавати Docker-шаблон сайдкара (`deploy/playwright-sidecar/`) і опційний профіль `playwright` у `docker-compose.prod.yml`.
- **Сценарій:** GIVEN `DAAS_FETCH_ENGINE=playwright` і сайдкар WHEN запитується публічна сторінка THEN сайдкар повертає відрендерений DOM, сторінка кешується з `engine="playwright-sidecar"`, а наступний запит іде з кешу.
- **Відмова:** GIVEN сайдкар повертає `final_url`, що веде на приватну IP (`127.0.0.1` / `169.254.169.254`) або заборонений у реєстрі домен THEN завантажувач блокує результат як `unsafe` або `policy`, а на сторінці з CAPTCHA класифікує відповідь як `blocked` і рахує у `DomainCircuitBreaker`.
- **Перевірка:** `tests/test_js_sidecar.py::test_playwright_sidecar_enforces_ssrf_policy_and_circuit_breaker`, `tests/test_js_sidecar.py::test_js_api_metrics_doctor_and_sidecar_deploy`

#### REQ-RUN-060 — Події та діагностика JS-рендерингу в пайплайні і калібруванні
Під час калібрування (`Calibrator`) і збору (`PipelineRunner`) система **ПОВИННА**:
- при успішній автоматичній ескалації сторінки в JS-рендер записувати подію `js_rendered` (`info`) з `url`, `engine` та причиною детекції;
- при fallback через недоступність/помилку JS-рендера записувати подію `js_render_fallback` (`warning`) і додавати підказку до попереджень звіту калібрування, якщо виявлено JS-оболонку при `fetch_engine="http"` або при збої рендера.
- **Сценарій:** GIVEN задача з `DAAS_FETCH_ENGINE=auto` калібрує SPA-сторінку через сайдкар WHEN калібрування або збір завершуються THEN у журналі є подія `js_rendered`, а екстрактор навчається на вже відрендереному DOM.
- **Відмова:** GIVEN у задачі виявлено JS-оболонку при `DAAS_FETCH_ENGINE=http` або сайдкар недоступний при `auto` THEN користувач отримує попередження/подію `js_render_fallback` з поясненням.
- **Перевірка:** `tests/test_js_sidecar.py::test_pipeline_events_metrics_api_doctor_and_sidecar_deploy`

#### REQ-API-012 — Спостережуваність JS-рендера в API, Prometheus `/api/metrics`, `daas doctor` та UI
Система **ПОВИННА**:
- відображати конфігурацію та статистику JS-рендерингу (активний режим `fetch_engine`, наявність/редагований URL сайдкара, лічильники `escalations`, `rendered_ok`, `fallbacks`, `errors`) у `GET /api/config` (`capabilities.js_renderer`), `GET /api/resilience` (`js_renderer`) та у виводі `daas doctor`;
- експортувати у `GET /api/metrics` Prometheus-лічильники `daas_js_renders_total{result="ok"}`, `daas_js_renders_total{result="fallback"}`, `daas_js_renders_total{result="error"}`;
- оновити типізовані контракти фронтенду (`types.ts`) та панель «Проксі та стійкість краулера» у `SourcesPanel`, щоб показувати стан JS-рендера та лічильники без розкриття секретів.
- **Сценарій:** GIVEN увімкнено `auto` + сайдкар і виконано рендеринг SPA-сторінки WHEN клієнт викликає `/api/config`, `/api/resilience` і `/api/metrics` THEN усі поля та метрики відображають точні лічильники рендерів.
- **Відмова:** GIVEN у URL сайдкара передано облікові дані (`http://user:secret@sidecar:8765`) WHEN викликається `/api/config`, `/api/resilience` або `/api/metrics` THEN пароль і логін маскуються й не потрапляють у відповідь.
- **Перевірка:** `tests/test_js_sidecar.py::test_js_api_metrics_doctor_and_sidecar_deploy`

### MODIFIED

#### REQ-FETCH-009 — Опційний JS-рушій → Сайдкар і локальний Playwright без залежностей у ядрі
- **Було:** `DAAS_FETCH_ENGINE=playwright` вмикає лише локальний `PlaywrightFetcher`; якщо Playwright не встановлено — попередження і fallback на HTTP.
- **Стане:** `DAAS_FETCH_ENGINE=playwright|auto` підтримує зовнішній Playwright-сайдкар (`DAAS_PLAYWRIGHT_SIDECAR_URL`) у пріоритеті перед локальним `PlaywrightFetcher`; за відсутності обох система пише попередження і безпечно переходить на HTTP.

## 4. Нефункціональні вимоги
- Усі тести виконуються повністю офлайн (`httpx.MockTransport` для сайдкара й цільового сайту, `FixtureFetcher`), без запуску справжнього Chromium чи мережевих викликів.
- Базові залежності у `backend/pyproject.toml` `[project].dependencies` залишаються без `playwright`; додається лише optional extra `browser`.
- Усі паролі в URL (проксі чи сайдкар) редагуються перед поверненням у API/UI/події.
- SSRF, `PolicyGuardFetcher`, `robots.txt`, `logged-out` та `DomainCircuitBreaker` обов'язкові для всіх рендерів.

## 5. Поза скоупом
- Автоматичне розв'язання CAPTCHA та обхід платних WAF кліками або розширеннями браузера.
- Кліки по cookie-банерах на основі LLM-підказок (окрема задача після `P1-07`).

## 6. Відкриті питання
- Немає: архітектура сайдкара та `auto` JS-детекції узгоджена з `docs/03` §3.4, §4 та `REQ-FETCH-009`.

## 7. Критерії приймання
- [x] Усі REQ (`REQ-FETCH-030`, `REQ-FETCH-031`, `REQ-RUN-060`, `REQ-API-012`, оновлена `REQ-FETCH-009`) мають тести й вони зелені
- [x] Нові `DAAS_*` змінні задокументовані у `.env.example` і `specs/capabilities/12-storage-cli-ops.md`
- [x] `make specs-check test lint build` зелені
- [x] `capabilities/*.md` і `docs/03_IMPLEMENTATION_PLAN.md` оновлені після злиття
