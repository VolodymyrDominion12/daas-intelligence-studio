# 015 — План реалізації: Тонкий Telegram-бот підписника

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
1. Створити ізольований пакет `backend/app/bot/` із чотирьох компактних модулів:
   - `client.py`: `StudioApiClient` — тонкий HTTP-клієнт на `httpx.AsyncClient` до REST API студії (`/api/tasks`, `/api/tasks/{id}/subscriptions*`) із `Authorization: Bearer` (підтримка кастомного `transport` для офлайн-тестів) та `TelegramBotClient` (`sendMessage`, `answerCallbackQuery`, `getUpdates`).
   - `state.py`: `BotSubscriberStore` та Pydantic-моделі `SubscriberProfile` (`plan`, `trial_ends_at`, `plan_expires_at`, `active_task_id`, `updated_at`), що зберігаються атомарно у JSON-файлі (`DAAS_BOT_STATE_FILE`), без будь-якого доступу до DuckDB чи `operational.sqlite`.
   - `service.py`: `SubscriberBotService` — детермінований обробник Telegram-оновлень (`message` і `callback_query`), діалогового стану, команд (`/start`, `/select`, `/cpv`, `/region`, `/budget`, `/keywords`, `/exclude`, `/mode`, `/status`, `/pause`, `/resume`, `/flush`, `/upgrade`), перевірки прав тарифу й ізоляції по `chat_id`.
   - `app.py`: `create_bot_app` (FastAPI) для `GET /health`, `POST /telegram/webhook` (перевірка `X-Telegram-Bot-Api-Secret-Token`), `POST /billing/webhook` (перевірка `X-Billing-Secret` або `X-Signature: sha256=...`) та `poll_bot_once` / `run_bot_polling` для режиму long-polling.
2. Додати налаштування `DAAS_BOT_*` у `backend/app/config.py` і `.env.example` та CLI-групу `daas bot serve|status|grant` у `backend/app/cli.py`.
3. Не змінювати існуючі таблиці SQLite/DuckDB студії та не додавати зовнішніх залежностей.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` → `plan.md` → `tasks.md` → тести → реалізація → sync |
| II. Детермінованість, LLM лише fallback | ✅ | Повністю детермінована маршрутизація команд і inline-кнопок без LLM |
| III. Тести офлайн | ✅ | `httpx.MockTransport` для Telegram Bot API + FastAPI `ASGITransport` до студії без мережі |
| IV. Походження, append-only | ✅ | Дані й версії датасетів залишаються у студії; підписки створюються через її API |
| V. Комплаєнс і PII | ✅ | Усю фільтрацію й маскування `pii="personal"` виконує `DeltaDispatcher` студії; бот ізолює `chat_id` і не розкриває секретів/URL |
| VI. Local-first, захищений API | ✅ | Бот працює локально поверх Bearer API студії, не відкриває DuckDB (зберігає ADR-02) |
| VII. Події для змін даних | ✅ | Операції з підписками через API студії автоматично пишуть `subscription_created`, `subscription_updated` у журнал студії |
| VIII. Типізовані межі, міграції | ✅ | Pydantic v2 моделі в `bot/state.py` і `bot/app.py`; без змін схеми SQLite/DuckDB |
| IX. Простота, без зайвих залежностей | ✅ | Жодних нових залежностей: лише існуючі `httpx`, `fastapi`, `pydantic`, `typer` |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Конфігурація | `backend/app/config.py`, `.env.example` | Поля `DAAS_BOT_*` (`bot_studio_url`, `bot_studio_token`, `bot_webhook_secret`, `bot_billing_secret`, `bot_payment_url`, `bot_trial_days`, `bot_state_file`, `bot_poll_timeout_s`) | `REQ-DEL-011`, `REQ-OPS-001` |
| Бот-клієнт і стан | `backend/app/bot/client.py`, `backend/app/bot/state.py` | REST-клієнт до API студії (`StudioApiClient`), `TelegramBotClient`, персистентний JSON-стор профілів підписників (`SubscriberProfile`, `BotSubscriberStore`) | `REQ-DEL-008`, `REQ-DEL-009` |
| Логіка бота | `backend/app/bot/service.py` | Обробка команд `/start`, `/select`, `/cpv`, `/region`, `/budget`, `/keywords`, `/exclude`, `/mode`, `/status`, `/pause`, `/resume`, `/flush`, `/upgrade`, callback-кнопки, тарифні правила, ізоляція `chat_id` | `REQ-DEL-008`, `REQ-DEL-009`, `REQ-DEL-010` |
| HTTP і polling | `backend/app/bot/app.py` | `create_bot_app` (`/health`, `/telegram/webhook`, `/billing/webhook`), `poll_bot_once`, `run_bot_polling` | `REQ-DEL-011` |
| CLI | `backend/app/cli.py` | Підкоманди `daas bot serve`, `daas bot status`, `daas bot grant` | `REQ-DEL-011`, `REQ-OPS-030` |

## 4. Дані та міграції
- БД студії (`operational.sqlite`, `analytics.duckdb`) не змінюються: підписки створюються й оновлюються через REST API студії.
- Стан тарифів і діалогу бота зберігається у `DAAS_BOT_STATE_FILE` (типово `<storage_dir>/bot_subscribers.json`) із версією схеми `"version": 1` та атомарним записом через `.tmp` + `os.replace`.

## 5. Контракти
- **Нові налаштування `DAAS_*`:**
  - `DAAS_BOT_STUDIO_URL` (типово `http://127.0.0.1:8000`)
  - `DAAS_BOT_STUDIO_TOKEN` (опційно; інакше з `api_token` / `runtime.json`)
  - `DAAS_BOT_WEBHOOK_SECRET` (секрет заголовка `X-Telegram-Bot-Api-Secret-Token`)
  - `DAAS_BOT_BILLING_SECRET` (секрет / HMAC ключ для `POST /billing/webhook`)
  - `DAAS_BOT_PAYMENT_URL` (шаблон URL оплати для `/upgrade`)
  - `DAAS_BOT_TRIAL_DAYS` (типово `7`)
  - `DAAS_BOT_STATE_FILE` (типово `bot_subscribers.json`)
  - `DAAS_BOT_POLL_TIMEOUT_S` (типово `25`)
- **HTTP-контракт бота (`backend/app/bot/app.py`):**
  - `GET /health` → `200 {"status": "ok", "studio_url": "..."}`
  - `POST /telegram/webhook` → перевіряє `X-Telegram-Bot-Api-Secret-Token` (401 при невідповідності, якщо задано `DAAS_BOT_WEBHOOK_SECRET`), обробляє Telegram Update JSON → `200 {"ok": true, "handled": bool}`
  - `POST /billing/webhook` → `503`, якщо `DAAS_BOT_BILLING_SECRET` порожній; `401`, якщо `X-Billing-Secret` або `X-Signature` (`sha256=...`) не збігається; приймає `{"chat_id": "...", "plan": "pro"|"b2b"|"free", "days": 30}` → `200 {"ok": true, "profile": ...}`
- **CLI-контракт (`backend/app/cli.py`):**
  - `daas bot serve [--mode polling|webhook --host 127.0.0.1 --port 8081]`
  - `daas bot status [--chat-id <id>]`
  - `daas bot grant <chat_id> --plan <free|trial|pro|b2b> [--days 30]`

## 6. Ризики і відкат
- **Недоступність REST API студії під час запиту користувача в Telegram:** кожен виклик `StudioApiClient` перехоплює мережеві/HTTP помилки й відповідає користувачу коректним повідомленням українською без падіння webhook- чи polling-циклу.
- **Відсутність автентифікації платіжного webhook-а:** `POST /billing/webhook` за замовчуванням відмовляє (`503`), якщо не задано `DAAS_BOT_BILLING_SECRET`, і перевіряє секрет/HMAC через `hmac.compare_digest`.
- **Відкат:** ізольований пакет `app/bot/` і CLI-група `bot`; видалення не зачіпає існуючі дані чи міграції студії.

## 7. Тестова стратегія
- Файл тестів: `backend/tests/test_subscriber_bot.py` (повністю офлайн):
  1. `test_bot_uses_studio_rest_api_and_isolates_chat_ids` (`REQ-DEL-008`): піднімається реальний FastAPI-додаток студії через `httpx.ASGITransport` (плюс перевірка недоступного API та відсутність прямого доступу до DuckDB), два різних `chat_id` і Webhook-підписка ізолюються строго за `target`.
  2. `test_bot_free_trial_upgrade_and_plan_enforcement` (`REQ-DEL-009`): `/start` видає 7-денний `trial`, `instant` дозволено; після закінчення тріалу (`free`) спроба `/mode instant` відхиляється з підказкою `/upgrade`; `/upgrade` повертає безпечне посилання з `chat_id`.
  3. `test_bot_filter_setup_pause_resume_and_flush` (`REQ-DEL-010`): повний self-service цикл (`/start` → `/select` → `/cpv` → `/region` → `/budget` → `/keywords` → `/exclude` → `/mode` → `/status` → `/pause` → `/resume` → `/flush`) та перевірка відмов на некоректних аргументах.
  4. `test_bot_webhooks_polling_and_cli_commands` (`REQ-DEL-011`): перевірка `POST /telegram/webhook` (401 без/з невірним токеном, 200 з валідним), `POST /billing/webhook` (503 без налаштованого секрету, 401 при поганому HMAC, 200 при валідному HMAC активує `pro`), одного циклу `poll_bot_once` через `httpx.MockTransport` та CLI `daas bot grant` / `daas bot status`.
