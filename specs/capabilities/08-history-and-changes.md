# 08 — Історія записів, снапшоти, детекція змін, tombstones

| | |
|:---|:---|
| **Код** | `backend/app/db/duckdb_store.py` (`AnalyticsStore`), `backend/app/pipeline/runner.py` (`snapshot`, `_write_snapshot_and_diff`, `_emit_changes`, `_apply_tombstones`, `rollback_removals`), `backend/app/pipeline/quality.py` (`evaluate_dataset_quality`, `load_gold_set`, `run_gold_eval`), `backend/app/pipeline/delivery.py` (`DeltaDispatcher`, `match_filters`, `change_dedup_key`, `flush_subscription`, `flush_due_digests`), `backend/app/db/sqlite_store.py` (`dataset_versions`, `record_misses`, `subscriptions`, `delivery_log`, `delivery_digest_queue`) |
| **API** | `GET /api/tasks/{id}/records?q&sort&dir&limit&offset&f_<key>`, `GET /api/tasks/{id}/records/{key}`, `GET /api/tasks/{id}/dataset`, `POST /api/tasks/{id}/snapshot`, `GET /api/tasks/{id}/changes`, `GET /api/tasks/{id}/quarantine`, `POST /api/tasks/{id}/quarantine/{item_id}/approve`, `DELETE /api/tasks/{id}/quarantine/{item_id}`, `GET/POST /api/tasks/{id}/subscriptions`, `PATCH/DELETE /api/tasks/{id}/subscriptions/{sub_id}`, `POST /api/tasks/{id}/subscriptions/{sub_id}/test`, `POST /api/tasks/{id}/subscriptions/{sub_id}/flush`, `POST /api/tasks/{id}/eval` |
| **CLI** | `daas snapshot <id>`, `daas changes <id>`, `daas notify add\|list\|test\|flush\|delete`, `daas eval --task <id> --gold <path> [--min-f1 ...]` |
| **Тести** | `tests/test_storage.py`, `tests/test_tombstones.py`, `tests/test_sql_safety.py`, `tests/test_api_flow.py`, `tests/test_delivery.py`, `tests/test_quality.py`, `tests/test_quarantine.py` |
| **Джерела** | ADR-02, ADR-05, `docs/03` §3.1 (P1-01), `docs/03` §3.3 (P1-03), `docs/03` §3.7 (P1-08), `docs/05` §5 (D-01), `013-delivery-filters-and-digest` |
| **Статус** | as-built (`013-delivery-filters-and-digest`) |

## 1. Призначення
Історія, яку конкурент не спарсить заднім числом: append-only версії кожного запису, поточний стан,
Parquet-снапшоти на версію датасету і події змін між версіями. Видалення — лише консервативні
tombstones.

## 2. Модель зберігання (DuckDB)
- `record_versions(dataset_id, record_key, dataset_version, revision, run_id, source_url,
  content_hash, payload JSON, provenance JSON, observed_at, deleted)` — **лише INSERT** (виняток:
  відкат tombstones незавершеного запуску). Злиття й роз'єднання дублікатів (`P1-02`, див. 07)
  також додають лише нові ревізії (`deleted=TRUE` для поглинутого ключа та відновлення обох при
  unmerge), не видаляючи жодної історичної ревізії.
- `current_records` (view) — остання ревізія кожного ключа, без `deleted`.
- `quarantine(id, dataset_id, run_id, source_url, reason, payload JSON, created_at)` — підозрілі
  записи з причиною відбраковування (P1-08).
- Один процесний конекшн під lock (ADR-02).

## 3. Вимоги — історія і запити

### REQ-HIST-001 — Append-only і поточний стан
Кожне спостереження запису **ПОВИННО** додаватися новою ревізією; `current_records` показує останню
ревізію кожного ключа; старі ревізії доступні в `record_history`.
- **Перевірка:** `tests/test_storage.py::test_record_versions_current_view_and_diff`

### REQ-HIST-002 — Запити до записів безпечні щодо SQL
`query_records` **ПОВИНЕН**: шукати `q` по всьому payload (ILIKE); фільтрувати `f_<key>=значення`
(ILIKE по JSON-полю); сортувати за колонкою з урахуванням типу (числа — `DOUBLE`, дати —
`TIMESTAMP`, інше — `lower()`), `NULLS LAST`, з тай-брейком за `record_key` у тому самому напрямку;
пагінація `limit ≤ 500`, `offset`. Усі значення та JSON-шляхи — **зв'язані параметри**; ключі
колонок проходять `_safe_key` (лише `[A-Za-z0-9_]`), невідомий ключ сортування → порядок за
замовчуванням.
- **Перевірка:** `tests/test_sql_safety.py::test_quotes_in_filter_values_are_data`,
  `tests/test_sql_safety.py::test_unsafe_column_keys_are_rejected`,
  `tests/test_sql_safety.py::test_fill_rates_with_bound_paths`,
  `tests/test_api_flow.py::test_full_pipeline` (`f_city`)

### REQ-HIST-003 — Якість = заповненість
`field_fill_rates` **ПОВИНЕН** повертати % непорожніх значень кожної не-AI колонки в поточному стані.
Точність (precision/recall) у v0.1 не вимірюється (P1-01).
- **Перевірка:** `tests/test_sql_safety.py::test_fill_rates_with_bound_paths`

## 4. Вимоги — снапшоти і зміни

### REQ-HIST-010 — Снапшот = стан на версію
Снапшот версії `v` **ПОВИНЕН** містити рівно по одному рядку на ключ — останню ревізію з
`dataset_version ≤ v`, без tombstones — і записуватися в
`snapshots/dataset=<slug>/version=<NNNN>/part.parquet` + `manifest.json` (≤ 500 змін).
Шляхи передаються в DuckDB як параметри (лапки в шляху безпечні).
- **Перевірка:** `tests/test_storage.py::test_diff_detects_new_entities_and_price_drops`,
  `tests/test_tombstones.py::test_snapshot_contains_current_state_only`,
  `tests/test_sql_safety.py::test_files_with_quotes_in_path`

### REQ-HIST-011 — Diff між версіями
`diff_versions(from, to)` **ПОВИНЕН** повертати: `new_entity` (ключ з'явився), `removed_entity`
(зник), `field_changed` (для кожної колонки зі зміненим значенням; для цінових колонок —
`delta_pct`), і додатково `price_drop`, якщо `delta_pct ≤ −DAAS_PRICE_DROP_THRESHOLD_PCT` (5%).
Колонки збагачення, відсутні в обох версіях, ігноруються.
- **Перевірка:** `tests/test_storage.py::test_diff_detects_new_entities_and_price_drops`,
  `tests/test_api_flow.py::test_full_pipeline`

### REQ-HIST-012 — Події змін
Після збору/збагачення **ПОВИННА** писатися подія `changes_detected` (зведення за типами) і до 25
подій окремих змін (`price_drop` — `warning`). Окремий `POST /snapshot` перезаписує снапшот
поточної версії і рахує зміни, але **не** пише події змін.
- **Перевірка:** `tests/test_api_flow.py::test_full_pipeline`,
  `tests/test_tombstones.py::test_full_crawl_marks_missing_record_removed`

### REQ-HIST-013 — Снапшот без даних
`POST /snapshot` без жодної версії датасету **ПОВИНЕН** завершити запуск `failed` з подією
`run_failed` «Немає даних для снапшота».
- **Перевірка:** — (прогалина)

## 5. Вимоги — tombstones (P1-03)

> Принцип: хибне «видалено» гірше за запізніле.

### REQ-HIST-020 — Tombstone — це версія
Видалення **ПОВИННО** бути новою ревізією з `deleted=TRUE` і останнім payload; запис зникає з
поточного стану, снапшотів і diff (→ `removed_entity`), історія зберігається; повторна поява
запису — `new_entity`.
- **Перевірка:** `tests/test_tombstones.py::test_tombstone_hides_record_but_keeps_history`

### REQ-HIST-021 — Лише після повного проходу
Промахи рахуються **лише** якщо прохід повний: не скасовано, не `queue stalled`, немає `failed`/
`skipped` задач, і немає залишку в черзі після ліміту сторінок (ліміт без залишку — повний прохід).
Інакше — подія `tombstones_skipped`, `manifest.full_crawl=false`.
- **Перевірка:** `tests/test_tombstones.py::test_partial_crawl_never_removes`,
  `tests/test_tombstones.py::test_page_limit_without_remaining_work_is_a_full_crawl`

### REQ-HIST-022 — Лише записи з перевіреними джерелами
Кандидатом **ПОВИНЕН** бути лише запис, якого не бачили в цьому запуску, востаннє спостережений у
старшій версії, і **кожна** відома сторінка-джерело якого була завантажена свіжо (не з кешу) у
цьому запуску й дала записи.
- **Перевірка:** `tests/test_tombstones.py::test_unchecked_known_source_blocks_removal`,
  `tests/test_tombstones.py::test_cached_page_is_not_evidence`

### REQ-HIST-023 — Пільговий період
Промах **ПОВИНЕН** рахуватися раз на запуск; запис видаляється після
`DAAS_TOMBSTONE_AFTER_MISSES` (1) послідовних придатних промахів; поява запису скидає лічильник.
- **Перевірка:** `tests/test_tombstones.py::test_misses_count_once_per_run`,
  `tests/test_tombstones.py::test_grace_period_counts_only_eligible_misses`

### REQ-HIST-024 — Запобіжник масового видалення
Якщо кандидатів більше за `DAAS_TOMBSTONE_MAX_RATIO` (0.5) датасету, видалення **НЕ ПОВИННО**
відбутися; подія `tombstones_suspicious` (warning).
- **Перевірка:** `tests/test_tombstones.py::test_mass_removal_is_refused`

### REQ-HIST-025 — Оптимістична перевірка ревізії
Tombstone **ПОВИНЕН** ставитися лише якщо поточна ревізія ключа досі та, що бачили під час відбору
(запис, повторно спостережений паралельним запуском, не ховається).
- **Перевірка:** `tests/test_tombstones.py::test_tombstone_skips_records_reobserved_meanwhile`

### REQ-HIST-026 — Відкат незавершеного запуску
Якщо запуск не зафіксував версію датасету (виняток, перезапуск процесу), його tombstones і
лічильники промахів **ПОВИННІ** відкочуватися.
- **Перевірка:** `tests/test_tombstones.py::test_rollback_tombstones_of_unfinished_run`,
  `tests/test_tombstones.py::test_failed_finish_rolls_back_removals`

### REQ-HIST-027 — Успішне видалення
Видалені записи **ПОВИННІ** давати `stats.records_removed`, подію `records_removed` (≤ 50 ключів) і
`manifest.removed`.
- **Перевірка:** `tests/test_tombstones.py::test_full_crawl_marks_missing_record_removed`

## 6. Вимоги — міграції

### REQ-HIST-030 — Старі файли DuckDB відкриваються
`_migrate` **ПОВИНЕН** forward-only доводити старі файли до поточної схеми (зокрема додавати
`deleted`, обходячи заборону DuckDB на `ALTER` таблиці з індексом).
- **Перевірка:** `tests/test_tombstones.py::test_old_database_is_migrated`

## 7. Вимоги — доставка дельти та алерти (`D-01`)

### REQ-DEL-001 — Керування підписками на доставку дельти (API і CLI)
Система **ПОВИННА** дозволяти створювати, переглядати, оновлювати, тестувати й видаляти підписки на
доставку для конкретної задачі (канали `webhook` або `telegram`, фільтри `event_types`, `keywords`,
`min_amount`, `max_amount`, `notify_on_error`, прапор `enabled`, опційний секрет підпису webhook).
Небезпечні webhook URL (локальні/приватні адреси без дозволу або URL із обліковими даними)
**ПОВИННІ** відхилятися (HTTP 400 / 403). Секрет підпису **НЕ ПОВИНЕН** повертатися відкритим текстом
у відповідях API чи подіях. Створення, оновлення і видалення підписки пишуть події
`subscription_created`, `subscription_updated`, `subscription_deleted`.
- **Перевірка:** `tests/test_delivery.py::test_subscription_crud_ssrf_and_secret_masking`,
  `tests/test_delivery.py::test_cli_notify_commands`

### REQ-DEL-002 — Детермінована фільтрація, маскування PII та форматування повідомлень
Перед відправленням дельти система **ПОВИННА**:
- відбирати лише зміни увімкнених підписок, що відповідають усім заданим фільтрам (`event_types`,
  регістронезалежні `keywords` у записі, `min_amount` / `max_amount`);
- безумовно приховувати/маскувати значення колонок із `pii="personal"` перед виходом у будь-який
  зовнішній канал (`[redacted]`);
- для `webhook` формувати JSON-пакет дельти (і заголовок `X-Daas-Signature: sha256=...` за наявності
  секрету), для `telegram` — безпечно екрановане HTML-повідомлення через Telegram Bot API.
- **Перевірка:** `tests/test_delivery.py::test_delta_filtering_pii_redaction_and_hmac`

### REQ-DEL-003 — Ідемпотентна дедуплікація, повторні спроби, SSRF на редиректі та алерти власнику
Система **ПОВИННА**:
- гарантувати, що кожна подія зміни версії або службовий алерт доставляються по конкретній підписці
  **щонайбільше один раз** (персистентний журнал дедуплікації `delivery_log` переживає перезапуск);
- виконувати повторні спроби (до налаштованого ліміту) при мережевих помилках / HTTP `429` / `5xx`,
  не повторювати `4xx` (крім `429`) і блокувати небезпечні URL перед кожним запитом без автоматичного
  слідування редиректам;
- при `notify_on_error=true` надсилати службовий алерт власнику, якщо запуск завершився з `failed` або
  дав 0 записів;
- фіксувати результат подіями `delivery_sent` або `delivery_failed` (`warning`, без токенів/секретів
  у тексті), не зриваючи фіксацію самого збору даних.
- **Перевірка:** `tests/test_delivery.py::test_delivery_retries_dedup_and_owner_error_alerts`

### REQ-DEL-004 — Розширені детерміновані фільтри підписок (стоп-слова, CPV, регіони, поля)
Система **ПОВИННА** підтримувати у фільтрах підписки (`API`, `CLI`, `UI` та зворотну сумісність зі
старими підписками):
- `exclude_keywords`: список регістронезалежних стоп-слів; якщо будь-яке стоп-слово знайдено в записі чи
  зміні, подія відхиляється незалежно від позитивних `keywords`;
- `cpv_prefixes`: список префіксів CPV-коду; якщо непорожній, подія проходить лише коли значення `cpv` /
  `cpv_code` у записі починається на один із префіксів;
- `regions`: список регіонів/міст (регістронезалежний підрядковий збіг по полях `region` / `city`); якщо
  непорожній, подія проходить лише при збігу хоча б із одним елементом;
- `field_filters`: словник `ключ_поля -> список дозволених значень` (регістронезалежний точний збіг або
  префіксний збіг за `*` у кінці шаблону); усі непорожні групи фільтрів поєднуються через логічне
  `AND`, варіанти всередині групи — через `OR`.
Перевірка фільтрів **НЕ ПОВИННА** бачити сирі значення колонок із `pii="personal"` (вони редагуються до
фільтрації).
- **Перевірка:** `tests/test_delivery.py::test_advanced_delivery_filters_cpv_region_exclude_and_fields`

### REQ-DEL-005 — Режим щоденного дайджесту (`digest_daily`), персистентна черга та планова відправка
Система **ПОВИННА** підтримувати режим доставки підписки `delivery_mode` (`instant` за замовчуванням або
`digest_daily`) із cron-виразом `digest_cron` (за замовчуванням `0 6 * * *` у UTC):
- невалідний `digest_cron` при створенні/оновленні підписки **ПОВИНЕН** відхилятися (HTTP 422);
- для активної `digest_daily`-підписки відфільтровані та ще не доставлені зміни **ПОВИННІ** ставитися в
  персистентну чергу дайджесту `delivery_digest_queue` без негайної відправки (із подією `digest_queued`),
  зберігаючись при перезапуску процесу;
- службові алерти власнику (`notify_on_error=true`) та тестові сповіщення залишаються миттєвими;
- планувальник за настання планового часу (`next_digest_at <= now`) або ручний виклик
  `POST /api/tasks/{id}/subscriptions/{sub_id}/flush` / CLI `daas notify flush` **ПОВИННІ** збирати
  накопичені записи в єдине зведення (`event="digest"` для Webhook, зведене повідомлення для Telegram),
  фіксувати `delivery_log` і `flushed_at` після успішної відправки, пропускати порожній дайджест без
  HTTP-запиту і зсувати `next_digest_at` на наступний слот;
- у разі збою доставки елементи черги дайджесту **НЕ ПОВИННІ** губитися (лишаються доступними для
  наступної відправки).
- **Перевірка:** `tests/test_delivery.py::test_digest_daily_queue_scheduler_flush_and_failure_safety`,
  `tests/test_delivery.py::test_cli_notify_advanced_filters_and_flush`

### REQ-DEL-006 — Читабельне Telegram-форматування та XLSX-вкладення при перевищенні ліміту
Під час відправки в канал `telegram` (як миттєвої, так і дайджесту) система **ПОВИННА**:
- формувати безпечно екрановане HTML-повідомлення з типом зміни, назвою, відформатованою сумою/валютою,
  регіоном/CPV (за наявності), зміною поля (`old → new`, `delta_pct`) і посиланням на джерело;
- обмежувати інлайн-перелік у текстовому повідомленні величиною `DAAS_DELIVERY_INLINE_LIMIT` (за
  замовчуванням `10`);
- якщо кількість змін у пачці перевищує `DAAS_DELIVERY_INLINE_LIMIT`, додатково надсилати через Telegram
  Bot API `sendDocument` згенерований у пам'яті XLSX-файл з усіма змінами пачки, у якому колонки
  `pii="personal"` безумовно замасковані (`[redacted]`), а рядкові значення захищені від formula injection
  (`=`, `+`, `-`, `@`); при збої `sendDocument` після успішного `sendMessage` текст уже доставлених змін
  фіксується в `delivery_log`, а про помилку вкладення пишеться подія `delivery_failed`.
- **Перевірка:** `tests/test_delivery.py::test_telegram_formatting_and_xlsx_attachment_over_limit`

## 8. Вимоги — якість даних та офлайн gold-set eval (`P1-01`)

### REQ-QUAL-001 — Автоматичні перевірки якості після прогону
Після завершення збору або збагачення система **ПОВИННА** детерміновано перевіряти:
- падіння кількості записів (або спостережених записів при повному проході) відносно попередньої
  версії більше ніж на `DAAS_QUALITY_MAX_DROP_RATIO` (`0.5`);
- заповненість обов'язкових не-AI колонок (`required=true`) нижче
  `DAAS_QUALITY_MIN_REQUIRED_FILL_PCT` (`80.0%`).
Звіт (`QualityChecksReport`) **ПОВИНЕН** записуватися в `manifest.quality_checks` версії датасету.
При порушенні (`passed=false`) система пише подію `quality_warning` (`warning`) і надсилає алерт
власнику (`kind="quality_warning"` підписникам з `notify_on_error=true`), не відкочуючи й не
зриваючи фіксацію зібраних записів.
- **Перевірка:** `tests/test_quality.py::test_post_run_quality_guards_pass_and_warn`,
  `tests/test_quality.py::test_quality_warning_sends_owner_alert`

### REQ-QUAL-002 — Офлайн gold-set eval та поріг регресії
Система **ПОВИННА** надавати детермінований офлайн eval-харнес (`run_gold_eval`) для HTML та JSON API
кейсів (без мережевих запитів):
- обчислювати `precision`, `recall`, `f1`, `coverage`, `llm_fallback_pct` і `usd_per_1k_records`;
- перевіряти поріг `f1 >= min_f1` (за замовчуванням `DAAS_EVAL_MIN_F1 = 0.8`);
- зберігати результат у `manifest.eval` останньої версії датасету та писати подію `eval_completed`
  (без витоку сирих PII-значень);
- надавати `POST /api/tasks/{id}/eval` (inline-кейси або безпечне ім'я пресету без path traversal)
  та CLI `daas eval --task <id> --gold <path> [--min-f1 ...]` (код виходу `1` при `passed=false`).
- **Перевірка:** `tests/test_quality.py::test_gold_eval_api_json_and_html_regression_gate`,
  `tests/test_quality.py::test_eval_endpoint_and_cli_exit_codes`

## 9. Вимоги — карантин підозрілих записів (`P1-08`)

### REQ-HIST-040 — Автоматичне спрямування підозрілих записів у карантин та міграція схеми
Під час збору (`HTML` або `api_json`) система **ПОВИННА** детерміновано перевіряти кандидати
записів і спрямовувати підозрілі в `quarantine` (з `id`, `source_url`, `payload`, `created_at` і
причиною), коли:
- кандидат мав ≥ 1 витягнутого поля, але після приведення типів лишається `< 2` валідних полів
  (за `>= 2` джерельних колонок) або відсутнє ідентифікуюче поле;
- не-порожнє значення мапленого поля в `api_json` не вдалося привести до оголошеного `dtype`;
- середня впевненість (`mean_confidence`) запису нижча за `0.40`.

Записи без помилки приведення типу і з `>= 2` валідними полями не карантиняться на рівні
рядка (контролюються `REQ-QUAL-001`). Старі DuckDB файли без `quarantine.id` мігрують
forward-only з backfill ідентифікаторів. Кожен елемент збільшує `stats.records_quarantined` і
пише подію `record_quarantined` (`warning`).
- **Перевірка:** `tests/test_quarantine.py::test_old_duckdb_quarantine_is_migrated`,
  `tests/test_quarantine.py::test_runner_quarantines_suspect_html_and_api_records`

### REQ-HIST-041 — HTTP API карантину: перегляд, підтвердження та відхилення
Система **ПОВИННА** надавати захищені Bearer-токеном ендпоінти:
- `GET /api/tasks/{id}/quarantine?limit&offset` — пагінований список карантину;
- `POST /api/tasks/{id}/quarantine/{item_id}/approve` — перенесення у append-only
  `record_versions` (`Provenance(method="manual", verified=True)`), оновлення `row_count`,
  видалення з карантину і подія `quarantine_approved`;
- `DELETE /api/tasks/{id}/quarantine/{item_id}` — відхилення з карантину і подія
  `quarantine_dismissed` (`404` для невідомих `task_id`/`item_id`).
- **Перевірка:** `tests/test_quarantine.py::test_quarantine_api_list_approve_dismiss_and_404`

## 10. Відкриті питання
- Невалідний ключ у `f_<key>` дає `ValueError` у сховищі → **HTTP 500** на API (немає мапінгу
  `ValueError → 400`). Має бути 400 «некоректна колонка» — потрібен тест на рівні API і фікс.
