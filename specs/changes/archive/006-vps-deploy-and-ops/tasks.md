# 006 — Задачі: Деплой на VPS і автоматизований щоденний бекап

> Агент відмічає `[x]` одразу після виконання кожної задачі.

## Фаза 0 — Підготовка
- [x] T001 Перечитати `spec.md`, `plan.md`, `specs/capabilities/12-storage-cli-ops.md`, `backend/app/main.py`, `backend/app/cli.py`

## Фаза 1 — Тести (мають падати)
- [x] T010 [P] Написати `backend/tests/test_deploy_ops.py` для REQ-OPS-060 і REQ-OPS-061 (перевірка `Dockerfile`, `.dockerignore`, `docker-compose.prod.yml`, `deploy/*`, `bash -n`, та `Host`-гарду за reverse proxy)

## Фаза 2 — Реалізація
- [x] T020 [P] Створити `.dockerignore`, `Dockerfile` і `docker-compose.prod.yml` (REQ-OPS-060)
- [x] T021 [P] Створити `deploy/systemd/daas-studio.service`, `deploy/caddy/Caddyfile`, `deploy/scripts/backup-and-upload.sh`, `deploy/README.md` (REQ-OPS-061)

## Фаза 3 — Перевірка
- [x] T030 `make specs-check`, `make test`, `make lint`, `make build` зелені

## Фаза 4 — Синхронізація
- [x] T040 Оновити `specs/capabilities/12-storage-cli-ops.md` і `specs/capabilities/11-api-and-security.md` (посилання на тести + нові REQ-OPS-060/061)
- [x] T041 Оновити `docs/03_IMPLEMENTATION_PLAN.md`, `docs/05_FIRST_PRODUCT_LAUNCH_PLAN.md`, `specs/changes/README.md` та архівувати `specs/changes/006-vps-deploy-and-ops/` → `specs/changes/archive/`
