# 07 — Збагачення (waterfall), AI-колонки, дедуплікація та злиття

| | |
|:---|:---|
| **Код** | `backend/app/enrichment/base.py`, `backend/app/enrichment/registry.py`, `backend/app/enrichment/site_audit.py`, `backend/app/enrichment/tech_detect.py`, `backend/app/enrichment/domain_resolver.py`, `backend/app/enrichment/ai_column.py`, `backend/app/enrichment/dedupe.py`, `backend/app/pipeline/runner.py` (`enrich`, `_enrich_row`, `_persist_records`, `_run_auto_enrichment`, `_matches_auto_enrich_trigger`), `backend/app/db/sqlite_store.py` (`entity_links`), `backend/app/db/duckdb_store.py` (`get_record_revision`), `backend/app/runtime.py` (`list_duplicates`, `merge_duplicates`, `dismiss_duplicate`, `unmerge_link`) |
| **API** | `POST /api/tasks/{id}/enrich {column_keys}`, `GET /api/tasks/{id}/duplicates?threshold=88`, `POST /api/tasks/{id}/duplicates/merge`, `POST /api/tasks/{id}/duplicates/dismiss`, `POST /api/tasks/{id}/duplicates/links/{link_id}/unmerge`, `GET /api/config` (`enrichers`) |
| **CLI** | `daas enrich <id> [--column k]`, `daas duplicates <id> [--threshold 88]`, `daas merge <id> <canon> <merged> [--policy ...]`, `daas unmerge <id> <link_id>` |
| **Тести** | `tests/test_api_flow.py`, `tests/test_compliance.py`, `tests/test_utils.py`, `tests/test_entity_merge.py`, `tests/test_prozorro_winners.py` |
| **Джерела** | `docs/01` модуль 3 («локальний Clay»), `docs/02` §4.2, `docs/03` §3.2 (`P1-02`), `014-prozorro-winners-b2b-triggers` |
| **Статус** | as-built (`014-prozorro-winners-b2b-triggers`) |

## 1. Призначення
Додати до зібраних записів те, за що платять: контакти, соцмережі, технології, сигнали наміру,
офіційний сайт, AI-оцінки — **не перезаписуючи** зібране. Дедуплікація формує чергу кандидатів
(без мовчазного автозлиття під час краулу) і дає змогу виконувати детерміноване злиття за обраною
політикою, відхилення хибних пар, канонічний редірект при повторному зборі та повністю оборотне
роз'єднання зі збереженням append-only історії версій.

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
`find_duplicates(rows, threshold, excluded_pairs)` **ПОВИНЕН** використовувати blocking (домен `d:`,
нормалізований телефон `p:` або перші 4 символи нормалізованої назви `n:`), рахувати
`token_set_ratio` нормалізованих назв, виключати пари з `excluded_pairs` (`merged`/`dismissed`),
повертати пари з `score ≥ threshold` (0–1 у відповіді), не більше 200, відсортовані за спаданням,
із оцінкою ідентичності та прев'ю полів. Автоматичного злиття без підтвердження **НЕ ПОВИННО** бути.
- **Перевірка:** `tests/test_utils.py::test_duplicate_candidates`,
  `tests/test_api_flow.py::test_full_pipeline` (`scanned == 3`)

### REQ-ENR-009 — Якість ідентичності
`identity_quality(payload)` **ПОВИНЕН** оцінювати, наскільки запис придатний до дедуплікації:
домен 1.0 > телефон > назва; порожній — 0.2.
- **Перевірка:** `tests/test_utils.py::test_duplicate_candidates`

### REQ-ENR-010 — Детермінована політика злиття, інкрементальне порівняння та канонічний редірект
Система **ПОВИННА**:
- надавати детерміноване злиття двох записів (`merge_records`) за політикою (`confidence` | `freshness` |
  `completeness` | `prefer_left` | `prefer_right`) та опційним `field_overrides`: порожні значення
  одного запису заповнюються з іншого, масиви об'єднуються без дублікатів, конфлікти скалярних полів
  вирішуються політикою, а змінені/конфліктні поля отримують `Provenance(method="merge")` із
  посиланням на вихідне джерело (невідома політика → `ValueError`);
- підтримувати інкрементальне виявлення дублікатів `find_incremental_duplicates` (порівняння нових
  записів лише з наявними канонічними кандидатами через blocking по домену, телефону та префіксу назви);
- під час інкрементального збору (`Runner._persist_records`) скеровувати оновлення раніше злитого
  ключа на його активний `canonical_key`, не відроджуючи поглинутий дублікат окремим рядком.
- **Перевірка:** `tests/test_entity_merge.py::test_deterministic_merge_policies_and_incremental_dedupe`,
  `tests/test_entity_merge.py::test_incremental_collect_redirects_merged_key_to_canonical`

### REQ-ENR-011 — Персистентні зв'язки сутностей, оборотне злиття та роз'єднання без втрати історії
Система **ПОВИННА**:
- зберігати рішення щодо пар у SQLite `entity_links` (`merged` | `dismissed` | `unmerged`, `score`,
  `rule`, `policy`, `reviewer`, `canonical_before_rev`, `merged_before_rev`) з forward-only
  міграцією #7 для існуючих баз;
- під час `merge_duplicates` додавати нову append-only ревізію канонічного запису, маркувати
  поглинутий запис tombstone-ревізією (`deleted=TRUE`), синхронізувати `row_count` і писати подію
  `entity_merged` (при самозлитті → `ValueError`, відсутності ключа → `KeyError`, конфлікті вже
  злитого ключа → `MergeConflictError`);
- дозволяти `dismiss_duplicate` (`dismissed`) з виключенням пари з черги та подією `duplicate_dismissed`;
- під час `unmerge_link` відновлювати обидва записи новими append-only ревізіями (`deleted=FALSE`,
  включно з детермінованим перезбиранням наступних активних злиттів на тому самому канонічному ключі
  при позачерговому роз'єднанні) **без видалення жодної історичної ревізії**, оновлювати `row_count`,
  переводити зв'язок у `unmerged` та писати подію `entity_unmerged`.
- **Перевірка:** `tests/test_entity_merge.py::test_old_sqlite_migrates_to_entity_links`,
  `tests/test_entity_merge.py::test_merge_dismiss_and_reversible_unmerge_api`,
  `tests/test_entity_merge.py::test_out_of_order_unmerge_replays_later_merges`

### REQ-ENR-012 — Послідовний waterfall для переможця та тригер авто-збагачення при зборі
Система **ПОВИННА** забезпечувати:
- цільовий пошук сайту за `spec_config.name_field` (наприклад, `winner_name`, з регіоном/містом) без
  підміни замовником і безпечний пропуск, якщо цільове поле порожнє;
- гарантований порядок `domain_resolver` → `site_audit` у `_enrich_row`, щоб знайдений у цьому ж
  проході `website` одразу проходив аудит (`site_audit` + `tech_detect`) без повторного запуску;
- автоматичний запуск збагачення в кінці `collect` (до снапшота, diff і доставки) для зібраних
  записів, що задовольняють `TaskSpec.auto_enrich` (`require_fields`, `status_field`/`allowed_statuses`,
  `min_amount`, відсутність уже заповнених збагачених полів/provenance) у межах
  `max_records_per_run`, через `PolicyGuardFetcher`, з append-only ревізією в DuckDB і подією
  `auto_enrichment_triggered`; збій чи блокування на окремому домені **НЕ ПОВИННІ** зривати збір.
- **Перевірка:** `tests/test_prozorro_winners.py::test_collect_winner_trigger_resolves_domain_and_audits_site`,
  `tests/test_prozorro_winners.py::test_winner_trigger_respects_threshold_idempotency_and_compliance`

## 4. Спостережувана поведінка / відкриті питання
- Через `overwrite=False` повторне збагачення **не оновлює** вже заповнені значення (включно з
  AI-колонками і змінами на сайті компанії). Це захищає зібране, але робить збагачення «одноразовим».
  Потрібне рішення: режим `refresh` / TTL для полів збагачення?
- `SourcePolicy.allowed_fields` зберігається, але не застосовується.

## 5. Прогалини
REQ-ENR-001, REQ-ENR-006 — без тестів. (REQ-ENR-005 частково покрито через
`tests/test_prozorro_winners.py::test_collect_winner_trigger_resolves_domain_and_audits_site`).
