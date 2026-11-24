# 009 — План реалізації: Entity Resolution v2 (merge/unmerge дублікатів + UI-черга)

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Операційний стан зв'язків сутностей (`entity_links`) зберігається в SQLite (міграція №7) із прив'язкою до `tasks(id)` (`ON DELETE CASCADE`) та фіксацією точних номерів ревізій `canonical_before_rev` і `merged_before_rev` у DuckDB до злиття. Самі записи й версії залишаються суворо append-only в DuckDB `record_versions`: злиття додає нову версію канонічного ключа (детерміновано об'єднану функцією `merge_records`) та tombstone (`deleted = TRUE`) для поглинутого ключа, а роз'єднання читає збережені ревізії з `record_versions` і додає дві нові активні ревізії (`deleted = FALSE`), не видаляючи жодної історії. Якщо після первинного злиття той самий канонічний запис брав участь у наступних активних злиттях, при позачерговому роз'єднанні базовий стан канонічного ключа детерміновано перезбирається з решти активних зв'язків.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | Спочатку `spec.md`, `plan.md`, `tasks.md` і тести |
| II. Детермінованість, LLM лише fallback | ✅ | Політики `confidence`/`freshness`/`completeness`/`prefer_*` та blocking повністю детерміновані без LLM |
| III. Тести офлайн | ✅ | Тимчасові SQLite/DuckDB + фікстури, жодної мережі |
| IV. Походження, append-only | ✅ | Кожне злиття ставить `Provenance(method="merge")`, злиття і роз'єднання пишуть лише нові ревізії |
| V. Комплаєнс і PII | ✅ | Поля та експорти зберігають `FieldSpec.pii`; події не розкривають сирих PII-значень |
| VI. Local-first, захищений API | ✅ | Усі нові маршрути під Bearer/host-guard у `api/data.py` |
| VII. Події для змін даних | ✅ | Нові події `entity_merged`, `entity_unmerged`, `duplicate_dismissed` додаються в `00-domain-model.md` §4 |
| VIII. Типізовані межі, міграції | ✅ | Pydantic-моделі + `MIGRATIONS` #7 у SQLite + оновлення `frontend/src/{types,api}.ts` |
| IX. Простота, без зайвих залежностей | ✅ | Використовуємо наявні `rapidfuzz`, SQLite і DuckDB без нових пакетів |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Моделі | `backend/app/models.py` | `MergePolicy`, `EntityLinkInfo`, розширені опційні поля прев'ю/якості в `DuplicateCandidate` | `REQ-ENR-010`, `REQ-ENR-011` |
| Дедуплікація | `backend/app/enrichment/dedupe.py` | blocking по домену/телефону/назві, `merge_records`, `find_incremental_duplicates`, фільтрація виключених пар | `REQ-ENR-010` |
| Сховище | `backend/app/db/sqlite_store.py`, `backend/app/db/duckdb_store.py` | SQLite міграція №7 `entity_links` + CRUD/редіректи; DuckDB `get_record_revision` | `REQ-ENR-011` |
| Пайплайн / Сервіс | `backend/app/runtime.py`, `backend/app/pipeline/runner.py` | `MergeConflictError`, `Studio.{list_duplicates,merge_duplicates,dismiss_duplicate,unmerge_link}`, редірект `merged_key → canonical_key` в `runner._persist_records` | `REQ-ENR-010`, `REQ-ENR-011` |
| API | `backend/app/api/schemas.py`, `backend/app/api/data.py` | `MergeDuplicatesRequest`, `DismissDuplicateRequest`; розширення `GET /duplicates`, нові POST `merge`, `dismiss`, `links/{id}/unmerge` | `REQ-API-010` |
| CLI | `backend/app/cli.py` | `daas merge`, `daas unmerge`, оновлений вивід `daas duplicates` | `REQ-API-010` |
| UI | `frontend/src/{types.ts,api.ts,styles.css}`, `frontend/src/components/{MiscPanels.tsx,TaskWorkspace.tsx}` | Черга перевірки дублікатів з політиками, порівнянням, merge/dismiss, списком зв'язків і unmerge | `REQ-UI-014` |

## 4. Дані та міграції
- **SQLite `MIGRATIONS` (позиція 7):**
  - `entity_links(id, task_id, canonical_key, merged_key, status, score, rule, policy, reviewer, canonical_before_rev, merged_before_rev, created_at, updated_at, unmerged_at)`
  - Індекси `idx_entity_links_task(task_id, status)` та `idx_entity_links_pair(task_id, canonical_key, merged_key)`.
  - Сумісність: додаємо тест відкриття SQLite-файлу версії схеми ≤ 6 і перевірки автоматичного накатування міграції 7.
- **DuckDB `AnalyticsStore`:** зміна схеми таблиць не потрібна; додається лише метод читання конкретної ревізії `get_record_revision(dataset_id, record_key, revision)` та `get_latest_revision_number(dataset_id, record_key)` для детермінованого відновлення без видалення історії.

## 5. Контракти
- **HTTP API (`11-api-and-security.md`):**
  - `GET /api/tasks/{id}/duplicates?threshold=88` → `{items, links, scanned, note}`
  - `POST /api/tasks/{id}/duplicates/merge` (`canonical_key`, `merged_key`, `policy`, `score`, `rule`, `field_overrides`) → `200 {link, record, row_count}` (`400`, `404`, `409`)
  - `POST /api/tasks/{id}/duplicates/dismiss` (`left_key`, `right_key`, `score`, `rule`) → `200 {link}` (`400`, `404`, `409`)
  - `POST /api/tasks/{id}/duplicates/links/{link_id}/unmerge` → `200 {link, canonical_record, restored_record, row_count}` (`404`, `409`)
- **Каталог подій (`00-domain-model.md` §4):**
  - `entity_merged` (`info`, `runtime.Studio`) — злиття двох записів (`data.link_id`, `canonical_key`, `merged_key`, `policy`)
  - `entity_unmerged` (`info`, `runtime.Studio`) — роз'єднання активного зв'язку (`data.link_id`, `canonical_key`, `merged_key`)
  - `duplicate_dismissed` (`info`, `runtime.Studio`) — відхилення пари як не-дубліката (`data.link_id`, `left_key`, `right_key`)

## 6. Ризики і відкат
- **Позачергове unmerge при ланцюжку злиттів (`A+B`, потім `A+C`, далі unmerge `A+B`):** розв'язується перезбиранням `canonical_key` від `link.canonical_before_rev` із послідовним застосуванням решти активних `later_links` на тому самому `canonical_key`.
- **Повторний збір воскрешає злитий ключ:** розв'язується завантаженням `active_merged_key_map(task_id)` у `RunContext` і редіректом `merged_key → canonical_key` у `Runner._persist_records`.
- **Зворотна сумісність `GET /duplicates`:** `items`, `scanned`, `note` збережені без зміни існуючих обов'язкових ключів.

## 7. Тестова стратегія
- `backend/tests/test_entity_merge.py` (повністю офлайн):
  1. Міграція SQLite v6 → v7 на існуючому файлі БД;
  2. Юніт-тести `merge_records` для всіх 5 політик (`confidence`, `freshness`, `completeness`, `prefer_left`, `prefer_right`), масивів, `field_overrides`, `Provenance(method="merge")` та невідомої політики;
  3. Інкрементальне виявлення дублікатів `find_incremental_duplicates` та blocking по телефону/домену;
  4. E2E `Studio`/`API` сценарій: виявлення → merge → перевірка payload/provenance/`row_count`/tombstone/подій → dismiss іншої пари → виключення з черги → unmerge → перевірка відновлення обох записів, `row_count`, збереження всіх історичних ревізій (append-only) та out-of-order unmerge;
  5. Негативні сценарії API: 400 (самозлиття, невалідна політика), 404 (невідомий task/record/link), 409 (повторний merge вже поглинутого ключа, повторний unmerge);
  6. Інкрементальний повторний збір (`FixtureFetcher`): оновлення під ключем `merged_key` автоматично скеровується на `canonical_key`;
  7. CLI `daas duplicates`, `daas merge`, `daas unmerge` (успіх і exit code != 0 при помилці).
