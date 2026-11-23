# 005 — Задачі: Веб-інтерфейс для розкладу, доставки дельти та JSON API джерел

> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, `specs/capabilities/13-frontend.md`, `frontend/src/api.ts`, `frontend/src/types.ts`

## Фаза 1 — Валідація перед реалізацією
- [x] T010 Зафіксувати початковий стан збірки (`make specs-check`) перед правками

## Фаза 2 — Реалізація
- [x] T020 [P] Оновити `frontend/src/App.tsx` і `frontend/src/components/NewTask.tsx` для OpenProcurement прикладів і кастомного `api_source` (REQ-UI-010)
- [x] T021 [P] Оновити `frontend/src/components/PlanPanel.tsx` для відображення `api_source` (REQ-UI-010)
- [x] T022 Створити `frontend/src/components/AutomationPanel.tsx` із керуванням розкладом (cron + пресети + 428 ack), підписками Telegram/Webhook (CRUD + test + фільтри + маскування секрету) та блоком якості/gold-eval (REQ-UI-011, REQ-UI-012)
- [x] T023 Підключити вкладку «Розклад і доставка» в `frontend/src/components/TaskWorkspace.tsx` та додати стилі в `frontend/src/styles.css` (REQ-UI-010, REQ-UI-011, REQ-UI-012)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check`, `make test`, `make lint`, `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Оновити `specs/capabilities/13-frontend.md` (таблиця компонентів + вимоги `REQ-UI-010..012`)
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md`, `specs/changes/README.md` та архівувати `specs/changes/005-ui-automation-and-api/` → `specs/changes/archive/`
