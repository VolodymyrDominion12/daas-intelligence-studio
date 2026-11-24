# 010 — Задачі: Proxy provider і domain circuit breaker

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (ріні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `PoolProxyProvider` (sticky session, ротація по блоку/cooldown, health-score, байти/вартість, редакція паролів) → `backend/tests/test_proxy_resilience.py::test_proxy_provider_sticky_health_cost_and_redaction`
- [x] T011 [P] Тест `DomainCircuitBreaker` + `HttpFetcher` з `MockTransport` (ban-маркери, `closed → open → half_open → closed`, повторне відкриття) → `backend/tests/test_proxy_resilience.py::test_http_fetcher_ban_detection_and_circuit_breaker_lifecycle`
- [x] T012 [P] Тест `PipelineRunner` fallback на `SourcePolicy.fallback_urls`, збереження tombstone-інваріанту і відмова на забороненому дзеркалі → `backend/tests/test_proxy_resilience.py::test_pipeline_circuit_breaker_fallback_and_compliance_guard`
- [x] T013 [P] Тест API `/api/resilience`, `/api/metrics`, `PUT /api/sources/{domain}`, reset і CLI → `backend/tests/test_proxy_resilience.py::test_resilience_api_metrics_validation_and_cli`

## Фаза 2 — Реалізація
- [x] T020 Конфігурація (`app/config.py`, `.env.example`) і моделі (`app/models.py`: `SourcePolicy.fallback_urls`, статус-моделі)
- [x] T021 Модуль `app/scraping/resilience.py` (`ProxyProvider`, `PoolProxyProvider`, `DomainCircuitBreaker`, `detect_ban_marker`, `redact_proxy_url`)
- [x] T022 Інтеграція у `app/scraping/fetcher.py` (`HttpFetcher`, `ErrorKind="circuit_open"`, proxy-ротація і circuit tracking)
- [x] T023 Інтеграція у `app/pipeline/runner.py` (події `circuit_opened`/`circuit_fallback`, fallback-дзеркала) і `app/runtime.py` (`resilience_status`, `reset_circuit`, `prometheus_metrics`, `capabilities`)
- [x] T024 HTTP API (`app/api/data.py`, `app/api/schemas.py`) і CLI (`app/cli.py`)
- [x] T025 UI (`frontend/src/{types.ts,api.ts,styles.css}`, `frontend/src/components/MiscPanels.tsx`) зі станами завантаження / порожньо / помилка

## Фаза 3 — Перевірка
- [x] T030 `make specs-check test lint build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{00-domain-model,04-runs-and-collection,05-fetching,11-api-and-security,12-storage-cli-ops,13-frontend}.md` (з посиланнями на тести)
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (`P1-06 ✅`, §3.4, §6, §7 п.6) і `specs/changes/README.md`
- [x] T042 Перенести `specs/changes/010-p1-06-proxy-circuit-breaker/` → `specs/changes/archive/`
