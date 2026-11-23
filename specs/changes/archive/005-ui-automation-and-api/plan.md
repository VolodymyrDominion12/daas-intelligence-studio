# 005 — План реалізації: Веб-інтерфейс для розкладу, доставки дельти та JSON API джерел

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Розширюємо наявний тонкий React-клієнт без змін у backend API (контракти `api.ts` і `types.ts` для
`api_source`, `schedule`, `subscriptions` та `evalTask` уже синхронізовані в попередніх змінах `001`–`004`).
Додаємо пресети OpenProcurement та опційний блок `api_source` у `NewTask.tsx`, зведення JSON API
у `PlanPanel.tsx`, а також нову вкладку «Розклад і доставка» з компонентом `AutomationPanel.tsx` у
`TaskWorkspace.tsx`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` і `plan.md` створені перед редагуванням UI |
| II. Детермінованість, LLM лише fallback | ✅ | UI не викликає LLM напряму і працює в offline-режимі |
| III. Тести офлайн | ✅ | Backend-тести лишаються офлайн; UI перевіряється `tsc` + `vite build` |
| IV. Походження, append-only | ✅ | Тільки читання `quality_checks`/`eval` із маніфесту версії |
| V. Комплаєнс і PII | ✅ | Повтор 428 (`acknowledge_high_risk` + `reason`) у розкладі; секрет не розкривається |
| VI. Local-first, захищений API | ✅ | Всі запити йдуть через `api.ts` з Bearer-токеном |
| VII. Події для змін даних | ✅ | Події пишуться backend-ом при зміні розкладу/підписок/eval |
| VIII. Типізовані межі, міграції | ✅ | Строгий TypeScript без `any`; без змін схеми БД |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових пакетів у `package.json` |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| UI створення задачі | `frontend/src/components/NewTask.tsx`, `frontend/src/App.tsx` | Приклади OpenProcurement із seed-URL, згорнутий редактор `api_source`, прокидання `api_source` в `api.createTask` | REQ-UI-010 |
| UI плану | `frontend/src/components/PlanPanel.tsx` | Показ налаштувань `api_source` (`items_path`, `id_path`, `cursor_path`, `incremental`, `field_map`) | REQ-UI-010 |
| UI автоматизації і якості | `frontend/src/components/AutomationPanel.tsx` (новий), `frontend/src/components/TaskWorkspace.tsx` | Вкладка «Розклад і доставка», керування cron (включно з 428 ack), CRUD/test підписок Telegram/Webhook, перегляд якості та запуск `evalTask` | REQ-UI-011, REQ-UI-012 |
| Стилі | `frontend/src/styles.css` | Стиль `.method--api_json`, адаптивні стилі карток і списків автоматизації | REQ-UI-010, REQ-UI-011, REQ-UI-012 |

## 4. Дані та міграції
Без змін БД або API-контрактів.

## 5. Контракти
Використовуються чинні ендпоінти з `specs/capabilities/11-api-and-security.md`:
- `POST /api/tasks` (`api_source`)
- `GET/PUT/DELETE /api/tasks/{id}/schedule`
- `GET/POST/PATCH/DELETE /api/tasks/{id}/subscriptions`, `POST .../test`
- `POST /api/tasks/{id}/eval`

## 6. Ризики і відкат
- **Ризик:** при перемиканні між задачами локальний стейт форм у `AutomationPanel` може зберегти старі дані.
  **Мітигація:** `TaskWorkspace` монтується з `key={detail.task.id}`, плюс `AutomationPanel` перезавантажує `schedule` та `subscriptions` по `task.id`.
- **Відкат:** локальна заміна/видалення `AutomationPanel.tsx` і повернення попередніх версій 4 файлів UI.

## 7. Тестова стратегія
- `make specs-check` — трасування спеки й модулів.
- `make lint` (`ruff` + `npm run typecheck`) — сувора перевірка типів TS для всіх нових компонентів.
- `make build` (`tsc -b && vite build`) — повна продакшн-збірка в `frontend/dist/`.
- `make test` — повний офлайн-прогін тестів backend.
