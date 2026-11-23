# 003 — План реалізації: Канали доставки дельти та алертів (`D-01`)

> Plan описує **ЯК**. Посилається на [`spec.md`](./spec.md); кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення

Реалізуємо підписки (`subscriptions`) та ідемпотентний журнал доставки (`delivery_log`) у SQLite
(міграція №6) і окремий асинхронний сервіс [`DeltaDispatcher`](../../../backend/app/pipeline/delivery.py),
який підключається в `Studio` та `PipelineRunner` після фіксації версії / снапшоту чи при помилці
запуску. Канали `webhook` (JSON + опційний HMAC-SHA256) і `telegram` (Bot API `sendMessage`)
працюють поверх `httpx` без жодних нових залежностей, із вбудованим SSRF-захистом, маскуванням
`pii="personal"` і ретраями при `5xx`/`429`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` → `plan.md` → `tasks.md` → тести → код → sync `capabilities/`. |
| II. Детермінованість, LLM лише fallback | ✅ | Фільтрація й форматування повністю детерміновані; 0 LLM. |
| III. Тести офлайн | ✅ | `httpx.MockTransport` і `FixtureFetcher`; жодного зовнішнього мережевого виклику. |
| IV. Походження, append-only | ✅ | Сповіщення містять `source_url` та версію; `delivery_log` фіксує доставлені ключі. |
| V. Комплаєнс і PII | ✅ | Поля `pii="personal"` завжди приховуються; webhook URL перевіряються `assert_logged_out` і `assert_safe_url`, редиректи заборонені. |
| VI. Local-first, захищений API | ✅ | Усе зберігається в локальному SQLite; API захищений Bearer-токеном і Host-guard; секрети маскуються у відповідях. |
| VII. Події для змін даних | ✅ | Додано `subscription_created`, `subscription_updated`, `subscription_deleted`, `delivery_sent`, `delivery_failed` у `00-domain-model.md`. |
| VIII. Типізовані межі, міграції | ✅ | `DeliveryFilters`, `SubscriptionInfo`, `SubscriptionView`, forward-only міграція №6 + тест відкриття БД версії 5. |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових бібліотек — використовуємо наявний `httpx`, `hmac`, `hashlib`, `html`. |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Налаштування | `backend/app/config.py`, `.env.example` | `telegram_bot_token`, `delivery_timeout_s`, `delivery_max_retries` | `REQ-DEL-003`, `REQ-OPS-001` |
| Моделі | `backend/app/models.py` | `DeliveryChannel`, `DeliveryFilters`, `SubscriptionInfo` | `REQ-DEL-001`, `REQ-DEL-002` |
| Сховище | `backend/app/db/sqlite_store.py` | Міграція №6 (`subscriptions`, `delivery_log`), CRUD підписок і методи перевірки/фіксації `delivery_log` | `REQ-DEL-001`, `REQ-DEL-003`, `REQ-OPS-010` |
| Доставка | `backend/app/pipeline/delivery.py` (новий) | `DeltaDispatcher`, фільтрація змін, маскування `personal` PII, HMAC-підпис, Telegram formatter, ретраї, дедуплікація, owner-алерти | `REQ-DEL-002`, `REQ-DEL-003` |
| Пайплайн / Runtime | `backend/app/pipeline/runner.py`, `backend/app/runtime.py` | Виклик `dispatcher.dispatch_changes` у `_finish_dataset` і `dispatch_alert` у `_fail`/порожньому результаті; `Studio.dispatcher` + CRUD методи | `REQ-DEL-001`, `REQ-DEL-003` |
| API / Frontend | `backend/app/api/schemas.py`, `backend/app/api/tasks.py`, `frontend/src/types.ts`, `frontend/src/api.ts` | `SubscriptionView`, `CreateSubscriptionRequest`, `UpdateSubscriptionRequest`, 5 ендпоінтів `/api/tasks/{id}/subscriptions*` | `REQ-DEL-001` |
| CLI | `backend/app/cli.py` | Група команд `daas notify add \| list \| delete \| test` | `REQ-DEL-001`, `REQ-OPS-030` |

## 4. Дані та міграції

- **SQLite міграція №6** у `MIGRATIONS` (`backend/app/db/sqlite_store.py`):
  ```sql
  CREATE TABLE subscriptions (
      id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
      name TEXT NOT NULL DEFAULT '',
      channel TEXT NOT NULL,
      target TEXT NOT NULL,
      secret TEXT NOT NULL DEFAULT '',
      filters_json TEXT NOT NULL DEFAULT '{}',
      enabled INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
  );
  CREATE INDEX idx_subscriptions_task ON subscriptions(task_id, enabled);

  CREATE TABLE delivery_log (
      subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
      dedup_key TEXT NOT NULL,
      run_id TEXT,
      status TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 1,
      error TEXT,
      created_at TEXT NOT NULL,
      delivered_at TEXT,
      PRIMARY KEY (subscription_id, dedup_key)
  );
  ```
- **Сумісність:** БД з `user_version = 5` (і нижче) автоматично оновлюється до `6`. Додаємо
  явний тест `test_sqlite_migration_v6_opens_v5_database`.

## 5. Контракти

- **API (`/api/tasks/{task_id}/subscriptions`):**
  - `GET /api/tasks/{task_id}/subscriptions` → `200 {items: SubscriptionView[]}` (без відкритого `secret`, із `has_secret: bool`)
  - `POST /api/tasks/{task_id}/subscriptions` (`CreateSubscriptionRequest`) → `201 {subscription: SubscriptionView}` (400 при SSRF/облікових даних або невалідному target)
  - `PATCH /api/tasks/{task_id}/subscriptions/{sub_id}` (`UpdateSubscriptionRequest`) → `200 {subscription: SubscriptionView}`
  - `DELETE /api/tasks/{task_id}/subscriptions/{sub_id}` → `200 {deleted: bool}`
  - `POST /api/tasks/{task_id}/subscriptions/{sub_id}/test` → `200 {ok: bool, detail: str}`
- **Події (`specs/capabilities/00-domain-model.md` §4):**
  - `subscription_created` (`info`, `runtime`)
  - `subscription_updated` (`info`, `runtime`)
  - `subscription_deleted` (`info`, `runtime`)
  - `delivery_sent` (`info`, `pipeline/delivery.py`) — `data.subscription_id`, `data.channel`, `data.count`
  - `delivery_failed` (`warning`, `pipeline/delivery.py`) — `data.subscription_id`, `data.channel`, `data.error` (редагований, без токена)
- **Налаштування `DAAS_*`:**
  - `DAAS_TELEGRAM_BOT_TOKEN=` (опційний токен Telegram-бота)
  - `DAAS_DELIVERY_TIMEOUT_S=10.0`
  - `DAAS_DELIVERY_MAX_RETRIES=3`

## 6. Ризики і відкат

| Ризик | Як помітимо | Мітигація / відкат |
|:---|:---|:---|
| Падіння зовнішнього webhook/Telegram зриває збір даних | Прогін позначається `failed` попри збережені дані | `dispatch_*` обгорнутий у `try/except`, помилка пише `delivery_failed`, дані версії лишаються в DuckDB |
| Витік `secret` або `DAAS_TELEGRAM_BOT_TOKEN` у лог/API | Секрет у `Subscription` або URL Telegram у тексті винятку | `SubscriptionView` не має поля `secret` (`has_secret`), помилки `httpx` санітуються (маскування `bot<token>`) |
| Повторне надсилання тих самих змін після рестарту | Дублі в Telegram/Webhook | Первинний ключ `(subscription_id, dedup_key)` у `delivery_log` перевіряється до відправки |
| SSRF через webhook URL або HTTP 302 редирект | Доступ до `127.0.0.1` / metadata IP | `assert_safe_url` при збереженні і перед кожним POST + `follow_redirects=False` |

## 7. Тестова стратегія (без мережі)

`backend/tests/test_delivery.py`:
1. `REQ-DEL-001`: SSRF-відмова на `http://127.0.0.1/hook`, відмова на URL з токеном, створення/оновлення/видалення webhook + telegram підписок, приховування `secret` (`has_secret=true`), події `subscription_*`.
2. `REQ-DEL-002`: фільтрація за `event_types`, `keywords`, `min_amount`/`max_amount`; обов'язкове маскування колонок `pii="personal"` у webhook payload і повідомленні Telegram; HMAC-підпис `X-Daas-Signature`.
3. `REQ-DEL-003`: `httpx.MockTransport` із першим `503` і другим `200` → ретрай успішний, `delivery_sent`, повторний виклик дедуплікується; незворотна помилка (`500` або missing Telegram token) → `delivery_failed` без витоку токена; `notify_on_error=true` надсилає алерт при 0 записів та при `run_failed`.
4. **Міграція №6:** відкриття БД версії `user_version=5` і робота `subscriptions`/`delivery_log`.
5. **CLI:** `daas notify add|list|test|delete` через `CliRunner` + `MockTransport`.
