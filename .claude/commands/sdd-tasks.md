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
