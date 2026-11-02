# 11 — Локальний HTTP API і його безпека

| | |
|:---|:---|
| **Код** | `backend/app/main.py` (`create_app`, guard-middleware, обробники помилок, SPA), `backend/app/api/tasks.py`, `backend/app/api/data.py`, `backend/app/api/schemas.py`, `backend/app/api/deps.py`, `backend/app/config.py` (`load_runtime_token`) |
| **Тести** | `tests/test_api_flow.py`, `tests/test_compliance.py` |
| **Джерела** | ADR-08, ADR-10 |
| **Статус** | as-built v0.1 |

## 1. Призначення
Єдиний процес: FastAPI віддає API під `/api/*` і зібраний SPA. API локальний, але захищений від
CSRF/DNS-rebinding, бо будь-яка сторінка в браузері може стукати на `127.0.0.1`.

## 2. Каталог ендпоінтів (контракт)

| Метод | Шлях | Призначення | Спека |
|:---|:---|:---|:---|
| GET | `/api/health` | стан, версія, `llm_ready` (публічний) | 11 |
| GET | `/api/config` | capabilities, overview, enrichers | 11 |
| GET/POST | `/api/tasks` | список / створити (201) | 01 |
| GET/PATCH/DELETE | `/api/tasks/{id}` | деталі (калібрування, датасет, запуски, активний запуск) / оновити / видалити | 01 |
| PATCH | `/api/tasks/{id}/limits` | ліміти | 01 |
| POST/DELETE | `/api/tasks/{id}/columns[/{key}]` | колонки | 01 |
| POST | `/api/tasks/{id}/calibrate` | калібрування (query: `force`, `acknowledge_high_risk`, `reason`) | 02, 06 |
| GET | `/api/tasks/{id}/calibration` | звіт калібрування | 02 |
| POST | `/api/tasks/{id}/run` | збір (`RunRequest`) | 04, 06 |
| POST | `/api/tasks/{id}/enrich` | збагачення | 07 |
| POST | `/api/tasks/{id}/snapshot` | снапшот + diff | 08 |
| GET | `/api/tasks/{id}/runs`, `/api/runs/{id}` | запуски (+ лічильники задач черги) | 04 |
| POST | `/api/runs/{id}/cancel` | скасування | 04 |
| GET | `/api/tasks/{id}/events`, `/events/stream`, `/changes` | журнал, SSE, зміни | 04, 08 |
| GET | `/api/tasks/{id}/dataset`, `/records`, `/records/{key}` | датасет, записи, історія запису | 08 |
| GET | `/api/tasks/{id}/duplicates` | кандидати на дублікати | 07 |
| POST/GET | `/api/tasks/{id}/exports`, `/api/exports`, `/api/exports/{file}` | експорт | 09 |
| GET | `/api/dashboard` | огляд задач | 11 |
| GET/PUT | `/api/sources[/{domain}]` | реєстр джерел | 06 |
| POST | `/api/maintenance/backup`, `/api/maintenance/purge-cache` | обслуговування | 12, 05 |

Зміна будь-якого рядка цієї таблиці (шлях, метод, тіло, коди) — це зміна контракту і потребує
`specs/changes/` + оновлення `frontend/src/api.ts` і `types.ts` в тому самому PR.

## 3. Вимоги

### REQ-API-001 — Bearer-токен
Кожен шлях `/api/*`, крім `/api/health`, `/api/docs`, `/api/openapi.json`, `/api/redoc`,
**ПОВИНЕН** вимагати токен (порівняння `secrets.compare_digest`), інакше 401 з
`WWW-Authenticate: Bearer`. Токен приймається з `Authorization: Bearer`, `X-Daas-Token` або
`?token=` (для SSE і завантажень). `DAAS_DISABLE_AUTH=true` вимикає перевірку (лише для розробки).
- **Перевірка:** `tests/test_api_flow.py::test_auth_and_host_guard`

### REQ-API-002 — Токен у runtime.json
Якщо `DAAS_API_TOKEN` не задано, токен **ПОВИНЕН** читатися з `storage/runtime.json` (≥ 16
символів) або генеруватися (`token_urlsafe(32)`) і зберігатися з правами `0600`. SPA отримує
токен, вбудований у `index.html` під час віддачі (`__DAAS_TOKEN__`, `__DAAS_BOOT__`).
- **Перевірка:** — (прогалина)

### REQ-API-003 — Host-guard (DNS-rebinding)
Запит із `Host`, якого немає серед `127.0.0.1`, `localhost`, `::1`, `testserver`, `DAAS_HOST` і
хостів із `DAAS_CORS_ORIGINS`, **ПОВИНЕН** отримувати 403 — навіть для публічних шляхів.
- **Перевірка:** `tests/test_api_flow.py::test_auth_and_host_guard`

### REQ-API-004 — Заголовки безпеки
Кожна відповідь **ПОВИННА** мати `X-Content-Type-Options: nosniff`, `Referrer-Policy:
same-origin`, `X-Frame-Options: DENY` і (крім `/api/docs`) CSP `default-src 'self'; …;
object-src 'none'; base-uri 'none'; frame-ancestors 'none'`. CORS — лише `DAAS_CORS_ORIGINS`, без
credentials.
- **Перевірка:** — (прогалина)

### REQ-API-005 — Мапінг помилок
| Виняток | HTTP | Тіло |
|:---|:---:|:---|
| `RunConflict` / `ActiveRunError` | 409 | `{detail}` |
| `ComplianceError(needs_ack)` | 428 | `{detail, code, domains}` |
| `ComplianceError(forbidden \| login_required)` | 403 | `{detail, code, domains}` |
| `KeyError` (не знайдено) | 404 | `{detail}` |
| `UnsafeUrlError` | 400 | `{detail}` |
| `ValueError` у запуску (немає seed-ів тощо) | 400 | `{detail}` |
| Pydantic-валідація | 422 | FastAPI default |

Повідомлення для користувача — українською.
- **Перевірка:** `tests/test_compliance.py::test_forbidden_source_is_a_hard_stop` (403),
  `tests/test_compliance.py::test_high_risk_requires_acknowledgement_and_is_audited` (428),
  `tests/test_api_flow.py::test_full_pipeline` (409); 404/400 — прогалина

### REQ-API-006 — Health і стан при старті
`/api/health` **ПОВИНЕН** повертати `status: ok|degraded` (за `analytics.ping()`), `version`,
`llm_ready`, `offline`. На старті студія відмічає перервані запуски (REQ-RUN-004) і робить probe
LLM (REQ-LLM-003).
- **Перевірка:** `tests/test_api_flow.py::test_auth_and_host_guard` (200 без токена)

### REQ-API-007 — SPA і заглушка
Будь-який не-API шлях **ПОВИНЕН** віддавати `frontend/dist/index.html` з вбудованим токеном; якщо UI
не зібрано — HTML-підказку зі статусом 200. Невідомий `/api/*` → 404 (не SPA).
- **Перевірка:** — (прогалина)

### REQ-API-008 — Обмеження вхідних даних
`CreateTaskRequest`: `prompt` 5–4000 симв., ≤ 20 seed-ів, `title` ≤ 80. `reason` ≤ 500.
`ExportRequest.limit` 1…1 000 000. `records.limit` ≤ 500. `events.limit` ≤ 1000.
- **Перевірка:** — (прогалина)

## 4. Відкриті питання
- `ValueError` зі сховища (невалідний `f_<key>`, див. 08) не мапиться → 500. Додати глобальний
  обробник `ValueError → 400` або валідацію ключів у роуті.
