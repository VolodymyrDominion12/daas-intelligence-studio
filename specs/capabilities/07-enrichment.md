# 07 — Збагачення (waterfall), AI-колонки, кандидати на дублікати

| | |
|:---|:---|
| **Код** | `backend/app/enrichment/base.py`, `backend/app/enrichment/registry.py`, `backend/app/enrichment/site_audit.py`, `backend/app/enrichment/tech_detect.py`, `backend/app/enrichment/domain_resolver.py`, `backend/app/enrichment/ai_column.py`, `backend/app/enrichment/dedupe.py`, `backend/app/pipeline/runner.py` (`enrich`, `_enrich_row`) |
| **API** | `POST /api/tasks/{id}/enrich {column_keys}`, `GET /api/tasks/{id}/duplicates?threshold=88`, `GET /api/config` (`enrichers`) |
| **CLI** | `daas enrich <id> [--column k]`, `daas duplicates <id> [--threshold 88]` |
| **Тести** | `tests/test_api_flow.py`, `tests/test_compliance.py`, `tests/test_utils.py` |
| **Джерела** | `docs/01` модуль 3 («локальний Clay»), `docs/02` §4.2 |
| **Статус** | as-built v0.1 |

## 1. Призначення
Додати до зібраних записів те, за що платять: контакти, соцмережі, технології, сигнали наміру,
офіційний сайт, AI-оцінки — **не перезаписуючи** зібране. Дедуплікація в v0.1 — лише чесний список
кандидатів, без злиття.

## 2. Реєстр збагачувачів

| Ключ | Потребує | Дає | Мережа |
|:---|:---|:---|:---|
| `site_audit` | `website` | `email(s)`, `phone(s)`, `socials`, `tech_stack`, `has_online_booking`, `has_chat`, `site_title` | сайт компанії (+ сторінка контактів) через guard |
| `domain_resolver` | `name`, немає `website` | `website` | SearXNG + перевірка кандидата через guard |
| AI-колонки | LLM | значення колонки з `prompt` | LLM-роутер |

## 3. Вимоги

### REQ-ENR-001 — Передумови збагачення
Запуск **ПОВИНЕН** завершуватися `failed` з подією `run_failed`, якщо немає цільових колонок
(`method="enrichment"` або AI, з урахуванням `column_keys`) або датасет порожній («спершу запустіть
збір»). Активний запуск → 409.
- **Перевірка:** — (прогалина)

### REQ-ENR-002 — Waterfall лише для порожнього
Для кожного запису і кожного увімкненого збагачувача зі spec, що `applies(payload)`, рушій
**ПОВИНЕН** запускати його лише якщо хоча б одна з його цільових колонок порожня. Паралельність —
`DAAS_MAX_ENRICH_CONCURRENCY` (3).
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (`enrichments_run ≥ 1`)

### REQ-ENR-003 — Злиття без затирання
`merge_payload` **ПОВИНЕН** записувати значення лише в колонки, що є в spec, і лише якщо поточне
значення порожнє (`overwrite=False`); `email`/`phone` також заповнюють `emails`/`phones`, якщо такі
колонки є. Змінені поля отримують `Provenance(method="enrichment", confidence=0.7,
source_url=website)`. Запис з'являється новою версією, лише якщо щось змінилось.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (email, tech_stack, booking з демо-сайту)

### REQ-ENR-004 — Аудит сайту
`site_audit` **ПОВИНЕН**: завантажити `website`; зібрати email з тексту і `mailto:`, телефони,
соцмережі, стек (сигнатури `tech_detect`), ознаки онлайн-запису й чату; якщо email або телефону
немає — спробувати сторінку контактів; повертати до 10 email/телефонів; недоступний сайт →
`skipped` з причиною.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`

### REQ-ENR-005 — Пошук офіційного сайту
`domain_resolver` **ПОВИНЕН** працювати лише з налаштованим пошуком; запит `"<назва> <місто>
офіційний сайт"`; пропускати агрегатори й соцмережі (`BLOCKED_DOMAINS`); приймати кандидата, якщо
fuzzy-схожість назви з `title/h1` сайту ≥ 62.
- **Перевірка:** — (прогалина: потрібен фейковий `SearchProvider`)

### REQ-ENR-006 — AI-колонки під захистом
AI-колонка **ПОВИННА**: викликатися лише за доступного LLM (інакше `skipped` з приміткою); отримувати
на вхід **лише** поля зі spec без `pii="personal"`; результат приводиться до `dtype`, порожній —
відкидається; `confidence` обмежується `[0.2, 0.9]`; `provenance.method="llm"`, `note` ≤ 240
символів; витрати йдуть у бюджет запуску (`prefer="quality"`).
- **Перевірка:** — (прогалина: потрібен mock-провайдер)

### REQ-ENR-007 — Guard діє і для збагачення
Запити збагачувачів **ПОВИННІ** йти через `PolicyGuardFetcher` у `run_scope` запуску.
- **Перевірка:** `tests/test_compliance.py::test_forbidden_enrichment_target_is_audited`

### REQ-ENR-008 — Кандидати на дублікати
`find_duplicates(rows, threshold)` **ПОВИНЕН** використовувати blocking (домен або перші 4 символи
нормалізованої назви; записи з доменом додатково блокуються за назвою), рахувати
`token_set_ratio` нормалізованих назв, повертати пари з `score ≥ threshold` (0–1 у відповіді), не
більше 200, відсортовані за спаданням. **НЕ ПОВИНЕН** зливати записи.
- **Перевірка:** `tests/test_utils.py::test_duplicate_candidates`,
  `tests/test_api_flow.py::test_full_pipeline` (`scanned == 3`)

### REQ-ENR-009 — Якість ідентичності
`identity_quality(payload)` **ПОВИНЕН** оцінювати, наскільки запис придатний до дедуплікації:
домен 1.0 > телефон > назва; порожній — 0.2.
- **Перевірка:** `tests/test_utils.py::test_duplicate_candidates`

## 4. Спостережувана поведінка / відкриті питання
- Через `overwrite=False` повторне збагачення **не оновлює** вже заповнені значення (включно з
  AI-колонками і змінами на сайті компанії). Це захищає зібране, але робить збагачення «одноразовим».
  Потрібне рішення: режим `refresh` / TTL для полів збагачення?
- `SourcePolicy.allowed_fields` зберігається, але не застосовується.

## 5. Прогалини
REQ-ENR-001, REQ-ENR-005, REQ-ENR-006 — без тестів.
