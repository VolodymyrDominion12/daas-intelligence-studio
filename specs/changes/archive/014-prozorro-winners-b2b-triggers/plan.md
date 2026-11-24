# 014 — План реалізації: Профіль «Prozorro Переможці» та B2B-тригери авто-збагачення

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
1. Додати в `app/extraction/api_json.py` фільтрацію елементів списку за точним полем (`awards[status=active]`) у `_walk_path`, щоб вибирати дійсного переможця навіть коли перші записи у `awards` скасовані/неуспішні.
2. Додати в `app/models.py` типізовану модель `AutoEnrichTrigger` та опційне поле `TaskSpec.auto_enrich: AutoEnrichTrigger | None = None` (зворотно сумісне зі старими задачами без SQLite-міграції).
3. Розширити `app/presets.py` профілем **Prozorro Winners (B2B Intent)** із полями переможця, бізнес-контактами (`pii="business_contact"`), правильним порядком збагачувачів (`domain_resolver` → `site_audit`), а також оновити `DomainResolverEnricher` (цільове поле `name_field`, перевага `winner_name`/`winner_city`/`region`) і `PipelineRunner._enrich_row` (гарантований порядок `domain_resolver` перед `site_audit`).
4. У `PipelineRunner._finish_dataset` додати `_run_auto_enrichment(ctx)` перед `_write_snapshot_and_diff`, щоб авто-збагачені поля входили у снапшот, diff і доставку (`DeltaDispatcher`), та доповнити Telegram-форматування контекстом переможця й `award_amount`. Оновити `NewTask.tsx`, `PlanPanel.tsx`, `types.ts`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | Спочатку `spec.md` → `plan.md` → `tasks.md` → тести → реалізація → sync |
| II. Детермінованість, LLM лише fallback | ✅ | Профіль, JSON-мапінг `[status=active]`, `domain_resolver`, `site_audit`, `tech_detect` і тригер — повністю детерміновані |
| III. Тести офлайн | ✅ | `FixtureFetcher`, фейковий `SearchProvider`, без реальної мережі |
| IV. Походження, append-only | ✅ | Авто-збагачені поля дописуються новою ревізією в `record_versions` з `Provenance(method="enrichment", source_url=website)` |
| V. Комплаєнс і PII | ✅ | Усі запити `domain_resolver`/`site_audit` ідуть через `PolicyGuardFetcher`; `email`/`phone`/`socials` переможця мають `pii="business_contact"`, а `pii="personal"` лишається під маскуванням |
| VI. Local-first, захищений API | ✅ | Усе працює в існуючому процесі та сховищі без нових сервісів |
| VII. Події для змін даних | ✅ | Додається подія `auto_enrichment_triggered` у каталог `00-domain-model.md` §4 |
| VIII. Типізовані межі, міграції | ✅ | `AutoEnrichTrigger` у Pydantic `TaskSpec`; опційне поле не ламає наявні `spec_json` у SQLite |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових runtime-залежностей |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Моделі | `backend/app/models.py` | Модель `AutoEnrichTrigger`, поле `TaskSpec.auto_enrich` | `REQ-PLAN-015`, `REQ-ENR-012` |
| Екстракція | `backend/app/extraction/api_json.py` | Підтримка `list_key[field=value]` у `_walk_path` | `REQ-EXT-011` |
| Планування | `backend/app/presets.py`, `backend/app/runtime.py` | Профіль Prozorro Переможці, `_parse_amount_threshold`, прив'язка `domain_resolver` у `_attach_enricher_to_spec` | `REQ-PLAN-015` |
| Збагачення | `backend/app/enrichment/domain_resolver.py` | Підтримка `config.name_field` / `winner_name` і `winner_city`/`region` | `REQ-ENR-012` |
| Пайплайн | `backend/app/pipeline/runner.py` | Сортування `domain_resolver` перед `site_audit` у `_enrich_row`, `_run_auto_enrichment` перед снапшотом/доставкою, подія `auto_enrichment_triggered` | `REQ-ENR-012` |
| Доставка | `backend/app/pipeline/delivery.py` | Підтримка `award_amount` у `_extract_amount` і показ переможця/сайту/контактів/техстеку у `_format_telegram_delta` | `REQ-DEL-007` |
| UI | `frontend/src/types.ts`, `frontend/src/components/NewTask.tsx`, `frontend/src/components/PlanPanel.tsx` | Тип `AutoEnrichTrigger`, приклад «Prozorro Переможці (B2B)», редактор тригера в `PlanPanel` | `REQ-UI-018` |

## 4. Дані та міграції
- Нових таблиць або колонок SQLite/DuckDB не потрібно: `AutoEnrichTrigger` серіалізується всередині `tasks.spec_json` як опційне поле з `default=None`. Старі бази й задачі відкриваються прозоро.

## 5. Контракти
- **Новий тип події в `00-domain-model.md` §4:**
  - `auto_enrichment_triggered` (`info`, `pipeline/runner.py`): спрацював B2B-тригер авто-збагачення під час збору (`data.version`, `data.matched`, `data.enriched`, `data.min_amount`, `data.record_keys`).
- **API / UI контракт:** `TaskSpec.auto_enrich` (`AutoEnrichTrigger | null`) доступне в `GET /api/tasks/{id}`, `POST /api/tasks`, `PATCH /api/tasks/{id}` та синхронізоване в `frontend/src/types.ts`.

## 6. Ризики і відкат
- **Повільний зовнішній аудит при великому зборі:** обмежується `max_records_per_run=20` і `max_enrich_concurrency`; помилка/таймаут на окремому сайті не зриває `collect`.
- **Помилковий пошук сайту держзамовника замість переможця:** виключається явною вказівкою `config={"name_field": "winner_name"}` у пресеті та перевіркою `require_fields=["winner_name"]` у тригері.
- **Відкат:** локальна ізольована зміна, що не зачіпає схему БД; відкочується простим `git revert`.

## 7. Тестова стратегія
- Фікстури Prozorro з `awards` (кілька авардів: `unsuccessful` + `active`, та тендер без переможця / з малою сумою) і локальні HTML-фікстури сайту компанії-переможця (`mailto:`, телефон, WordPress/Cloudflare сигнатури).
- Фейковий `SearchProvider` у тестах повертає URL тестового домену переможця (без реальної мережі); `FixtureFetcher` віддає HTML сайту компанії; окремий тест перевіряє, що `tos_risk="forbidden"` на домені переможця блокується через `PolicyGuardFetcher`, пише `compliance_blocked` і не зриває збір.
