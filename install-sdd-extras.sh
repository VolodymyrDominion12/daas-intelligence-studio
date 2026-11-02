#!/usr/bin/env bash
# One-time installer for SDD files that could not be written remotely
# (Makefile, .claude/commands/*, .github/*). Run from the repo root, then delete this script.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p ".claude/commands"
cat > ".claude/commands/sdd-specify.md" <<'__SDD_EOF__'
---
description: SDD крок 1 — створити специфікацію зміни в specs/changes/
argument-hint: <опис фічі або зміни>
---

Створи специфікацію зміни для: **$ARGUMENTS**

1. Прочитай `specs/README.md`, `specs/constitution.md`, `specs/_templates/spec.md`.
2. Знайди в `specs/capabilities/` спеки, яких стосується зміна (за таблицею «Код» і змістом).
   Прочитай їх повністю, а також відповідний розділ `docs/03_IMPLEMENTATION_PLAN.md` (беклог).
3. Визнач наступний вільний номер NNN серед `specs/changes/*/` і `specs/changes/archive/*/`.
   Slug — kebab-case латиницею, з ID беклогу, якщо є (`004-p1-05-scheduler`).
4. Створи `specs/changes/NNN-slug/spec.md` за шаблоном:
   - **що і навіщо**, без назв функцій/таблиць/бібліотек;
   - нові вимоги з ID, що продовжують нумерацію області (`grep -rho "REQ-RUN-[0-9]*" specs`);
   - сценарії GIVEN/WHEN/THEN для успіху і для відмови;
   - явна дельта ADDED / MODIFIED (було → стане) / REMOVED;
   - нефункціональні вимоги: офлайн-тести, PII, комплаєнс, вартість LLM, сумісність сховища;
   - поза скоупом; відкриті питання з `[NEEDS CLARIFICATION]`.
5. Додай рядок у таблицю «Активні зміни» в `specs/changes/README.md`.
6. Не пиши код. Наприкінці покажи список відкритих питань і запропонуй наступний крок
   `/sdd-plan NNN`, якщо питань немає.
__SDD_EOF__
echo "created .claude/commands/sdd-specify.md"

mkdir -p ".claude/commands"
cat > ".claude/commands/sdd-plan.md" <<'__SDD_EOF__'
---
description: SDD крок 3 — план реалізації з перевіркою конституції
argument-hint: <NNN або шлях до specs/changes/NNN-slug>
---

Склади план реалізації для зміни **$ARGUMENTS**.

1. Прочитай `specs/changes/<NNN>-*/spec.md`. Якщо там лишились `[NEEDS CLARIFICATION]` —
   зупинись і постав ці питання людині (або, якщо працюєш без нагляду, запиши обрані обережні
   припущення в spec і познач їх).
2. Прочитай зачеплені `specs/capabilities/*.md`, `specs/constitution.md`, і **код** файлів із їхніх
   таблиць «Код». Перевір фактичні сигнатури, моделі, міграції, події.
3. Створи `plan.md` за `specs/_templates/plan.md`:
   - підсумок рішення і чому саме воно;
   - чекліст конституції (I–IX) з обґрунтуванням кожного ⚠️;
   - зміни за шарами з прив'язкою до REQ-ID;
   - міграції (наступний номер у `MIGRATIONS`, `_migrate` для DuckDB) і сумісність зі старими файлами;
   - контракти: API (метод, шлях, тіло, коди), нові події (для каталогу в `00-domain-model.md`),
     нові `DAAS_*`;
   - ризики, відкат, тестова стратегія без мережі.
4. Архітектурне рішення → чернетка ADR для `docs/02` §10 у plan.md.
5. Не пиши код. Запропонуй наступний крок `/sdd-tasks NNN`.
__SDD_EOF__
echo "created .claude/commands/sdd-plan.md"

mkdir -p ".claude/commands"
cat > ".claude/commands/sdd-tasks.md" <<'__SDD_EOF__'
---
description: SDD крок 4 — розбити план на задачі (тести першими)
argument-hint: <NNN>
---

Розбий план зміни **$ARGUMENTS** на задачі.

1. Прочитай `spec.md` і `plan.md` зміни.
2. Створи `tasks.md` за `specs/_templates/tasks.md`:
   - нумерація `T001…`, фази: підготовка → тести (мають падати) → реалізація → перевірка → sync;
   - **кожна** REQ зі spec має щонайменше одну задачу-тест із точним шляхом
     `backend/tests/test_<…>.py::test_<…>` (щасливий шлях і відмова);
   - `[P]` лише для задач у різних файлах без залежностей;
   - окремі задачі на події, міграції, `.env.example`, `frontend/src/types.ts`/`api.ts`, UI-стани,
     якщо вони зачеплені планом;
   - фінальні задачі: `make specs-check test lint build`, перенесення дельти в
     `specs/capabilities/`, оновлення `docs/03`, архівування.
3. Онови статус зміни в `specs/changes/README.md` на `planned`.
4. Не пиши код. Запропонуй `/sdd-implement NNN`.
__SDD_EOF__
echo "created .claude/commands/sdd-tasks.md"

mkdir -p ".claude/commands"
cat > ".claude/commands/sdd-implement.md" <<'__SDD_EOF__'
---
description: SDD кроки 5–7 — реалізувати задачі, перевірити, синхронізувати спеки, архівувати
argument-hint: <NNN>
---

Реалізуй зміну **$ARGUMENTS** строго за `tasks.md`.

1. Прочитай `spec.md`, `plan.md`, `tasks.md` зміни і `AGENTS.md` §2 (непорушні правила).
2. Виконуй задачі по порядку:
   - спершу тести — переконайся, що вони **падають** з очікуваної причини;
   - потім мінімальна реалізація, що робить їх зеленими;
   - одразу відмічай `[x]` у `tasks.md`;
   - якщо реальність розходиться з планом — зупинись, онови `plan.md`/`spec.md`, поясни чому, потім
     продовжуй.
3. Перевірка: `make specs-check`, `make test`, `make lint`, `make build`. Чесно повідом, що не вдалося
   запустити.
4. Sync:
   - перенеси дельту в `specs/capabilities/*.md`: нові/змінені REQ з посиланнями
     `` `tests/…py::test_…` ``, рядок «Код» (нові файли), каталог подій у `00-domain-model.md`,
     таблицю ендпоінтів у `11-api-and-security.md`;
   - прибери закриті пункти з розділів «Прогалини» / «Відкриті питання»;
   - онови статус і беклог у `docs/03_IMPLEMENTATION_PLAN.md`, ADR у `docs/02` за потреби;
   - перемісти `specs/changes/NNN-slug/` у `specs/changes/archive/` і онови `specs/changes/README.md`.
5. Підсумок: змінені REQ-ID, нові тести, що не перевірено, відкриті питання.
__SDD_EOF__
echo "created .claude/commands/sdd-implement.md"

mkdir -p ".claude/commands"
cat > ".claude/commands/sdd-bugfix.md" <<'__SDD_EOF__'
---
description: SDD короткий шлях для бага — тест, фікс, посилання у спеці
argument-hint: <опис бага>
---

Виправ баг: **$ARGUMENTS**

1. Знайди в `specs/capabilities/` вимогу, яку порушує поведінка (таблиця «Код» → файл → REQ).
   - Якщо вимога є і правильна — спека не змінюється.
   - Якщо вимоги немає або вона неправильна — спершу виправ/додай вимогу (новий ID, якщо нова).
   - Якщо поведінка описана в «Спостережувана поведінка / відкриті питання» — це рішення людини:
     опиши варіанти і спитай, перш ніж міняти.
   - Якщо фікс зачіпає API, схему БД, комплаєнс, безпеку чи PII — це не короткий шлях, використай
     `/sdd-specify`.
2. Напиши офлайн-тест, що відтворює баг, і переконайся, що він падає.
3. Мінімальний фікс → тест зелений.
4. Додай посилання на тест у **Перевірка** відповідної REQ; прибери пункт із «Прогалин», якщо закрито.
5. `make specs-check test lint` → підсумок: REQ-ID, тест, причина бага.
__SDD_EOF__
echo "created .claude/commands/sdd-bugfix.md"

mkdir -p ".github"
cat > ".github/copilot-instructions.md" <<'__SDD_EOF__'
# Copilot instructions

Повні правила — у [`AGENTS.md`](../AGENTS.md). Коротко:

- Проєкт працює за **Spec-Driven Development**: специфікації в `specs/` — джерело правди. Жодної
  зміни поведінки без зміни спеки (`specs/changes/NNN-slug/spec.md → plan.md → tasks.md`, потім
  синхронізація `specs/capabilities/*.md`).
- Перед зміною файлу знайди спеку-власника в `specs/capabilities/` (рядок «Код») і дотримуйся її
  вимог `REQ-…`.
- Тест пишеться до реалізації; кожна вимога має посилання `` `tests/<file>.py::<test>` ``.
- Тести офлайн (`FixtureFetcher`, `tests/fixtures/`), LLM лише як guarded fallback, комплаєнс
  (robots, реєстр джерел, logged-out, SSRF) і маскування PII не послаблюються.
- Тексти для користувача — українською; ідентифікатори — англійською.
- Перевірки: `make specs-check test lint build`.
__SDD_EOF__
echo "created .github/copilot-instructions.md"

mkdir -p ".github"
cat > ".github/pull_request_template.md" <<'__SDD_EOF__'
## Що і навіщо

<!-- 1–3 речення. Посилання на беклог (P1-xx / D-xx), якщо є. -->

## Специфікація

- Тип зміни: <!-- фіча / баг (код ≠ спека) / баг (спека неправильна) / рефакторинг / документація -->
- Зміна: <!-- specs/changes/NNN-slug або «—» -->
- Вимоги: <!-- ADDED: REQ-…; MODIFIED: REQ-…; REMOVED: REQ-… -->

## Чекліст (AGENTS.md §4)

- [ ] Спека описує нову поведінку; `specs/capabilities/*.md` синхронізовані
- [ ] Кожна нова/змінена REQ має посилання на тест; прогалини позначені чесно
- [ ] Офлайн-тести: щасливий шлях і відмова
- [ ] Події / міграції / `.env.example` / `frontend/src/types.ts` оновлені, якщо зачеплені
- [ ] Комплаєнс і маскування PII не послаблені (або є зміна конституції з ADR)
- [ ] `docs/03` і ADR у `docs/02` оновлені за потреби
- [ ] `make specs-check test lint build` зелені
- [ ] Зміну перенесено в `specs/changes/archive/`
__SDD_EOF__
echo "created .github/pull_request_template.md"

EXPECTED_MAKEFILE_SHA="aae6d53f603a8cc7c7ce416bf1e8bd3e76ae9c78498cfee379fb18862826a231"
CURRENT_MAKEFILE_SHA="$( (sha256sum Makefile 2>/dev/null || shasum -a 256 Makefile) | cut -d" " -f1)"
if [ "$CURRENT_MAKEFILE_SHA" = "$EXPECTED_MAKEFILE_SHA" ]; then
cat > Makefile <<'__SDD_EOF__'
SHELL := /bin/bash
.DEFAULT_GOAL := help

.PHONY: help setup backend-setup frontend-setup test specs-check lint fmt build demo serve doctor clean

help: ## Показати команди
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: backend-setup frontend-setup build ## Встановити залежності та зібрати UI

backend-setup: ## Python-залежності (uv)
	cd backend && uv sync --extra dev

frontend-setup: ## Node-залежності (React/Vite)
	cd frontend && npm install

test: ## Тести backend (офлайн, включно зі specs-check)
	cd backend && uv run pytest -q

specs-check: ## SDD: спеки ↔ тести ↔ модулі (specs/README.md §7)
	cd backend && uv run pytest -q --noconftest tests/test_specs_traceability.py

lint: ## ruff + TypeScript
	cd backend && uv run ruff check app tests
	cd frontend && npm run typecheck

fmt: ## Автовиправлення lint
	cd backend && uv run ruff check app tests --fix

build: ## Зібрати UI у frontend/dist
	cd frontend && npm run build

demo: build ## Офлайн-демо + жива студія на :8000
	cd backend && uv run daas demo --serve

serve: ## Запустити студію
	cd backend && uv run daas serve

doctor: ## Перевірити середовище
	cd backend && uv run daas doctor

clean: ## Прибрати білд-артефакти
	rm -rf frontend/dist backend/.pytest_cache backend/.ruff_cache
__SDD_EOF__
  echo "updated Makefile (added specs-check)"
else
  echo "Makefile changed since review - add the specs-check target manually:" >&2
  printf '%s\n' 'specs-check: ## SDD: specs <-> tests <-> modules' '	cd backend && uv run pytest -q --noconftest tests/test_specs_traceability.py' >&2
fi

echo "Done. Check with: make specs-check && git status"
