# 01 — Планування задачі (промпт → TaskSpec)

| | |
|:---|:---|
| **Код** | `backend/app/pipeline/planner.py`, `backend/app/presets.py`, `backend/app/discovery.py`, `backend/app/llm/prompts.py` (`PLANNER_SYSTEM`), `backend/app/runtime.py` (`Studio.create_task`, `update_task`, `update_column`, `reorder_columns`) |
| **API** | `POST /api/tasks`, `PATCH /api/tasks/{id}`, `PATCH /api/tasks/{id}/limits`, `POST/PATCH/DELETE /api/tasks/{id}/columns[/{key}]`, `POST /api/tasks/{id}/columns/reorder`, `DELETE /api/tasks/{id}` |
| **CLI** | `daas task create --prompt … --seed … [--api-source …]`, `daas task list`, `daas task show` |
| **Тести** | `tests/test_api_flow.py`, `tests/test_compliance.py`, `tests/test_open_api_source.py`, `tests/test_metrics_and_spend.py`, `tests/test_spec_editor_and_diff.py` |
| **Джерела** | `docs/01` модуль 1, `docs/03` §2, `docs/03` §3.7 (`P1-10`) |
| **Статус** | as-built (P1-10) |

## 1. Призначення
Перетворити бізнес-запит природною мовою і 0–20 seed-URL на валідний `TaskSpec`: колонки з типом,
PII-класом і методом, план збагачення, ліміти й знімок політик джерел. Працює **без LLM** (офлайн-
шаблони); LLM, якщо доступний, покращує план.

## 2. Потік

```text
prompt, seeds ─► assert_logged_out(seed)  (кожен непорожній seed)
             ─► build_spec(preset за ключовими словами)        ← завжди
             ─► LLM-план (prefer=quality), якщо router.available ─► _merge(base, llm)
             ─► discovery через SearXNG, якщо seeds порожні
             ─► title, warning, Limits з налаштувань, source_policy
             ─► відкинути знайдені seed-и з токенами ─► store.create_task ─► події
```

## 3. Вимоги

### REQ-PLAN-001 — Офлайн-шаблон завжди дає план
Планувальник **ПОВИНЕН** будувати `TaskSpec` без LLM: обирається пресет із найбільшою кількістю
збігів ключових слів у промпті (`tender`, `job`, `property`, `local_business`, `product`,
`company`; за замовчуванням — останній у списку, `company`). У такому разі `warning` повідомляє,
що використано локальний шаблон.
- **Сценарій:** GIVEN LLM вимкнений WHEN промпт «Збери стоматологічні клініки Києва…»
  THEN `entity="local_business"`, колонки містять `name, city, phone, website, rating`.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`

### REQ-PLAN-002 — AI-колонки за ключовими словами
Шаблон **ПОВИНЕН** додавати AI-колонки (`method="llm"` з `prompt`), якщо промпт містить:
`біль/pain/пріоритет/score/скоринг` → `pain_score` (integer);
`керівник/директор/decision/лпр/контактна особа` → `decision_maker` (`pii="personal"`);
`відгук/review/скарг/проблем` → `customer_pain`.
- **Перевірка:** — (прогалина)

### REQ-PLAN-003 — План збагачення з колонок
Якщо серед колонок є `website`, шаблон **ПОВИНЕН** додати `site_audit` (email, phone, tech_stack)
і `domain_resolver` (website); кожна колонка з `method="enrichment"` додається до полів
`site_audit`.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (наявність `site_audit`)

### REQ-PLAN-004 — LLM-план покращує, але не ламає шаблон
Якщо LLM доступний, планувальник **ПОВИНЕН** запросити JSON-план (≤ 18 колонок) і:
- відкидати невалідні колонки поодинці; невідомий `method` → `css`; `llm` без `prompt` → `css`;
- відкидати збагачувачі, яких немає в реєстрі;
- вважати план невдалим (→ лише шаблон), якщо валідних колонок < 3 або відповідь не JSON;
- зливати: колонки LLM + відсутні колонки шаблону; `name` гарантовано присутня; seed-и, ліміти і
  `warning` — із шаблону.
- **Перевірка:** — (прогалина: немає тесту з фейковим LLM-планом)

### REQ-PLAN-005 — Авто-пошук seed-ів
Якщо після планування seed-ів немає і налаштовано пошук (`DAAS_SEARXNG_URL`), планувальник
**СЛІД** знайти до 5 стартових URL (запит із промпту без стоп-слів, один URL на домен).
Без seed-ів `warning` **ПОВИНЕН** підказувати додати 1–3 посилання.
- **Перевірка:** — (прогалина: потребує фейкового `SearchProvider`)

### REQ-PLAN-006 — Ліміти з налаштувань
`spec.limits` **ПОВИНЕН** заповнюватися з `DAAS_MAX_PAGES_PER_RUN`, `DAAS_MAX_DEPTH`,
`DAAS_LLM_BUDGET_USD`, `DAAS_SAMPLE_SIZE`, `DAAS_PER_DOMAIN_RPS`. `title` користувача обрізається до
60 символів.
- **Перевірка:** — (прогалина)

### REQ-PLAN-007 — Знімок політик джерел
Для кожного домену seed-а планувальник **ПОВИНЕН** покласти в `spec.source_policy` поточну політику
з реєстру; невідомий домен реєструється з `tos_risk="unknown"`. Цей знімок **не** використовується
для рішень комплаєнсу (див. REQ-COMP-013, REQ-EXP-006 — лише живий реєстр).
- **Перевірка:** `tests/test_api_flow.py::test_task_editing_and_column_management` (домен seed-а
  з'являється в `/api/sources`)

### REQ-PLAN-008 — Лише публічні seed-и
Створення задачі **ПОВИННО** відхилятися (HTTP 403, `code="login_required"`), якщо будь-який
переданий seed містить облікові дані або токен сесії. Seed-и з токенами, знайдені пошуком,
**ПОВИННІ** відкидатися до збереження з приміткою в `assumptions`. Те саме правило діє для
`PATCH /api/tasks/{id}` зі spec.
- **Перевірка:** `tests/test_compliance.py::test_seed_with_credentials_is_rejected`,
  `tests/test_compliance.py::test_discovered_seeds_with_tokens_are_dropped`

### REQ-PLAN-009 — Події створення
Після збереження задачі **ПОВИННІ** записуватися події `task_created` (ключі колонок, чи доступний
LLM) і `plan_ready` (`data.spec`).
- **Перевірка:** — (прогалина)

### REQ-PLAN-010 — Редагування колонок і лімітів
- `POST /columns` з наявним ключем **ПОВИНЕН** повертати 409; додавання пише `column_added`.
  Колонка з `method="enrichment"` або ключем `email/phone/tech_stack/has_online_booking/has_chat/
  socials` автоматично прив'язується до збагачувача `site_audit`.
- `DELETE /columns/{key}` **НЕ ПОВИНЕН** видаляти останню колонку (400).
- `PATCH /limits` і `PATCH /tasks/{id}` зі spec **ПОВИННІ** зберігати нові ліміти.
- `DELETE /tasks/{id}` **ПОВИНЕН** видаляти задачу і її датасет у DuckDB.
- **Перевірка:** `tests/test_api_flow.py::test_task_editing_and_column_management`

### REQ-PLAN-011 — Помилка планування зрозуміла користувачу
Неочікувана помилка планувальника **ПОВИННА** повертати 400 «Не вдалося побудувати план: …»;
`ComplianceError` прокидається як є (403/428).
- **Перевірка:** — (прогалина)

### REQ-PLAN-012 — Шаблон задачі для відкритого API закупівель і аукціонів
Планувальник **ПОВИНЕН** розпізнавати запит на збір із відкритого API тендерів/аукціонів (Prozorro
та Prozorro.Продажі за доменами або ключовими словами) і формувати готовий набір колонок
(`method="api_json"`) і `ApiSourceSpec` без участі LLM (`limits.max_depth = 0`) та без персональних
даних (`pii="personal"`).
- **Перевірка:** `tests/test_open_api_source.py::test_openprocurement_preset_builds_non_pii_api_spec`

### REQ-PLAN-013 — Атрибуція витрат планування до створеної задачі
Коли під час створення задачі виконується платний/некешований LLM-виклик планування (`purpose="plan"`),
система **ПОВИННА** прив'язати цей запис витрат до щойно створеного `task_id` одразу після збереження
задачі, включити `plan_llm_usd` у подію створення задачі та відображати сумарні витрати задачі у
`GET /api/tasks/{id}`. Якщо створення задачі провалюється до збереження, виклик лишається в журналі
під унікальним plan-ідентифікатором запуску без присвоєння чужого `task_id`.
- **Перевірка:** `tests/test_metrics_and_spend.py::test_plan_spend_attributed_to_created_task`

### REQ-PLAN-014 — Редагування, перестановка колонок і ручні CSS-селектори в `TaskSpec`
Система **ПОВИННА** дозволяти змінювати набір колонок задачі через типізований контракт:
- додавати колонки будь-якого підтримуваного методу (`css`, `json_ld`, `regex`, `llm`, `enrichment`, `manual`, `api_json`), автоматично прив'язуючи збагачувач `site_audit` за потреби;
- оновлювати параметри наявної колонки (`title`, `dtype`, `description`, `required`, `pii`, `method`, `aliases`, `selector`, `attr`, `prompt`) із записом події `column_updated`;
- переставляти порядок колонок задачі із збереженням унікальності та повноти набору ключів і записом події `columns_reordered`;
- валідувати синтаксис непорожнього ручного CSS-селектора до збереження (як при створенні/оновленні колонки, так і при оновленні всього `spec`).
- **Сценарій:** GIVEN задача зі списком колонок `[name, city, phone]` WHEN користувач змінює заголовок `phone` на «Гаряча лінія», тип на `phone`, задає ручний селектор `.clinic-card__phone` і переставляє порядок колонок THEN оновлена спека зберігається, новий порядок фіксується, а в журналі з'являються події `column_updated` і `columns_reordered`.
- **Відмова:** GIVEN синтаксично невалідний CSS-селектор (наприклад `div[[broken`) або дублікат/відсутній ключ під час перестановки WHEN надсилається запит оновлення/перестановки THEN система відхиляє зміну з HTTP `400`/`422` і зрозумілим повідомленням українською без пошкодження збереженої спеки.
- **Перевірка:** `tests/test_spec_editor_and_diff.py::test_column_update_reorder_and_selector_validation`

## 4. Інваріанти
- План завжди валідний `TaskSpec` (≥ 1 колонка, унікальні ключі) — інакше задача не створюється.
- Витрати планування атрибутуються створеній задачі одразу після збереження (`P1-09`).

## 5. Прогалини
REQ-PLAN-002, 004, 005, 006, 009, 011 — без тестів. Пріоритет: REQ-PLAN-004 (фейковий провайдер
у `LLMRouter.providers`), REQ-PLAN-005 (фейковий `SearchProvider`).
