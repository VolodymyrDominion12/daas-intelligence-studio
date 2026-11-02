# 06 — Комплаєнс: реєстр джерел, logged-out, аудит

| | |
|:---|:---|
| **Код** | `backend/app/compliance.py`, `backend/app/runtime.py` (`_compliance_gate`, `_create_run`), `backend/app/db/sqlite_store.py` (`source_registry`, `create_run(audit=…)`), `backend/app/api/data.py` (`/sources`) |
| **API** | `GET /api/sources`, `PUT /api/sources/{domain}`; параметри `acknowledge_high_risk`, `reason` у `/calibrate` і `/run` |
| **CLI** | `daas calibrate|collect <id> --ack-high-risk --reason "…"` |
| **Тести** | `tests/test_compliance.py` |
| **Джерела** | `docs/03` §3.5 (P1-04), `docs/02` §2, ADR-09 |
| **Статус** | as-built (P1-04 ✅) |

## 1. Призначення
Зробити реєстр джерел **обов'язковим**: два шари — гейт перед запуском і guard на кожному запиті
(включно з редиректами, обходом посилань, збагаченням і пошуком сайту). Лише публічні сторінки
без авторизації. Це інженерні запобіжники, а не юридична консультація.

## 2. Вимоги — logged-out

### REQ-COMP-001 — URL з обліковими даними не запитуються і не зберігаються
URL **ПОВИНЕН** вважатися порушенням, якщо має `user:pass@` або непорожній query-параметр-
облікові дані: точні ключі `AUTH_QUERY_KEYS` (`token`, `access_token`, `session`, `sid`,
`api_key`, `password`, `signature`, `x_amz_*`, …) або суфікси `token`, `sessionid`, `_secret`,
`_password`, `*_key` з `api/access/secret/private/auth`. **НЕ** є порушенням: пагінаційні курсори
(`pageToken`, `nextToken`, `cursor`, `continuation`, `csrf…`) і загальні фільтри (`key`, `sig`).
- **Перевірка:** `tests/test_compliance.py::test_login_violation_detects_credentials`,
  `tests/test_compliance.py::test_login_violation_allows_public_urls`

### REQ-COMP-002 — Редагування URL у журналах
У подіях і повідомленнях URL **ПОВИНЕН** записуватися через `redact_url`: без userinfo, значення
облікових параметрів → `***`, інші параметри зберігаються.
- **Перевірка:** `tests/test_compliance.py::test_redact_url_hides_credentials`,
  `tests/test_compliance.py::test_guard_reports_once_and_redacts`

## 3. Вимоги — реєстр і гейт

### REQ-COMP-003 — Ефективна політика: найточніша, батьківська покриває піддомени
Для хоста **ПОВИННА** братися перша політика з `tos_risk ≠ unknown` у ланцюжку
`a.b.example.com → b.example.com → example.com`; точніший запис має пріоритет. Домени в реєстрі
нормалізуються (`WWW.Example.com` → `example.com`).
- **Перевірка:** `tests/test_compliance.py::test_domain_chain`,
  `tests/test_compliance.py::test_parent_policy_covers_subdomains`,
  `tests/test_compliance.py::test_policy_domains_are_normalized`

### REQ-COMP-004 — Гейт запуску: forbidden → стоп, high → підтвердження
Перед калібруванням і збором кожен seed **ПОВИНЕН** перевірятися по живому реєстру:
- облікові дані → `ComplianceError(login_required)` → HTTP 403;
- будь-який `forbidden` → `forbidden` → HTTP 403; підтвердження **не** розблоковує;
- будь-який `high` без `acknowledge_high_risk=true` і причини ≥ 10 символів → `needs_ack` →
  HTTP **428**; тіло `{detail, code, domains}`;
- відмова пише подію `compliance_blocked` (`level=error`, `data.code/domains/run_kind`) і
  **не** створює запуск.
- **Перевірка:** `tests/test_compliance.py::test_check_run_rules`,
  `tests/test_compliance.py::test_forbidden_source_is_a_hard_stop`,
  `tests/test_compliance.py::test_high_risk_requires_acknowledgement_and_is_audited`

### REQ-COMP-005 — Аудит підтвердження атомарний із запуском
Підтверджений запуск по `high`-джерелах **ПОВИНЕН** створюватися в одній транзакції з подією
`compliance_override` (`data.domains`, `data.reason`, `data.at`). Якщо запуск відхилено
(активний запуск), audit-запис не лишається.
- **Перевірка:** `tests/test_compliance.py::test_exclusive_run_creation_is_atomic_with_audit`,
  `tests/test_compliance.py::test_high_risk_requires_acknowledgement_and_is_audited`

## 4. Вимоги — guard на кожному запиті

### REQ-COMP-010 — `PolicyGuardFetcher` обгортає будь-який fetcher
Кожен запит студії **ПОВИНЕН** проходити через guard: logged-out, `forbidden` → відмова
`kind="policy"` без звернення до внутрішнього fetcher-а; `high` — лише якщо домен підтверджено
для поточного `run_scope` (поза scope — відмова). Перевірка повторюється для `final_url`
(рушії/кеш, що слідують редиректам самі).
- **Перевірка:** `tests/test_compliance.py::test_guard_fetcher_blocks_forbidden_and_logged_in`,
  `tests/test_compliance.py::test_high_risk_needs_acknowledgement_inside_the_run`,
  `tests/test_compliance.py::test_guard_rejects_forbidden_final_url`

### REQ-COMP-011 — Бічні двері закриті
Сторінки, знайдені обходом, цілі збагачення (`site_audit`) і пошуку **ПОВИННІ** блокуватися так само,
як seed-и; заборонені URL пропускаються (задача `skipped`), у датасет не потрапляють.
- **Перевірка:** `tests/test_compliance.py::test_forbidden_website_is_skipped_during_collection`,
  `tests/test_compliance.py::test_forbidden_enrichment_target_is_audited`

### REQ-COMP-012 — Одна подія на домен і правило за запуск
Guard **ПОВИНЕН** писати `compliance_blocked` (`level=warning`, редагований URL, `run_id`) лише
раз на пару (домен, правило) у межах запуску; збій запису події не ламає завантаження.
- **Перевірка:** `tests/test_compliance.py::test_guard_reports_once_and_redacts`

### REQ-COMP-013 — Живий реєстр, а не знімок
Рішення гейту, guard-а і примітка про джерела в експорті (`source_note`) **ПОВИННІ** брати політику з
живого реєстру на момент дії, а не з `spec.source_policy`.
- **Перевірка:** `tests/test_compliance.py::test_source_note_uses_live_registry`,
  `tests/test_compliance.py::test_high_risk_needs_acknowledgement_inside_the_run`

### REQ-COMP-014 — Реєстр через API
`GET /api/sources` **ПОВИНЕН** повертати політики з реєстру плюс домени seed-ів усіх задач
(невідомі — `unknown`). `PUT /api/sources/{domain}` нормалізує домен (400, якщо порожній),
зберігає `tos_risk`, `license`, `notes` (≤ 1000 симв.), `allowed_fields`, і `reviewed_at=now`,
якщо `reviewed=true`.
- **Перевірка:** `tests/test_api_flow.py::test_task_editing_and_column_management`

## 5. Залишилось (беклог, ще не вимоги)
DSAR (`daas dsar --email … --delete`), retention job для PII, audit export, застосування
`allowed_fields` (поле зберігається, але ніде не застосовується). Див. `docs/03` §3.5.
