# 008 — Задачі: Атрибуція plan-витрат LLM та Prometheus `/api/metrics`

> Порядок важливий. Тест кожної вимоги пишеться **перед** реалізацією і спершу падає.
> `[P]` — можна робити паралельно з сусідніми `[P]` (різні файли, немає залежностей).
> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, `capabilities/{01-task-planning,10-llm,11-api-and-security}.md`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Тест `REQ-PLAN-013` (атрибуція plan-витрат до `task_id` і `GET /api/tasks/{id}.spend_usd`) → `backend/tests/test_metrics_and_spend.py::test_plan_spend_attributed_to_created_task`
- [x] T011 [P] Тест `REQ-LLM-007` (відсутність гонки атрибуції на спільному `LLMRouter` у паралельних `run_scope`) → `backend/tests/test_metrics_and_spend.py::test_router_attribution_isolated_across_concurrent_scopes`
- [x] T012 [P] Тест `REQ-API-009` (`/api/metrics`: auth 401/200, формат `text/plain; version=0.0.4`, усі метрики) → `backend/tests/test_metrics_and_spend.py::test_prometheus_metrics_endpoint_requires_auth_and_reports_state`

## Фаза 2 — Реалізація
- [x] T020 LLM-шар: `llm/base.py` (`task_id`/`run_id` в `LLMRequest`) і `llm/router.py` (ізольована атрибуція `request → run_scope → bind`)
- [x] T021 Планування і сховище: `pipeline/planner.py` (локальний бюджет і `run_id`, без мутації `router.bind`), `db/sqlite_store.py` (`bind_llm_calls`, `metrics_snapshot`), `db/duckdb_store.py` (`metrics_snapshot`)
- [x] T022 Сервіс, API, UI-типи: `runtime.py` (`create_task` атрибуція + `prometheus_metrics()`), `api/data.py` (`GET /api/metrics`), `api/tasks.py` (`spend_usd`), `frontend/src/types.ts` (`TaskDetail.spend_usd`)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check` · `make test` · `make lint` · `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Перенести дельту в `specs/capabilities/{01-task-planning,10-llm,11-api-and-security}.md` і `specs/README.md`
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md` (`P1-09 ✅` і зняти обмеження §7 п.3)
- [x] T042 Перенести `specs/changes/008-p1-09-metrics-and-spend/` → `specs/changes/archive/` та оновити `specs/changes/README.md`
