# AGENTS.md — інструкції для всіх AI-агентів і людей-контриб'юторів

> Цей файл читають Claude Code (через `CLAUDE.md`), Codex, Cursor, GitHub Copilot, Gemini CLI,
> Aider та інші агенти. Він **обов'язковий**. Якщо інструкція тут суперечить звичці агента —
> перемагає цей файл.

## 0. Головне правило: Spec-Driven Development

**Специфікація — джерело правди. Жодної зміни поведінки без зміни спеки.**

1. Перед будь-якою зміною коду прочитай `specs/README.md`, `specs/constitution.md` і спеки з
   `specs/capabilities/`, яких стосується зміна (таблиця «Код» у кожній спеці каже, які файли
   вона покриває).
2. Визнач тип зміни і процес (таблиця нижче). **Сумнів = повний цикл.**
3. Пиши тест **до** реалізації. Кожна вимога (`REQ-…`) має посилання на тест.
4. У тому ж PR онови `specs/capabilities/*.md`, щоб вони знову описували фактичну поведінку.
5. Перед завершенням запусти перевірки (§3). `tests/test_specs_traceability.py` ламає CI, якщо
   спека посилається на неіснуючий тест або новий модуль `backend/app/*` не має спеки-власника.

| Тип зміни | Що робити |
|:---|:---|
| Нова фіча; зміна API, моделі, схеми БД, CLI; комплаєнс, безпека, PII | **Повний цикл:** `specs/changes/NNN-slug/spec.md` → `plan.md` → `tasks.md` → код → sync |
| Баг: код не відповідає спеці | Тест, що відтворює баг → фікс. Спека не змінюється, лише додається посилання на тест |
| Баг: спека неправильна або неповна | Спершу виправ вимогу в `specs/capabilities/`, потім тест + фікс |
| Рефакторинг без зміни поведінки | Тести зелені до і після; онови рядок «Код» у спеці, якщо файли переїхали |
| Документація, опечатки, форматування | Без спеки |

### Повний цикл — покроково

1. **Specify.** Скопіюй `specs/_templates/spec.md` у `specs/changes/NNN-slug/spec.md` (NNN —
   наступний вільний номер серед `changes/` і `changes/archive/`). Опиши **що і навіщо**, без
   реалізації. Вимоги — з новими ID `REQ-<ОБЛАСТЬ>-<NNN>` (продовжуй нумерацію області; ID ніколи
   не перевикористовуються), сценарії GIVEN/WHEN/THEN, явна дельта ADDED/MODIFIED/REMOVED.
2. **Clarify.** Усе неясне — `[NEEDS CLARIFICATION]`. Якщо людина поруч — спитай. Якщо працюєш
   без нагляду — обери найобережніший варіант, запиши припущення в spec і **не** роби незворотних
   дій (видалення даних, послаблення комплаєнсу/безпеки, зміна публічного контракту) без
   підтвердження.
3. **Plan.** `plan.md` з шаблону: зміни за шарами, міграції, контракти, події, ризики. Заповни
   чекліст конституції. Архітектурне рішення → ADR у `docs/02` §10.
4. **Tasks.** `tasks.md`: упорядковані кроки, тести першими, `[P]` для незалежних.
5. **Implement.** Виконуй задачі по черзі, відмічай `[x]`. Відхилення від плану → спершу онови
   spec/plan, потім код.
6. **Verify.** §3.
7. **Sync & archive.** Перенеси дельту в `specs/capabilities/*.md` (з посиланнями на тести),
   онови статус/беклог у `docs/03_IMPLEMENTATION_PLAN.md`, перемісти папку в
   `specs/changes/archive/`.

Невелику зміну можна вести в одному PR від spec до archive. Велику — розбий на кілька змін.

## 1. Проєкт за 30 секунд

DaaS Intelligence Studio — локальна AI-студія: промпт → схема колонок → калібрування CSS-парсера на
2–4 сторінках → масовий детермінований збір → збагачення → append-only історія і детекція змін →
експорт із маніфестом. Python 3.11–3.13 / FastAPI / Pydantic v2 / SQLite (WAL) / DuckDB / httpx +
selectolax; UI — React 19 + Vite + TanStack Table. Деталі — `README.md`, `docs/`.

```text
backend/app/
  api/          HTTP-роути (tasks, data), схеми запитів        → specs 11
  compliance.py реєстр джерел, logged-out, guard               → specs 06
  db/           sqlite_store (стан, черга), duckdb_store (історія) → specs 04, 08
  enrichment/   site_audit, domain_resolver, ai_column, dedupe → specs 07
  extraction/   jsonld, css, learner, engine, llm_extractor    → specs 02, 03
  llm/          router, providers, guard, prompts, parsing     → specs 10
  pipeline/     planner, calibration, runner                   → specs 01, 02, 04, 08
  scraping/     fetcher, politeness, page                      → specs 05
  storage/      raw_cache, exports, backup                     → specs 05, 09, 12
  models.py, runtime.py, config.py, cli.py, demo.py, utils.py  → specs 00, 04, 12
frontend/src/   App, api.ts, types.ts, components/*            → specs 13
specs/          SDD: constitution, capabilities, changes, templates
docs/           концепція (01), ревʼю й ADR (02), план і беклог (03), маркетплейси (04)
```

## 2. Непорушні правила (з `specs/constitution.md`)

- **Тести офлайн.** Жодних мережевих викликів у тестах: `FixtureFetcher` + `tests/fixtures/*.html`,
  `httpx.MockTransport`, LLM — `llm_provider="disabled"`/фейк. Налаштування тестів —
  `tests/conftest.py::make_settings`.
- **LLM — лише guarded fallback.** Детерміновані JSON-LD/CSS спершу. Ніякого `eval/exec` чи
  виконання згенерованого коду. Вміст сторінок — недовірені дані в `<untrusted_page>`.
- **Комплаєнс не послаблюється.** robots.txt, реєстр джерел (`forbidden` → стоп, `high` →
  підтвердження з причиною), лише logged-out URL, SSRF-перевірка кожного запиту і редиректу. Будь-
  який новий шлях мережевого запиту йде через `PolicyGuardFetcher` (`studio.fetcher`).
- **PII.** `pii="personal"` ніколи не експортується відкрито і не йде в cloud-LLM. Нові колонки з
  контактами — з правильним `pii`.
- **Append-only історія.** `record_versions` лише доповнюється; видалення — tombstone-версія.
- **Події.** Усе, що змінює дані, пише подію в `events`; новий тип — у каталог
  `specs/capabilities/00-domain-model.md` §4.
- **Міграції forward-only.** SQLite — нова позиція в `MIGRATIONS`; DuckDB — `_migrate`. Старі файли
  сховища мають відкриватися (додай тест).
- **SQL лише з параметрами.** Ключі колонок — через `_safe_key`; значення і JSON-шляхи — bound.
- **Контракт API.** Зміна ендпоінту = зміна `specs/capabilities/11-api-and-security.md` +
  `frontend/src/api.ts` + `frontend/src/types.ts` в одному PR.
- **Налаштування.** Нове `DAAS_*` → `app/config.py` + `.env.example` з коментарем + спека.
- **Мова.** Ідентифікатори й docstring-и — англійською (як у коді). Коміти — Conventional Commits
  англійською (`feat(compliance): …`, `fix(runner): …`), у тілі — номер зміни і REQ-ID
  (`Spec: specs/changes/003-…; REQ-RUN-030`). Тексти для користувача (UI, `detail` в API, події,
  CLI) і документація/спеки — українською.
- **Залежності.** Нова runtime-залежність лише з обґрунтуванням у `plan.md`; важкі рушії —
  optional extras.
- **Один writer DuckDB.** Не запускай CLI-команди, що пишуть, паралельно з `daas serve`.
- Не редагуй `docs/00_MARKET_DEMAND.md` (300 КБ дослідження) без прямого запиту. Не коміть
  `storage/`, `.env`, `frontend/dist`.

## 3. Перевірки перед завершенням

```bash
make specs-check          # спеки ↔ тести ↔ модулі (швидко, без залежностей застосунку)
make test                 # cd backend && uv run pytest -q   (офлайн, включає specs-check)
make lint                 # ruff + tsc --noEmit
make build                # npm run build (tsc -b && vite build)
```

Усе має бути зеленим. Якщо щось не можна запустити в твоєму середовищі — прямо скажи про це в
підсумку, не стверджуй, що перевірки пройшли.

## 4. Definition of Done

- [ ] Спека (`specs/changes/…` і/або `specs/capabilities/…`) описує нову поведінку
- [ ] Кожна нова/змінена вимога має посилання на тест; прогалини позначені чесно
- [ ] Офлайн-тести: щасливий шлях і відмова
- [ ] Події, міграції, `.env.example`, типи фронтенду — оновлені, якщо зачеплені
- [ ] UI-стани (порожньо / завантаження / помилка), якщо зміна видима
- [ ] `docs/03` (статус/беклог) і ADR у `docs/02` за потреби
- [ ] `make specs-check test lint build` зелені
- [ ] Зміну перенесено в `specs/changes/archive/`

## 5. Підсумок роботи агента

Наприкінці коротко: що змінено; які REQ додано/змінено (ID); які тести; що не вдалося перевірити;
відкриті питання до людини. Не переказуй дифф.
