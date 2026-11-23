# 006 — Деплой на VPS і автоматизований щоденний бекап

| Поле | Значення |
|:---|:---|
| Беклог | `docs/05` §5 Тиждень 3 (VPS deploy + щоденний `daas backup` + S3/R2/B2) |
| Статус | done |
| Зачіпає спеки | `capabilities/12-storage-cli-ops.md`, `capabilities/11-api-and-security.md` |
| Автор / дата | AI-agent · 2026-11-23 |

> Spec описує **ЩО** і **НАВІЩО**. Ніяких назв функцій, таблиць чи бібліотек — це для `plan.md`.

## 1. Проблема і мета
Студія вміє збирати OpenProcurement/JSON API та веб-сайти, запускатися за UTC cron-розкладом і
доставляти дельту в Telegram/Webhook, але в репозиторії немає готового виробничого контейнера
(`Dockerfile`, production `docker-compose`), шаблону `systemd` юніту, конфігурації reverse proxy та
безпечного скрипта щоденного бекапу з урахуванням правила «один writer DuckDB» (`daas serve` тримає
БД відкритою).

**Мета:** дати оператору готовий, перевірений комплект для запуску на 1 VPS за 10 хвилин
(Docker Compose або `systemd` + Caddy HTTPS) і автоматизований щоденний бекап із вивантаженням у
зовнішнє об'єктне сховище (S3/R2/Backblaze B2 через `rclone`/`aws`).

## 2. Користувацькі сценарії
1. **Як** оператор продукту, **я хочу** підняти студію на 1 VPS через `docker compose -f docker-compose.prod.yml up -d` або `systemd`, **щоб** UI, планувальник і Telegram/Webhook-доставка працювали 24/7 з персистентним `storage/`.
2. **Як** оператор, **я хочу** відкрити студію за власним доменом по HTTPS через reverse proxy без
   вимкнення DNS-rebinding захисту, **щоб** API залишався захищеним токеном і `Host`-перевіркою.
3. **Як** оператор, **я хочу** запускати щоденний бекап по cron або systemd timer, **щоб** при
   працюючому `daas serve` архів створювався безконфліктно через `POST /api/maintenance/backup`,
   старі локальні копії ротувалися, а свіжий архів опційно вивантажувався в S3/R2/B2.

## 3. Вимоги (дельта до `capabilities/12-storage-cli-ops.md` і `11-api-and-security.md`)

### ADDED

#### REQ-OPS-060 — Контейнерне складання та production Compose для VPS
Репозиторій **ПОВИНЕН** містити:
- `.dockerignore` та багатоетапний `Dockerfile`, який збирає фронтенд (`frontend/dist`) і запускає
  `daas serve` із персистентним сховищем та healthcheck на `/api/health`;
- `docker-compose.prod.yml`, який прокидає порт студії виключно на `127.0.0.1` (для проксування
  через Caddy/Nginx), монтує постійний том `storage/` і підтримує `.env`.
- **Сценарій:** GIVEN чистий репозиторій WHEN будується образ за `Dockerfile` і читається
  `docker-compose.prod.yml` THEN у середовищі визначено збірку фронтенду, команду запуску
  `daas serve`, healthcheck `/api/health` і прив'язку порту лише до `127.0.0.1`.
- **Відмова:** GIVEN у production Compose прокинуто публічний `0.0.0.0:8000:8000` або відсутній
  том `storage` THEN перевірка конфігурації не проходить (небезпечна публікація без proxy / втрата БД).
- **Перевірка:** `tests/test_deploy_ops.py::test_docker_and_compose_configs_are_valid`

#### REQ-OPS-061 — Шаблони VPS (`systemd`, reverse proxy) та безпечний скрипт щоденного бекапу
Репозиторій **ПОВИНЕН** містити каталог `deploy/` з:
- шаблоном `systemd`-сервісу для запуску `daas serve` на VPS;
- шаблоном reverse proxy (Caddy) для HTTPS із підказкою щодо `DAAS_CORS_ORIGINS` / `Host`;
- скриптом щоденного бекапу (`backup-and-upload.sh`), який **ПОВИНЕН**:
  1) при доступному `/api/health` викликати `POST /api/maintenance/backup` із Bearer-токеном
     (`DAAS_API_TOKEN` або `storage/runtime.json`), щоб не порушити lock DuckDB;
  2) при зупиненій студії використовувати `daas backup` як fallback;
  3) ротувати старі локальні `.zip` бекапи за `KEEP_DAYS` (за замовчуванням 14) і за наявності
     налаштованого `BACKUP_REMOTE` вивантажувати архів через `rclone` або `aws`;
- інструкцією деплою й відновлення (`deploy/README.md`).
- **Сценарій:** GIVEN студія працює локально або в Docker WHEN запускається скрипт бекапу THEN він
  синтаксично валідний (`bash -n`), викликає живий API бекапу без прямого відкриття DuckDB і
  формує коректні команди вивантаження/ротації.
- **Відмова:** GIVEN жива студія тримає DuckDB lock WHEN запускається прямий CLI `daas backup` без
  API THEN CLI не пошкоджує БД і підказує використовувати `POST /api/maintenance/backup`.
- **Перевірка:** `tests/test_deploy_ops.py::test_deploy_templates_and_backup_script`,
  `tests/test_deploy_ops.py::test_host_guard_allows_cors_domain_behind_proxy`

## 4. Нефункціональні вимоги
- Ніяких секретів чи токенів у закомічених конфігах (усе через `.env`).
- Порти додаткових сервісів і студії в Compose прив'язані до `127.0.0.1` — назовні дивиться лише TLS-проксі.
- Нуль нових runtime-залежностей Python/JS.

## 5. Поза скоупом
- Автоматичний Terraform/Ansible плейбук під конкретного хмарного провайдера.
- Multi-node кластеризація або міграція з SQLite/DuckDB на Postgres.

## 6. Відкриті питання
- Немає блокуючих питань.

## 7. Критерії приймання
- [x] Додано `Dockerfile`, `.dockerignore`, `docker-compose.prod.yml`
- [x] Додано `deploy/systemd/daas-studio.service`, `deploy/caddy/Caddyfile`, `deploy/scripts/backup-and-upload.sh`, `deploy/README.md`
- [x] Нові офлайн-тести у `backend/tests/test_deploy_ops.py` написані перед реалізацією і проходять
- [x] `make specs-check test lint build` зелені
- [x] Спеки синхронізовано, `006` перенесено в `archive/`
