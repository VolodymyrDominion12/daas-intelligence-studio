# 011 — План реалізації: JS-детекція порожнього DOM і Playwright-сайдкар

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Виносимо детерміновану детекцію порожнього JS-DOM (`detect_js_shell`), лічильники телеметрії (`JsRenderStats`) та легкий HTTP-клієнт сайдкара (`PlaywrightSidecarClient`) у новий модуль `backend/app/scraping/js_render.py` поверх уже наявного `httpx` без додавання важких runtime-залежностей у ядро. У `HttpFetcher` додаємо підтримку `DAAS_FETCH_ENGINE=auto` (HTTP → `detect_js_shell` → ескалація на сайдкар або optional локальний Playwright із безпечним fallback на HTTP) та `SidecarPlaywrightFetcher` для `DAAS_FETCH_ENGINE=playwright`. Увесь JS-трафік проходить `assert_public_url`, `PolicyGuardFetcher` (pre-fetch і `final_url`), `DomainCircuitBreaker`, `detect_ban_marker` і `ProxyProvider`. Для production VPS додаємо `deploy/playwright-sidecar/` (ізольований FastAPI + Playwright Chromium сервіс) із профілем `profiles: ["playwright"]` у `docker-compose.prod.yml`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | Спершу `spec.md`, `plan.md`, `tasks.md` і офлайн-тести в `test_js_sidecar.py` |
| II. Детермінованість, LLM лише fallback | ✅ | `detect_js_shell` повністю детермінований через `selectolax`, без LLM і `eval` |
| III. Тести офлайн | ✅ | `httpx.MockTransport` для обох рівнів: цільового сайту і HTTP API сайдкара |
| IV. Походження, append-only | ✅ | Відрендерений HTML потрапляє в `PageContent(engine=...)`, `RawCache` і стандартну екстракцію |
| V. Комплаєнс і PII | ✅ | Перевірки SSRF, `PolicyGuardFetcher` (`final_url` редиректів), `logged-out` і `robots.txt` зберігаються; паролі в proxy/sidecar URL редагуються |
| VI. Local-first, захищений API | ✅ | Внутрішній виклик сайдкара тільки через конфіг `DAAS_PLAYWRIGHT_SIDECAR_URL`; `/api/config`, `/api/resilience`, `/api/metrics` за Bearer-токеном |
| VII. Події для змін даних | ✅ | Додаються події `js_rendered` та `js_render_fallback` до каталогу `00-domain-model.md` §4 |
| VIII. Типізовані межі, міграції | ✅ | Нові `DAAS_*` у `Settings`, типізований `JsRendererStatus`, міграція БД не потрібна |
| IX. Простота, без зайвих залежностей | ✅ | Базові залежності у `pyproject.toml` не змінюються; `playwright` лише в optional extra `[browser]` та окремому сайдкар-контейнері |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Конфіг | `backend/app/config.py`, `.env.example`, `backend/pyproject.toml` | `DAAS_PLAYWRIGHT_SIDECAR_URL`, `DAAS_PLAYWRIGHT_WAIT_UNTIL`, `DAAS_PLAYWRIGHT_WAIT_MS`, `DAAS_JS_MIN_TEXT_CHARS`; optional extra `browser` у `pyproject.toml` | `REQ-FETCH-030`, `REQ-FETCH-031`, `REQ-FETCH-009` |
| JS-детекція / сайдкар-клієнт | `backend/app/scraping/js_render.py` | `detect_js_shell`, `JsRenderStats`, `PlaywrightSidecarClient` (з `transport`/`client_factory` для офлайн-тестів) | `REQ-FETCH-030`, `REQ-FETCH-031` |
| Завантажувач | `backend/app/scraping/fetcher.py` | Ескалація `auto` у `HttpFetcher._attempt`, `SidecarPlaywrightFetcher`, безпека `final_url` у `PlaywrightFetcher` та оновлений `build_fetcher` | `REQ-FETCH-030`, `REQ-FETCH-031`, `REQ-FETCH-009` |
| Пайплайн / Студія | `backend/app/pipeline/calibration.py`, `backend/app/pipeline/runner.py`, `backend/app/runtime.py` | Події `js_rendered` / `js_render_fallback`, попередження при `http`+JS-оболонці, `capabilities().js_renderer`, `resilience_status().js_renderer`, метрики `daas_js_renders_total` | `REQ-RUN-060`, `REQ-API-012` |
| CLI | `backend/app/cli.py` | Перевірка `js_renderer` (включно з `sidecar_healthy` при конфігурації й `!offline`) у `daas doctor` | `REQ-API-012` |
| UI | `frontend/src/types.ts`, `frontend/src/components/MiscPanels.tsx` | Типи `JsRendererStatus`, індикатор режиму та лічильників JS-рендерингу в `SourcesPanel` | `REQ-API-012` |
| Деплой / Сайдкар | `deploy/playwright-sidecar/Dockerfile`, `deploy/playwright-sidecar/server.py`, `docker-compose.prod.yml`, `deploy/README.md` | Контейнер сайдкара (`GET /health`, `POST /render`) і профіль `playwright` у `docker-compose.prod.yml` | `REQ-FETCH-031` |

## 4. Дані та міграції
- Нових таблиць або колонок у SQLite/DuckDB не потрібно; стан діагностики JS-рендерингу тримається у `JsRenderStats` у пам'яті `Studio`, а кешування відрендерених сторінок використовує чинний `RawCache` з `engine="playwright-sidecar"` / `"playwright"`.

## 5. Контракти
- **Нові `DAAS_*`:**
  - `DAAS_PLAYWRIGHT_SIDECAR_URL: str | None = None` — базовий URL сайдкара (напр. `http://127.0.0.1:8765`)
  - `DAAS_PLAYWRIGHT_WAIT_UNTIL: Literal["networkidle", "domcontentloaded", "load"] = "networkidle"`
  - `DAAS_PLAYWRIGHT_WAIT_MS: int = Field(default=700, ge=0, le=15_000)`
  - `DAAS_JS_MIN_TEXT_CHARS: int = Field(default=120, ge=20, le=2000)`
- **HTTP API сайдкара:**
  - `GET /health` → `200 {"status": "ok", "engine": "playwright", "browser": "chromium"}`
  - `POST /render` ← `{"url", "user_agent", "timeout_ms", "wait_until", "wait_ms", "proxy_url"}` → `200 {"url", "final_url", "status_code", "html", "headers", "elapsed_ms"}`
- **API студії & Metrics:**
  - `GET /api/config` → `capabilities.js_renderer: {mode, active_engine, sidecar_configured, sidecar_url, escalations, rendered_ok, fallbacks, errors}` (`sidecar_url` проходить `redact_proxy_url`)
  - `GET /api/resilience` → додає `js_renderer` із тими ж полями
  - `GET /api/metrics` → додає `daas_js_renders_total{result="ok|fallback|error"}`
- **Події (`00-domain-model.md` §4):**
  - `js_rendered` (`info`, `pipeline/calibration.py`, `pipeline/runner.py`)
  - `js_render_fallback` (`warning`, `pipeline/calibration.py`, `pipeline/runner.py`)

## 6. Ризики і відкат
- **Хибна ескалація коротких сторінок:** `detect_js_shell` не спрацьовує без явних маркерів SPA-кореня, `<noscript>`-вимоги JS або модульних бандлів; сторінки з JSON-LD або антибот-баном не ескалюються.
- **SSRF через внутрішній редирект у браузері:** `HttpFetcher` / `SidecarPlaywrightFetcher` примусово перевіряють `final_url` відповіді сайдкара через `assert_public_url(..., allow_private=settings.allow_private_network)` та `redirect_guard` до кешування й екстракції.
- **Затримка чи падіння сайдкара:** при збої в режимі `auto` рендерер робить чистий fallback на HTTP і записує `js_render_fallback`; у разі потреби достатньо виставити `DAAS_FETCH_ENGINE=http`.

## 7. Тестова стратегія
- `backend/tests/test_js_sidecar.py` (повністю офлайн):
  1. Юніт-тести `detect_js_shell`: порожні `#root`/`#app`/`#__next`, `<noscript>Enable JavaScript</noscript>`, JS-бандли з коротким текстом → причина; повноцінний HTML, JSON-LD, Cloudflare CAPTCHA, порожній рядок → `None`.
  2. Тести `HttpFetcher` з `fetch_engine="auto"` + `MockTransport` для цільового сайту і для сайдкара: SSR-сторінка не чіпає сайдкар, SPA-сторінка ескалується на сайдкар, кешується вже відрендереною в `RawCache`, проксі-конфіг передається з коректним обліком, а збій сайдкара (503) виконує безпечний fallback на HTTP.
  3. Тести `build_fetcher` і `SidecarPlaywrightFetcher` (`fetch_engine="playwright"`): робота через сайдкар, безпечний fallback на HTTP без сайдкара/локального Playwright, SSRF-блокування (`unsafe`) при редиректі на `127.0.0.1`, `policy` при редиректі на заборонений домен та інтеграція з `detect_ban_marker` / `DomainCircuitBreaker`.
  4. Інтеграційний тест пайплайна (`Calibrator` + `PipelineRunner`), API (`/api/config`, `/api/resilience`, `/api/metrics`), `daas doctor` (маскування паролів у `DAAS_PLAYWRIGHT_SIDECAR_URL`), а також перевірка валідності файлів сайдкара (`deploy/playwright-sidecar/*`, `docker-compose.prod.yml` профіль `playwright`).
