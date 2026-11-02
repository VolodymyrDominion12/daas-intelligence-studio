# 02 — Калібрування (тестова вибірка → детермінована схема)

| | |
|:---|:---|
| **Код** | `backend/app/pipeline/calibration.py` (`Calibrator`), `backend/app/extraction/learner.py`, `backend/app/extraction/synonyms.py`, `backend/app/pipeline/runner.py` (`PipelineRunner.calibrate`) |
| **API** | `POST /api/tasks/{id}/calibrate?force&acknowledge_high_risk&reason`, `GET /api/tasks/{id}/calibration` |
| **CLI** | `daas calibrate <id> [--ack-high-risk --reason "…"]` |
| **Тести** | `tests/test_learner.py`, `tests/test_api_flow.py` |
| **Джерела** | `docs/01` модуль 2 (двофазний парсинг), ADR-06 |
| **Статус** | as-built v0.1 |

## 1. Призначення
Головна економія токенів: на 2–4 сторінках вивести CSS-схему **без LLM**, показати прев'ю й
покриття полів, зберегти схему для масового збору. LLM — лише якщо детерміновані стратегії дали 0.

## 2. Вимоги

### REQ-CAL-001 — Передумови запуску
Калібрування **ПОВИННО** відхилятися з 400, якщо в задачі немає seed-ів; з 409, якщо в задачі вже
є активний запуск (якщо не `force`); проходити комплаєнс-гейт (див. REQ-COMP-004).
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (успішний запуск),
  `tests/test_compliance.py::test_forbidden_source_is_a_hard_stop`

### REQ-CAL-002 — Вибірка сторінок
Калібратор **ПОВИНЕН** завантажити до `min(spec.limits.sample_size, DAAS_SAMPLE_SIZE)` seed-ів
паралельно. Якщо сторінок менше за розмір вибірки, він **ПОВИНЕН** добрати same-site посилання з
уже завантажених сторінок (до 6 з кожної, без seed-ів). Помилки окремих URL — попередження, не
падіння.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`

### REQ-CAL-003 — Навчання схеми без LLM
`learn_schema(samples, spec)` **ПОВИНЕН**:
- на сторінці зі списком знайти повторювану групу елементів (container) і зіставити колонки з
  нащадками за класами / `itemprop` / мітками-синонімами (uk/en);
- на сторінці деталей без повторів — побудувати схему з `container="body"`, `item_scope=False`,
  використовуючи текстові мітки;
- оцінити кожного кандидата на **всіх** семплах: `score = records × середнє покриття`; обрати
  найкращий за (`є записи`, `score`, кількість полів);
- повернути порожню схему (`container=""`) або `score ≤ 0` з приміткою, якщо нічого не знайдено.
- **Перевірка:** `tests/test_learner.py::test_learns_catalog_schema_and_extracts_items`,
  `tests/test_learner.py::test_learns_detail_page_schema_using_labels`,
  `tests/test_learner.py::test_learner_handles_page_without_items`

### REQ-CAL-004 — Схема перевикористовується
Вивчені селектори **ПОВИННІ** давати однаковий результат на сторінках того самого шаблону і
зберігатися в `selector_cache` під ключем `(домен першого seed-а, "auto:<entity>")` лише якщо є
поля і `score > 0`.
- **Перевірка:** `tests/test_learner.py::test_selectors_are_reusable_across_pages`

### REQ-CAL-005 — Прев'ю тим самим рушієм
Прев'ю **ПОВИННО** будуватися тим самим `ExtractionEngine`, що й масовий збір, на 2–3 сторінках
(до 5 записів на сторінку) з `allow_llm=False`. Звіт містить `strategy` (`css`/`json_ld`/`llm`/
`none`), `schema`, `samples`, `coverage` (% заповненості кожної не-AI колонки), `records_found`,
`pages_tried`, `pages_ok`, `warnings` (≤ 20, унікальні).
- **Сценарій:** GIVEN демо-каталог із 3 клініками WHEN калібрування THEN `records_found ≥ 3`,
  `strategy ∈ {css, json_ld}`, `coverage.name = 100`, `samples` непорожні.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`

### REQ-CAL-006 — LLM лише як останній крок
Якщо детерміновані стратегії дали 0 рядків і LLM доступний, калібратор **ПОВИНЕН** спробувати LLM
рівно на одній сторінці, позначити `strategy="llm"`, `schema=None`, записати
`estimated_llm_cost_usd` і попередження про вартість на великих обсягах (подія `llm_fallback`).
- **Перевірка:** — (прогалина)

### REQ-CAL-007 — Результат запуску і статус задачі
Звіт **ПОВИНЕН** зберігатися в `calibrations` (по одному на задачу, перезаписується). Запуск
завершується `succeeded`, якщо є рядки, інакше `partial`; задача → `ready` або `draft`.
Порожня вибірка дає підказку, що seed має бути сторінкою зі списком/картками.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (`succeeded|partial`, звіт доступний)

### REQ-CAL-008 — Збір вимагає калібрування
`POST /run` без збереженого калібрування **ПОВИНЕН** повертати 409 «Спершу запустіть
калібрування…», якщо не передано `skip_calibration=true`.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline` (`refused.status_code == 409`)

## 3. Інваріанти
- Калібрування не пише записи в DuckDB і не створює версію датасету.
- Схема — декларативні селектори (`CssSchema`), ніколи не код.
- Одна схема на `domain + entity`; сайти з кількома шаблонами — відоме обмеження (`docs/03` §7 п.5,
  P1-10).

## 4. Прогалини
- REQ-CAL-006 (LLM-fallback калібрування) — немає тесту з mock-провайдером.
- Немає тесту на добір додаткових сторінок, коли seed — лендинг без повторів.
