# 008 — План реалізації: Атрибуція plan-витрат LLM та Prometheus `/api/metrics`

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
1. Розширюємо `LLMRequest` (додаємо опційні `task_id`, `run_id`) та `LLMRouter.complete_json(...)`, щоб атрибуція витрат у `llm_calls` брала контекст із `(explicit arg / request) → current_scope() → self.task_id/self.run_id`, без гонок між паралельними задачами.
2. У `Planner.plan(...)` передаємо ізольований локальний `LLMBudget` і `run_id=plan_ref` без мутації спільного `router.bind(...)`; у `Studio.create_task` після `store.create_task(spec)` викликаємо `OperationalStore.bind_llm_calls(plan_ref, task.id)` і включаємо `spend_usd` у `GET /api/tasks/{id}`.
3. Додаємо легковагові методи `metrics_snapshot()` у `OperationalStore` і `AnalyticsStore`, метод `Studio.prometheus_metrics()` та ендпоінт `GET /api/metrics` (під Bearer auth) з формуванням Prometheus exposition text (`0.0.4`).

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `008-p1-09-metrics-and-spend/{spec,plan,tasks}.md` створені до коду |
| II. Детермінованість, LLM лише fallback | ✅ | Метрики агрегуються детерміновано; plan fallback зберігається |
| III. Тести офлайн | ✅ | Використовується фейковий провайдер у `test_metrics_and_spend.py` і локальний HTTP `TestClient` |
| IV. Походження, append-only | ✅ | `record_versions` не зачіпається |
| V. Комплаєнс і PII | ✅ | `/api/metrics` не виводить жодних PII, секретів чи URL |
| VI. Local-first, захищений API | ✅ | `/api/metrics` вимагає Bearer-токен (401 без токена) |
| VII. Події для змін даних | ✅ | `task_created` отримує `plan_llm_usd`; нові типи подій не потрібні |
| VIII. Типізовані межі, міграції | ✅ | Нових таблиць/колонок не потрібно (`llm_calls.task_id` і `run_id` уже існують) |
| IX. Простота, без зайвих залежностей | ✅ | Форматер Prometheus у ~45 рядках чистого Python без `prometheus-client` |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| LLM-шар | `app/llm/base.py`, `app/llm/router.py` | Опційні `task_id`/`run_id` у `LLMRequest` та `complete_json`; пріоритет атрибуції `request → run_scope → bind` | `REQ-LLM-007` |
| Планування / Сервіс | `app/pipeline/planner.py`, `app/db/sqlite_store.py`, `app/runtime.py` | Ізольований бюджет і `run_id` у `Planner.plan`; `bind_llm_calls(plan_ref, task_id)`; `plan_llm_usd` у `task_created` | `REQ-PLAN-013` |
| Метрики / Сховище | `app/db/sqlite_store.py`, `app/db/duckdb_store.py`, `app/runtime.py` | `metrics_snapshot()` у обох сховищах; `Studio.prometheus_metrics()` | `REQ-API-009` |
| API / Типи UI | `app/api/data.py`, `app/api/tasks.py`, `frontend/src/types.ts` | `GET /api/metrics` (`PlainTextResponse`), `spend_usd` у `GET /api/tasks/{id}` та `TaskDetail.spend_usd` | `REQ-API-009`, `REQ-PLAN-013` |

## 4. Дані та міграції
- Схема SQLite `llm_calls` уже має `task_id TEXT, run_id TEXT`. Міграції не потрібні.
- Запит `bind_llm_calls`: параметризований `UPDATE llm_calls SET task_id = ? WHERE run_id = ? AND task_id IS NULL`.

## 5. Контракти
- Новий API ендпоінт:
  - `GET /api/metrics` → `200 text/plain; version=0.0.4; charset=utf-8` (401 без валідного Bearer-токена)
- Розширення `GET /api/tasks/{task_id}`:
  - Додається поле `"spend_usd": float` (`round(studio.store.task_spend(task_id), 6)`), сумісно з `frontend/src/types.ts::TaskDetail.spend_usd?: number`.

## 6. Ризики і відкат
- **Ризик:** Існуючі тести могли очікувати, що `router.bind(...)` керує бюджетом або `cache_key()` залежить від `LLMRequest`.
  - *Мітигація:* `LLMRequest.cache_key()` не включає `task_id`/`run_id`, тому кешування ідентичне. Legacy-виклик `router.bind(...)` залишається fallback-ом, якщо `request`/`run_scope` не задані.
- **Ризик:** Некоректне екранування Prometheus label-значень.
  - *Мітигація:* Усі значення label проходять просте екранування `\` / `"` / `\n`; назви статусів і цілей фіксовані/короткі ASCII-рядки.

## 7. Тестова стратегія
- `backend/tests/test_metrics_and_spend.py` (повністю офлайн):
  1. `test_plan_spend_attributed_to_created_task`: створення задачі з фейковим платним LLM-провайдером → `llm_calls` містить запис `purpose="plan"` з `task_id == task.id`, `store.task_spend(task.id)` та `GET /api/tasks/{id}.spend_usd` відображають plan-витрати; провальне створення (403/disabled) не забруднює існуючі задачі.
  2. `test_router_attribution_isolated_across_concurrent_scopes`: два паралельні корутини в різних `run_scope(task_id, run_id)` одночасно викликають спільний `LLMRouter` (і між ними викликається `Planner`) — кожен запис у `llm_calls` отримує свій точний `task_id` і `run_id` без гонок.
  3. `test_prometheus_metrics_endpoint_requires_auth_and_reports_state`: перевіряє `401` без токена та `200 text/plain` із токеном на `GET /api/metrics`, наявність `daas_up`, `daas_tasks_total`, `daas_queue_jobs`, `daas_pages_fetched_total`, `daas_blocked_total`, `daas_records_current`, `daas_records_quarantined`, `daas_llm_spend_usd_total`, `daas_raw_cache_pages` із коректними числовими значеннями після збору й карантину.
