# 013 — План реалізації: Розширені фільтри доставки, режим дайджесту та вкладення для Telegram

> Plan описує **ЯК**. Посилається на [`spec.md`](./spec.md); кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення

Розширюємо `DeliveryFilters` у `backend/app/models.py` новими детермінованими полями (`exclude_keywords`, `cpv_prefixes`, `regions`, `field_filters`, `delivery_mode`, `digest_cron`) зі зворотною сумісністю для старих `filters_json`. Для режиму `digest_daily` додаємо forward-only міграцію SQLite №9 (`delivery_digest_queue` + колонки `next_digest_at`, `last_digest_at` у `subscriptions`) та інтегруємо перевірку прострочених дайджестів у `TaskScheduler.tick()`. У `DeltaDispatcher` додаємо формування читабельних повідомлень Telegram і генерацію безпечного `.xlsx` у пам'яті (`openpyxl` + `_defuse`) з відправкою через Telegram `sendDocument`, коли кількість змін перевищує `DAAS_DELIVERY_INLINE_LIMIT`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md` → `plan.md` → `tasks.md` → тести → код → sync `capabilities/`. |
| II. Детермінованість, LLM лише fallback | ✅ | Усі фільтри, cron дайджесту, форматування й XLSX-експорт повністю детерміновані (0 LLM). |
| III. Тести офлайн | ✅ | Використовуються `httpx.MockTransport` та тимчасова SQLite БД; жодних зовнішніх запитів. |
| IV. Походження, append-only | ✅ | Кожна зміна в повідомленні, webhook та XLSX зберігає `record_key`, `source_url`; `delivery_log` фіксує доставлені ключі. |
| V. Комплаєнс і PII | ✅ | Поля `pii="personal"` редагуються до фільтрації та до генерації JSON/HTML/XLSX; у XLSX діє `_defuse` від formula injection. |
| VI. Local-first, захищений API | ✅ | Черга дайджестів зберігається в локальному SQLite WAL; новий endpoint `/flush` захищений Bearer-токеном і Host-guard. |
| VII. Події для змін даних | ✅ | Додається подія `digest_queued` у каталог `00-domain-model.md` §4; відправка/збій фіксуються `delivery_sent`/`delivery_failed`. |
| VIII. Типізовані межі, міграції | ✅ | Pydantic-валідація `DeliveryFilters` і `SubscriptionView`, forward-only міграція SQLite №9 + тест відкриття БД v8. |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових пакетів — використовуємо наявний `openpyxl`, `httpx`, `TaskScheduler`. |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Налаштування | `backend/app/config.py`, `.env.example` | `delivery_inline_limit` (`DAAS_DELIVERY_INLINE_LIMIT=10`) | `REQ-DEL-006`, `REQ-OPS-001` |
| Моделі | `backend/app/models.py` | `DeliveryMode`, розширені поля `DeliveryFilters` + валідація `digest_cron`, `next_digest_at`/`last_digest_at`/`pending_digest_count` на `SubscriptionInfo` | `REQ-DEL-004`, `REQ-DEL-005` |
| Сховище | `backend/app/db/sqlite_store.py` | Міграція №9 (`delivery_digest_queue`, колонки в `subscriptions`), методи `enqueue_digest_items`, `list_pending_digest`, `list_due_digest_subscriptions`, `mark_digest_flushed`, `advance_subscription_digest`, підрахунок очікуючих змін | `REQ-DEL-005`, `REQ-OPS-010` |
| Доставка | `backend/app/pipeline/delivery.py` | Розширений `match_filters`, постановка в чергу для `digest_daily`, `flush_subscription`, `flush_due_digests`, форматування Telegram, генерація XLSX (`openpyxl` + `_defuse`) і `_post_telegram_document` (`sendDocument`) | `REQ-DEL-004`, `REQ-DEL-005`, `REQ-DEL-006` |
| Планувальник і Runtime | `backend/app/pipeline/scheduler.py`, `backend/app/runtime.py` | Виклик `dispatcher.flush_due_digests(now=current)` у `TaskScheduler.tick()`, обчислення `next_digest_at` при CRUD підписок, `Studio.flush_subscription` | `REQ-DEL-005` |
| API | `backend/app/api/schemas.py`, `backend/app/api/tasks.py` | Поля `next_digest_at`, `last_digest_at`, `pending_digest_count` у `SubscriptionView`; `POST /api/tasks/{id}/subscriptions/{sub_id}/flush` | `REQ-DEL-005` |
| CLI | `backend/app/cli.py` | Прапорці `--exclude`, `--cpv`, `--region`, `--field`, `--mode`, `--digest-cron` у `daas notify add`; команда `daas notify flush` | `REQ-DEL-004`, `REQ-DEL-005`, `REQ-OPS-030` |
| Frontend | `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/components/AutomationPanel.tsx` | Типи нових фільтрів і полів дайджесту, `flushSubscription` у `api.ts`, UI-поля фільтрів/дайджесту та кнопка «Надіслати зараз» | `REQ-UI-017` |

## 4. Дані та міграції

- **SQLite міграція №9** у `MIGRATIONS` (`backend/app/db/sqlite_store.py`):
  ```sql
  ALTER TABLE subscriptions ADD COLUMN next_digest_at TEXT;
  ALTER TABLE subscriptions ADD COLUMN last_digest_at TEXT;

  CREATE TABLE delivery_digest_queue (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
      task_id TEXT NOT NULL,
      dataset_version INTEGER NOT NULL,
      run_id TEXT,
      dedup_key TEXT NOT NULL,
      change_json TEXT NOT NULL,
      record_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      flushed_at TEXT,
      UNIQUE(subscription_id, dedup_key)
  );
  CREATE INDEX idx_digest_queue_pending
      ON delivery_digest_queue(subscription_id, flushed_at, id);
  ```
- **Сумісність:** БД із `user_version = 8` (і нижче) автоматично оновлюється до `9`. Додаємо тест `test_sqlite_migration_v9_opens_v8_database`.

## 5. Контракти

- **API (`/api/tasks/{task_id}/subscriptions`):**
  - `SubscriptionView` поповнюється полями: `next_digest_at: str | None`, `last_digest_at: str | None`, `pending_digest_count: int`.
  - `POST /api/tasks/{task_id}/subscriptions/{sub_id}/flush` → `200 {ok: bool, count: int, detail: str}` (`404` для невідомої задачі або підписки).
- **Події (`specs/capabilities/00-domain-model.md` §4):**
  - `digest_queued` (`info`, `pipeline/delivery.py`) — `data.subscription_id`, `data.count`, `data.version`.
- **Налаштування `DAAS_*`:**
  - `DAAS_DELIVERY_INLINE_LIMIT=10` — максимум інлайн-рядків у одному Telegram-повідомленні, після якого додається XLSX-вкладення.

## 6. Ризики і відкат

| Ризик | Як помітимо | Мітигація / відкат |
|:---|:---|:---|
| Дублювання тексту, якщо `sendMessage` успішний, а `sendDocument` падає | Подвійні повідомлення в Telegram | Фіксуємо `delivery_log` для доставленого `sendMessage` і логуємо окрему подію `delivery_failed` (`attachment=true`) для вкладення |
| Проблемний `flush` спамить у циклі планувальника кожні 15 с | Багато `delivery_failed` щохвилини | Завжди пересуваємо `next_digest_at` на наступний cron-слот після спроби планової відправки, зберігаючи записи в черзі (`flushed_at IS NULL`) |
| Зміна `DeliveryFilters` ламає старі записи в `subscriptions` | Помилка валідації при `list_subscriptions` | Усі нові поля мають безпечні дефолти; `digest_cron` при порожньому значенні нормалізується до `0 6 * * *`, при невалідному cron — явна помилка 422 |

## 7. Тестова стратегія (без мережі)

`backend/tests/test_delivery.py`:
1. `REQ-DEL-004`: перевірка `exclude_keywords`, `cpv_prefixes`, `regions`, `field_filters` (точний і `*` префіксний збіг), комбінування AND/OR та відсіювання після редагування `pii="personal"`.
2. `REQ-DEL-005`: створення підписки `digest_daily` (включно з 422 на невалідний `digest_cron`), постановка кількох версій у чергу `delivery_digest_queue` без HTTP-запитів, перевищення рестарту (`OperationalStore`), спрацювання `TaskScheduler.tick(now=...)` (одне зведення + дедуплікація), збереження черги при збої доставки, ручний `POST .../flush` і CLI `daas notify flush`.
3. `REQ-DEL-006`: форматування Telegram-повідомлень із сумою/регіоном/CPV/посиланням; пачка з 12 змін (при `delivery_inline_limit=10`) → `sendMessage` (10 рядків) + `sendDocument` (`.xlsx`), перевірка вмісту XLSX через `openpyxl` (12 рядків, маскований `pii="personal"`, `_defuse` для `=formula`), плюс гілка падіння `sendDocument` після успішного `sendMessage`.
4. `REQ-OPS-010`: `test_sqlite_migration_v9_opens_v8_database` відкриває БД `user_version=8` із наявними підписками і перевіряє чергу дайджестів.
5. `REQ-OPS-030`: `daas notify add` з новими прапорцями та `daas notify flush`.
6. Frontend: `npm run typecheck` і `npm run build` перевіряють синхронність `types.ts`, `api.ts` та `AutomationPanel.tsx`.
