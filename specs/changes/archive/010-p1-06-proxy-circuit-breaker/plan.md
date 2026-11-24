# 010 — План реалізації: Proxy provider і domain circuit breaker

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Виносимо чисту логіку пулу проксі (`PoolProxyProvider`, `NoopProxyProvider`), детектора банів (`detect_ban_marker`) і доменного автомата станів (`DomainCircuitBreaker`) в окремий модуль `backend/app/scraping/resilience.py` без нових зовнішніх залежностей (`httpx` уже підтримує `proxy=` на рівні клієнта). `HttpFetcher` приймає проксі-провайдер, circuit breaker та опційний `httpx.AsyncBaseTransport` для детермінованого офлайн-тестування через `MockTransport`. `SourcePolicy` розширюється зворотньо сумісним полем `fallback_urls`, що дозволяє `PipelineRunner` перемикати заблоковані URL на дозволене дзеркало або API без міграції таблиць SQLite і зі збереженням усіх гарантій `PolicyGuardFetcher`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | Спочатку `spec.md`, `plan.md`, `tasks.md` і офлайн-тести |
| II. Детермінованість, LLM лише fallback | ✅ | Детекція банів, sticky-ротація і circuit breaker повністю детерміновані без LLM |
| III. Тести офлайн | ✅ | `httpx.MockTransport` і `FixtureFetcher`, жодної реальної мережі |
| IV. Походження, append-only | ✅ | Fallback-записи проходять стандартний шлях екстракції з `Provenance` і append-only ревізіями |
| V. Комплаєнс і PII | ✅ | Усі proxy-запити і fallback URL ідуть через `PolicyGuardFetcher`, SSRF та `assert_logged_out`; паролі проксі маскуються |
| VI. Local-first, захищений API | ✅ | `/api/resilience*` захищені Bearer-токеном і Host-guard |
| VII. Події для змін даних | ✅ | Додаються події `circuit_opened`, `circuit_fallback`, `circuit_reset` у `00-domain-model.md` §4 |
| VIII. Типізовані межі, міграції | ✅ | Pydantic-моделі, нові `DAAS_*` у `config.py`/`.env.example`, сумісний `SourcePolicy.fallback_urls` без ломки існуючих JSON |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових пакетів у `pyproject.toml`; нативні можливості `httpx` |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Конфіг | `backend/app/config.py`, `.env.example` | `DAAS_PROXY_URLS`, `DAAS_PROXY_STRATEGY`, `DAAS_PROXY_STICKY_TTL_S`, `DAAS_PROXY_COOLDOWN_S`, `DAAS_PROXY_COST_PER_GB_USD`, `DAAS_CIRCUIT_BREAKER_THRESHOLD`, `DAAS_CIRCUIT_BREAKER_COOLDOWN_S` | `REQ-FETCH-020`, `REQ-FETCH-021` |
| Моделі | `backend/app/models.py`, `backend/app/api/schemas.py` | `SourcePolicy.fallback_urls` (з валідацією `http/https` + `logged-out`), `ProxyEndpointStatus`, `DomainCircuitStatus`, `SourcePolicyRequest.fallback_urls` | `REQ-RUN-050`, `REQ-API-011` |
| Скрапінг | `backend/app/scraping/resilience.py`, `backend/app/scraping/fetcher.py` | `ProxyProvider`, `PoolProxyProvider`, `DomainCircuitBreaker`, `detect_ban_marker`, `redact_proxy_url`; інтеграція в `HttpFetcher` + `ErrorKind="circuit_open"` | `REQ-FETCH-020`, `REQ-FETCH-021` |
| Пайплайн / Студія | `backend/app/pipeline/runner.py`, `backend/app/runtime.py` | Події `circuit_opened`/`circuit_fallback`/`circuit_reset`, fallback на `SourcePolicy.fallback_urls`, Prometheus-метрики та `resilience_status()`/`reset_circuit()` | `REQ-RUN-050`, `REQ-API-011` |
| API / CLI | `backend/app/api/data.py`, `backend/app/api/tasks.py`, `backend/app/cli.py` | `GET /api/resilience`, `POST /api/resilience/circuits/{domain}/reset`, `PUT /api/sources/{domain}`, `/api/config`; CLI `proxy-status`, `circuit-reset` | `REQ-API-011` |
| UI | `frontend/src/{types.ts,api.ts,styles.css}`, `frontend/src/components/MiscPanels.tsx` | Блок проксі/circuit breaker і редагування `fallback_urls` у `SourcesPanel` | `REQ-UI-015` |

## 4. Дані та міграції
- `SourcePolicy` серіалізується в JSON (`source_registry.payload_json`). Нове поле `fallback_urls: list[str] = Field(default_factory=list)` зчитується зі значень за замовчуванням для існуючих записів, тому стара БД SQLite відкривається без додаткової табличної міграції.
- Стан пулу проксі та лічильники circuit breaker живуть у пам'яті екземпляра `Studio` (аналогічно `DomainRateLimiter` та `RobotsGate`) й скидаються при рестарті процесу або через `POST /api/resilience/circuits/{domain}/reset`.

## 5. Контракти
- **Нові `DAAS_*`:** `DAAS_PROXY_URLS`, `DAAS_PROXY_STRATEGY`, `DAAS_PROXY_STICKY_TTL_S`, `DAAS_PROXY_COOLDOWN_S`, `DAAS_PROXY_COST_PER_GB_USD`, `DAAS_CIRCUIT_BREAKER_THRESHOLD`, `DAAS_CIRCUIT_BREAKER_COOLDOWN_S` (додаються в `config.py`, `.env.example`, `12-storage-cli-ops.md`).
- **API:**
  - `GET /api/resilience` → `{strategy, pool_size, healthy_count, total_bytes, total_cost_usd, proxies: [...], circuits: [...]}`
  - `POST /api/resilience/circuits/{domain}/reset` → `{domain, circuit}` (`200`, `400` при порожньому домені)
  - `PUT /api/sources/{domain}` → приймає `fallback_urls: list[str]` (`400`/`422` при невалідному/login URL)
  - `GET /api/metrics` → `daas_proxy_pool_size`, `daas_proxy_healthy`, `daas_proxy_bytes_total`, `daas_proxy_cost_usd_total`, `daas_circuit_open_domains`, `daas_blocked_total{reason="circuit_open"}`
- **Події (`00-domain-model.md` §4):**
  - `circuit_opened` (`warning`, `pipeline/runner.py`)
  - `circuit_fallback` (`info`, `pipeline/runner.py`)
  - `circuit_reset` (`info`, `runtime.Studio`)

## 6. Ризики і відкат
- **Хибне спрацювання ban-маркерів на валідному HTML:** обмежуємо перевірку тіла лише сторінками `< 64 КБ` і точними сигнатурами challenge-екранів (`cf-browser-verification`, `cf_chl_opt`, `<title>Just a moment...</title>`, `g-recaptcha` без контенту тощо), щоб не чіпати звичайні сайти.
- **Зациклення fallback:** маркуємо fallback URL прапором/лічильником у межах `RunContext.fallback_attempted`, щоб один вихідний URL робив не більше одного перемикання на дзеркало.
- **Витік пароля проксі:** `redact_proxy_url` прибирає `username:password@` ще на етапі створення `ProxyEndpointStatus` та під час логування помилок `httpx`.

## 7. Тестова стратегія
- `backend/tests/test_proxy_resilience.py` (повністю офлайн):
  1. Юніт + `MockTransport` тести `PoolProxyProvider`: sticky session по домену, ротація при `blocked`/cooldown, відновлення `health_score`, облік `bytes_transferred` та `cost_usd`, сувора редакція паролів у `snapshot()`.
  2. Юніт + `HttpFetcher` з `MockTransport` тести `DomainCircuitBreaker` та `detect_ban_marker`: перехід `closed → open` після `threshold` блокувань (включно з CAPTCHA HTML на HTTP 200), локальна пауза `circuit_open` без виклику транспорту, перехід у `half_open` після cooldown і повернення в `closed` або повторне `open`.
  3. E2E `PipelineRunner` (`FixtureFetcher` / mock-транспорт): серія блоків на основному домені → події `circuit_opened` та `circuit_fallback` → перемикання на дозволене дзеркало з `SourcePolicy.fallback_urls` → збереження записів; також перевірка відмови для забороненого дзеркала (`forbidden`).
  4. API & CLI тести: `GET /api/resilience`, `GET /api/metrics` (проксі та circuit метрики), `PUT /api/sources/{domain}` із валідним і невалідним (`?token=`) `fallback_urls`, `POST /api/resilience/circuits/{domain}/reset` і CLI `daas proxy-status` / `daas circuit-reset`.
