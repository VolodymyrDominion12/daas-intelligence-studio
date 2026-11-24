# 011 — Задачі: JS-детекція порожнього DOM і Playwright-сайдкар

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, зачеплені `capabilities/*.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `detect_js_shell` (SPA roots, `<noscript>`, бандли vs SSR/JSON-LD/CAPTCHA/empty) → `backend/tests/test_js_sidecar.py::test_detect_js_shell_heuristics`
- [x] T011 [P] Тест `HttpFetcher` у режимі `auto` + `MockTransport` сайдкара (ескалація SPA, кешування відрендереного DOM, ігнор для SSR/JSON, fallback при 503) → `backend/tests/test_js_sidecar.py::test_auto_fetch_escalates_js_shell_caches_and_falls_back`
- [x] T012 [P] Тест `fetch_engine="playwright"` із сайдкаром і захистами (SSRF на `final_url`, `PolicyGuardFetcher`, `detect_ban_marker` + `DomainCircuitBreaker`, HTTP fallback за відсутності рушія) → `backend/tests/test_js_sidecar.py::test_playwright_sidecar_enforces_ssrf_policy_and_circuit_breaker`
- [x] T013 [P] Тест пайплайна, `/api/config`, `/api/resilience`, `/api/metrics`, `daas doctor` (з маскуванням паролів у URL сайдкара) і валідності `deploy/playwright-sidecar` + `docker-compose.prod.yml` → `backend/tests/test_js_sidecar.py::test_pipeline_events_metrics_api_doctor_and_sidecar_deploy`, `backend/tests/test_js_sidecar.py::test_js_api_metrics_doctor_and_sidecar_deploy`

## Фаза 2 — Реалізація
- [x] T020 Конфігурація (`app/config.py`, `.env.example`, `pyproject.toml` extra `browser`)
- [x] T021 Модуль `app/scraping/js_render.py` (`detect_js_shell`, `JsRenderStats`, `PlaywrightSidecarClient`)
- [x] T022 Інтеграція у `app/scraping/fetcher.py` (`HttpFetcher` режим `auto`, `SidecarPlaywrightFetcher`, безпека `final_url` у `PlaywrightFetcher`, `build_fetcher`)
- [x] T023 Інтеграція у `app/pipeline/calibration.py`, `app/pipeline/runner.py` (події `js_rendered` / `js_render_fallback`), `app/runtime.py` (`capabilities`, `resilience_status`, `prometheus_metrics`) і `app/cli.py` (`doctor`)
- [x] T024 UI (`frontend/src/types.ts`, `frontend/src/components/MiscPanels.tsx`) для відображення стану JS-рендера й сайдкара
- [x] T025 Шаблон сайдкара (`deploy/playwright-sidecar/{Dockerfile,server.py}`, `docker-compose.prod.yml`, `deploy/README.md`)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check test lint build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{00-domain-model,02-calibration,04-runs-and-collection,05-fetching,11-api-and-security,12-storage-cli-ops,13-frontend}.md` та `specs/README.md`
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (`P1-07 ✅`, §3.4, §6, §7 п.10, §10) і `specs/changes/README.md`
- [x] T042 Перенести `specs/changes/011-p1-07-js-detection-playwright-sidecar/` → `specs/changes/archive/`
