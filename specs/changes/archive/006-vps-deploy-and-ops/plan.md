# 006 — План реалізації: Деплой на VPS і автоматизований щоденний бекап

> Plan описує **ЯК**. Посилається на `spec.md`; кожна REQ звідти має тут місце реалізації.

## 1. Підсумок рішення
Додаємо стандартний контейнерний цикл (`Dockerfile` + `.dockerignore` + `docker-compose.prod.yml`)
без змін Python-коду, а також комплект `deploy/` (systemd unit, Caddyfile, POSIX/Bash скрипт
щоденного бекапу та інструкцію з деплою й відновлення). Перевіряємо роботу `Host`-гарду за
reverse proxy та структуру всіх артефактів офлайн-тестами в `backend/tests/test_deploy_ops.py`.

## 2. Перевірка конституції (`specs/constitution.md`)

| Стаття | Відповідність | Примітка |
|:---|:---:|:---|
| I. Спека перед кодом | ✅ | `spec.md`, `plan.md`, `tasks.md` і тест передують артефактам |
| II. Детермінованість, LLM лише fallback | ✅ | Контейнер працює і з вимкненим LLM (`DAAS_LLM_PROVIDER=disabled`) |
| III. Тести офлайн | ✅ | Офлайн перевірка конфігів, синтаксису скрипту та `TestClient` host-guard |
| IV. Походження, append-only | ✅ | Скрипт бекапу викликає API `/api/maintenance/backup`, який знімає узгоджений WAL + DuckDB checkpoint |
| V. Комплаєнс і PII | ✅ | Без відкриття портів назовні; автентифікація завжди увімкнена |
| VI. Local-first, захищений API | ✅ | Порт `127.0.0.1:8000`, host-guard дозволяє домен лише з `DAAS_CORS_ORIGINS` |
| VII. Події для змін даних | ✅ | Живий бекап пише подію `backup_created` через наявний ендпоінт |
| VIII. Типізовані межі, міграції | ✅ | Без змін схеми БД |
| IX. Простота, без зайвих залежностей | ✅ | Нуль нових runtime-залежностей |

## 3. Зміни за шарами

| Шар | Файли | Що змінюється | REQ |
|:---|:---|:---|:---|
| Контейнер | `Dockerfile`, `.dockerignore`, `docker-compose.prod.yml` | Multi-stage Node 22 + Python 3.12 (`uv`), healthcheck `/api/health`, volume `./storage:/app/storage`, bind `127.0.0.1:8000:8000` | REQ-OPS-060 |
| Операції VPS | `deploy/systemd/daas-studio.service`, `deploy/caddy/Caddyfile`, `deploy/scripts/backup-and-upload.sh`, `deploy/README.md` | Юніт systemd, TLS reverse proxy, щоденний бекап через API + fallback + S3/rclone upload + retention | REQ-OPS-061 |
| Тести | `backend/tests/test_deploy_ops.py` | Офлайн тести конфігів деплою, синтаксису скрипта і `Host`-гарду за reverse proxy | REQ-OPS-060, REQ-OPS-061 |

## 4. Дані та міграції
Без міграцій і без нових env-змінних ядра Python (усі потрібні `DAAS_*` уже існують у `app/config.py`).

## 5. Контракти
- Використовується `GET /api/health` і `POST /api/maintenance/backup` (`11-api-and-security.md`, `12-storage-cli-ops.md`).
- Для роботи за доменом через reverse proxy оператор вказує `DAAS_CORS_ORIGINS=https://your-domain.example` у `.env` (REQ-API-003).

## 6. Ризики і відкат
- **Ризик:** у Docker `uv sync` міг би встановити пакет не в editable-режимі, через що зламається обчислення `PROJECT_ROOT` (`Path(__file__).resolve().parents[2]`).
  **Мітигація:** `uv sync --frozen --no-dev` встановлює `backend` у editable-режимі в `/app/backend`, а `DAAS_STORAGE_DIR=/app/storage` задано явно.
- **Відкат:** видалення `Dockerfile`, `docker-compose.prod.yml`, `deploy/` не зачіпає ядро.

## 7. Тестова стратегія
- Написати `backend/tests/test_deploy_ops.py` **до** створення файлів деплою та переконатися, що тести падають.
- Перевірити `bash -n deploy/scripts/backup-and-upload.sh` (і за наявності Docker — валідацію синтаксису).
- Перевірити через `TestClient` поведінку `Host`-гарду з `DAAS_HOST=0.0.0.0` і `DAAS_CORS_ORIGINS=https://studio.example.com` + живий `POST /api/maintenance/backup`.
