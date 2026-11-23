# 03 — Екстракція (сторінка → записи)

| | |
|:---|:---|
| **Код** | `backend/app/extraction/engine.py`, `backend/app/extraction/jsonld.py`, `backend/app/extraction/css.py`, `backend/app/extraction/api_json.py`, `backend/app/extraction/llm_extractor.py`, `backend/app/extraction/base.py`, `backend/app/extraction/learner.py` (`fill_from_text`, `coerce_any`), `backend/app/scraping/page.py` (`extract_json_ld`) |
| **Тести** | `tests/test_extraction.py`, `tests/test_learner.py`, `tests/test_open_api_source.py`, `tests/test_quarantine.py` |
| **Джерела** | ADR-06, `docs/02` §3 |
| **Статус** | as-built (P1-08) |

## 1. Призначення
Один рушій для калібрування і масового збору: детерміновані стратегії (JSON-LD, CSS) спершу, LLM —
лише коли вони нічого не дали і це дозволено. Результат — `ExtractedRecord` зі стабільним ключем і
походженням кожного поля.

## 2. Каскад стратегій (`ExtractionEngine.extract`)

| Умова | База | Доповнення (лише 1 сутність, `_same_entity`) |
|:---|:---|:---|
| CSS дав > 1 запис | CSS | JSON-LD, якщо рівно 1 елемент |
| інакше JSON-LD дав записи | JSON-LD | CSS, якщо рівно 1 елемент |
| інакше CSS дав 1 запис | CSS | JSON-LD, якщо рівно 1 |
| нічого | `none` → LLM, якщо `allow_llm` | — |

Доповнення лише заповнює відсутні поля (`setdefault`), ніколи не перезаписує базу.

## 3. Вимоги

### REQ-EXT-001 — JSON-LD має пріоритет на сторінці однієї сутності
На сторінці з придатним блоком schema.org і без списку CSS рушій **ПОВИНЕН** обрати `json_ld`;
поля мапляться через `PATH_MAP` і синоніми; `provenance.method = "json_ld"` для всіх полів.
- **Перевірка:** `tests/test_extraction.py::test_jsonld_extraction_maps_columns`,
  `tests/test_extraction.py::test_engine_uses_jsonld_before_css`

### REQ-EXT-002 — Битий JSON-LD не ламає сторінку
Некоректний `<script type="application/ld+json">` **ПОВИНЕН** ігноруватися (0 елементів, без
винятку). Корисним вважається блок, що має хоча б одне з `name/headline/url/telephone/email/price`.
- **Перевірка:** `tests/test_extraction.py::test_jsonld_survives_broken_script`

### REQ-EXT-003 — Список через вивчену CSS-схему
Якщо схема має `container`, рушій **ПОВИНЕН** витягти по запису на кожен контейнер; записи мають
різні `record_key`. Значення приводяться до `dtype` колонки (телефон → E.164-подібний, рейтинг →
число тощо).
- **Перевірка:** `tests/test_extraction.py::test_engine_uses_learned_css_schema`,
  `tests/test_learner.py::test_learns_catalog_schema_and_extracts_items`

### REQ-EXT-004 — Без LLM і без даних — порожньо і безкоштовно
Якщо детерміновані стратегії нічого не дали, а `allow_llm=False` або LLM недоступний, рушій
**ПОВИНЕН** повернути 0 записів і `llm_calls = 0`.
- **Перевірка:** `tests/test_extraction.py::test_engine_without_llm_and_without_data_returns_empty`

### REQ-EXT-005 — Детерміноване дозаповнення
Для кожного запису рушій **ПОВИНЕН** дозаповнювати порожні колонки з тексту сторінки
(`email`, `phone`, числові колонки з `price` у ключі), `website` — з домену сторінки, `name` —
з першого рядка тексту, якщо такі колонки є в spec. AI-колонки не дозаповнюються.
- **Перевірка:** `tests/test_learner.py::test_learns_detail_page_schema_using_labels` (частково)

### REQ-EXT-006 — Фільтр придатності запису і маршрутизація відхилених у карантин
Значення, що не приводяться до `dtype` колонки, **ПОВИННІ** видалятися **до** підрахунку
непорожніх полів, а не-схемні ключі елемента — відфільтровуватися. Запис відхиляється (і
повертається в `outcome.quarantined` з причиною, якщо мав ≥ 1 витягнутого поля), коли після
очищення має `< 2` валідних полів (за наявності ≥ 2 джерельних колонок) або жодного ідентифікуючого
поля (`name, title, company, website, phone, email, address`).
- **Перевірка:** `tests/test_quarantine.py::test_extraction_usability_counts_after_coercion_and_flags_quarantine`

### REQ-EXT-007 — Ключ і походження запису
`to_records` **ПОВИНЕН** відкидати порожні значення, рахувати ключ через `record_key_for`
(fallback — URL сторінки) і проставляти кожному полю `Provenance` з `source_url = final_url`,
`confidence ∈ [0.25, 0.98]` (для колонок `method="llm"` — ≤ 0.65), `verified = true` лише для
`json_ld`/`css`.
- **Перевірка:** `tests/test_extraction.py::test_engine_uses_jsonld_before_css`

### REQ-EXT-008 — LLM-екстракція під захистом
LLM-екстрактор **ПОВИНЕН**: передавати сторінку лише всередині `<untrusted_page>`, обрізати її до
48 000 символів, повідомляти маркери injection як попередження, приймати лише ключі зі схеми
не-AI колонок, обмежувати кількість елементів (`max_items`, 25), рахувати виклик навіть при
невдачі. Див. також 10-llm.
- **Перевірка:** `tests/test_llm.py::test_injection_detection_and_wrapping` (обгортка й детектор);
  сам екстрактор — прогалина

### REQ-EXT-009 — Посилання для обходу
`extract_links` **ПОВИНЕН** повертати абсолютні http(s)-URL того самого сайту без `mailto:` і
навігаційного шуму.
- **Перевірка:** `tests/test_extraction.py::test_links_are_absolute_and_filtered`

### REQ-EXT-010 — Декларативна екстракція з JSON-відповіді
`api_json.py` **ПОВИНЕН** детерміновано без LLM витягувати цілі пагінації/деталей та записи за
`ApiSourceSpec` (dot-path з індексами та `|`-резервами), приводити значення до типів колонок,
виставляти стабільний ключ `api:<id>` та `Provenance(method="api_json", confidence=0.98, verified=True)`.
На невалідному JSON повертаються 0 записів і попередження без падіння прогону.
- **Перевірка:** `tests/test_open_api_source.py::test_json_extractor_maps_fields_and_handles_broken_json`

## 4. Спостережувана поведінка / відкриті питання
- (немає)

## 5. Прогалини
REQ-EXT-008 (екстрактор із mock-LLM) — без тестів.
