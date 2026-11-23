# Деплой DaaS Intelligence Studio на 1 VPS

Мінімальні вимоги: VPS з **2 vCPU / 2–4 ГБ RAM / 40 ГБ SSD** (Ubuntu 24.04 / Debian 12), один процес
студії (SQLite WAL + DuckDB single-writer) за TLS-проксі.

## Варіант A — Docker Compose (рекомендовано)

1. Скопіюйте репозиторій на сервер і налаштуйте `.env`:

   ```bash
   cp .env.example .env
   ```

   Обов’язково задайте у `.env`:
   - `DAAS_API_TOKEN=<довгий-випадковий-секрет>` (або візьміть з `storage/runtime.json`)
   - `DAAS_CORS_ORIGINS=https://studio.example.com` (щоб DNS-rebinding guard пропускав ваш домен)
   - `DAAS_TELEGRAM_BOT_TOKEN=...` (для Telegram-алертів)
   - `DAAS_LLM_PROVIDER=disabled` (якщо запускаєте лише детермінований Prozorro/OpenProcurement збір без LLM)

2. Зберіть та підніміть контейнер:

   ```bash
   docker compose -f docker-compose.prod.yml up -d --build
   docker compose -f docker-compose.prod.yml logs -f studio
   ```

   Порт `8000` прив’язаний виключно до `127.0.0.1:8000`.

## Варіант B — systemd (без Docker)

1. Встановіть код у `/opt/daas-intelligence-studio`, виконайте `make setup`.
2. Створіть системного користувача `daas` і `.env`.
3. Встановіть сервіс із `deploy/systemd/daas-studio.service`:

   ```bash
   sudo cp deploy/systemd/daas-studio.service /etc/systemd/system/daas-studio.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now daas-studio
   ```

## Reverse Proxy (HTTPS через Caddy)

Скопіюйте `deploy/caddy/Caddyfile` у `/etc/caddy/Caddyfile`, вкажіть свій домен і перезапустіть
`caddy`. Перевірте, що домен доданий у `DAAS_CORS_ORIGINS`.

## Щоденний автоматичний бекап (`cron`)

Скрипт `deploy/scripts/backup-and-upload.sh` сам визначає, чи працює студія:
- при живому `/api/health` викликає `POST /api/maintenance/backup` (без конфлікту lock DuckDB);
- якщо сервіс зупинено — виконує `daas backup`;
- чистить локальні архіви старші за `KEEP_DAYS` (14 днів);
- при заданому `BACKUP_REMOTE` вивантажує `.zip` у Cloudflare R2 / Backblaze B2 (`rclone`) або S3 (`aws`).

Приклад запису в `crontab -e` (щодня о 03:15 UTC):

```cron
15 3 * * * BACKUP_REMOTE="r2:daas-backups" KEEP_DAYS=14 /opt/daas-intelligence-studio/deploy/scripts/backup-and-upload.sh >> /var/log/daas-backup.log 2>&1
```

## Відновлення з архіву

1. Зупиніть студію (`docker compose -f docker-compose.prod.yml down` або `systemctl stop daas-studio`).
2. Розпакуйте `daas-backup-*.zip` у `storage/` (`operational.sqlite`, `analytics.duckdb`, `snapshots/`).
3. Запустіть студію знову.
