# 10 — LLM-шар: каскад провайдерів, бюджет, кеш, захист

| | |
|:---|:---|
| **Код** | `backend/app/llm/router.py` (`LLMRouter`), `backend/app/llm/providers.py` (`OpenAICompatProvider`, `MockProvider`, `build_providers`, `PRICING`, `estimate_cost`), `backend/app/llm/base.py` (`LLMRequest`, `LLMResponse`, `LLMBudget`), `backend/app/llm/guard.py`, `backend/app/llm/parsing.py`, `backend/app/llm/prompts.py` |
| **Налаштування** | `DAAS_LLM_PROVIDER`, `DAAS_LLM_LOCAL_BASE_URL/MODEL`, `DAAS_LLM_BASE_URL/API_KEY/MODEL`, `DAAS_LLM_FALLBACK_*`, `DAAS_LLM_BUDGET_USD`, `DAAS_LLM_CACHE`, `DAAS_LLM_REDACT_PII`, `DAAS_MAX_LLM_CALLS_PER_RUN` |
| **Тести** | `tests/test_llm.py` |
| **Джерела** | ADR-04, ADR-06, `docs/02` §3 |
| **Статус** | as-built v0.1 |

## 1. Призначення
Один протокол (OpenAI-сумісний) для локальних (Ollama, LM Studio, vLLM) і хмарних моделей; LLM —
опційний: студія повністю працює без нього. Кожен виклик має мету, бюджет, кеш і запис у ledger.
Вміст сторінок — **недовірені дані**.

## 2. Вимоги

### REQ-LLM-001 — Без провайдера — явна відмова, а не виняток
Якщо провайдерів немає (`DAAS_LLM_PROVIDER=disabled` або нічого не налаштовано), `router.available`
**ПОВИНЕН** бути `false`, а `complete_json` — повертати `LLMResponse(ok=false,
error="no_llm_provider_configured")`.
- **Перевірка:** `tests/test_llm.py::test_router_without_providers_is_explicit`

### REQ-LLM-002 — Каскад провайдерів
Порядок побудови: `local` (якщо задано `LLM_LOCAL_BASE_URL`) → `primary` (URL + ключ) → `fallback`.
`prefer="local"` ставить локальні першими; `prefer="quality"` — хмарні першими; `any` — як є.
Невдача провайдера → наступний; недоступний локальний / `ConnectError` / таймаут позначає
провайдер нездоровим на 120 с. `mock` використовується **лише** при явному
`DAAS_LLM_PROVIDER=mock`.
- **Перевірка:** — (прогалина: немає тесту порядку і fallback-у)

### REQ-LLM-003 — Probe доступності
На старті і в `/api/config` (якщо минуло > 30 с) роутер **ПОВИНЕН** перевіряти `GET <base>/models`
(таймаут 1.5–2 с); 2xx/3xx/404 — доступний. UI не обіцяє модель, яка недоступна.
- **Перевірка:** — (прогалина)

### REQ-LLM-004 — Бюджет на запуск
`LLMBudget` **ПОВИНЕН**: дозволяти виклики з оцінкою ≤ 0 (локальні/безкоштовні) завжди; інакше —
лише якщо `remaining ≥ оцінка`; `charge` додає фактичну вартість і рахує виклики. Провайдер, на
якого не вистачає бюджету, пропускається; якщо всіх пропущено — помилка з `budget`.
- **Перевірка:** `tests/test_llm.py::test_budget_semantics`,
  `tests/test_llm.py::test_router_skips_when_budget_is_exhausted`

### REQ-LLM-005 — Оцінка вартості
`estimate_cost(model, in, out)` **ПОВИНЕН** рахувати за таблицею `PRICING` ($/1M токенів);
локальні й невідомі моделі — 0.
- **Перевірка:** `tests/test_llm.py::test_cost_estimate_uses_pricing_table`

### REQ-LLM-006 — Кеш відповідей
Успішна відповідь **ПОВИННА** кешуватися на диску (`storage/llm_cache`) за ключем
`hash(purpose|system|user|schema|max_tokens|temperature)` + провайдер; повторний запит повертає
`cached=true` без виклику провайдера. Невдалі відповіді не кешуються.
- **Перевірка:** `tests/test_llm.py::test_router_caches_successful_responses`

### REQ-LLM-007 — Ledger витрат
Кожен некешований виклик **ПОВИНЕН** записуватися в `llm_calls` (purpose, provider, model, tokens,
usd, task_id, run_id); `run_spend`/`task_spend` агрегують.
- **Перевірка:** `tests/test_storage.py::test_events_and_llm_ledger`

### REQ-LLM-008 — Недовірений вміст і injection
Вміст сторінки **ПОВИНЕН** передаватися лише в `user`-повідомленні всередині
`<untrusted_page>…</untrusted_page>`; інструкції — лише в system-промптах; моделі не отримують
tools. `detect_injection` повідомляє маркери (`ignore_instructions`, `system_override`,
`role_hijack`, `reveal_secret`, `exfiltrate`, `tool_hijack`, `json_breakout`) як попередження.
- **Перевірка:** `tests/test_llm.py::test_injection_detection_and_wrapping`

### REQ-LLM-009 — PII не йде в хмару
Для не-локальних провайдерів текст користувацького повідомлення **ПОВИНЕН** проходити
`redact_pii` (email → `[email]`, телефон → `[phone]`) і обрізатися до 60 000 символів. AI-колонки
не отримують `personal`-полів (REQ-ENR-006).
- **Перевірка:** `tests/test_llm.py::test_injection_detection_and_wrapping` (редагування);
  гілка «лише для хмари» — прогалина

### REQ-LLM-010 — Стійкий парсинг JSON
`parse_json_loose` **ПОВИНЕН** витягати JSON з code-fence, балакучої відповіді, прибирати кінцеві
коми; сміття → `ValueError`.
- **Перевірка:** `tests/test_llm.py::test_parse_json_loose`,
  `tests/test_llm.py::test_parse_json_loose_rejects_garbage`

### REQ-LLM-011 — Жодного виконання LLM-коду
Результат LLM **НЕ ПОВИНЕН** виконуватися як код чи селектори з побічними ефектами; він лише
валідується за схемою і приводиться до типів (ADR-06).
- **Перевірка:** архітектурна вимога; перевіряється рев'ю (у коді немає `eval/exec`)

## 3. Відкриті питання
- `router.bind(task_id, run_id)` змінює стан **спільного** роутера; при паралельних запусках різних
  задач записи ledger можуть атрибутуватися не тому запуску. Перенести `task_id/run_id` у
  `LLMRequest` або в `run_scope`?
- Витрати планування мають `task_id=None` (P1-09).
