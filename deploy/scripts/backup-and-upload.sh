#!/usr/bin/env bash
# Щоденний безпечний бекап DaaS Intelligence Studio (+ опційне вивантаження в S3/R2/B2).
# Дотримується правила одного writer'а DuckDB:
#   - якщо студія запущена (/api/health відповідає 200) -> викликає POST /api/maintenance/backup
#   - якщо студія зупинена -> викликає CLI `daas backup`

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STORAGE_DIR="${DAAS_STORAGE_DIR:-$APP_DIR/storage}"
BACKUP_DIR="${BACKUP_DIR:-$STORAGE_DIR/backups}"
DAAS_URL="${DAAS_URL:-http://127.0.0.1:8000}"
INCLUDE_CACHE="${INCLUDE_CACHE:-false}"
KEEP_DAYS="${KEEP_DAYS:-14}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"

if [[ -f "$APP_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env"
  set +a
fi

mkdir -p "$BACKUP_DIR"

read_token() {
  if [[ -n "${DAAS_API_TOKEN:-}" ]]; then
    printf '%s' "$DAAS_API_TOKEN"
    return 0
  fi
  if [[ -f "$STORAGE_DIR/runtime.json" ]]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("api_token", ""))' "$STORAGE_DIR/runtime.json"
    return 0
  fi
  return 1
}

BACKUP_FILE=""

if curl -fsS --max-time 3 "$DAAS_URL/api/health" >/dev/null 2>&1; then
  TOKEN="$(read_token || true)"
  if [[ -z "$TOKEN" ]]; then
    echo "Помилка: студія запущена, але не знайдено DAAS_API_TOKEN або $STORAGE_DIR/runtime.json" >&2
    exit 1
  fi
  RESP="$(curl -fsS --max-time 120 -X POST \
    -H "Authorization: Bearer $TOKEN" \
    "$DAAS_URL/api/maintenance/backup?include_cache=$INCLUDE_CACHE")"
  FILE_NAME="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["file"])' "$RESP")"
  BACKUP_FILE="$BACKUP_DIR/$FILE_NAME"
  echo "OK (API backup): $BACKUP_FILE"
else
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  BACKUP_FILE="$BACKUP_DIR/daas-backup-$STAMP.zip"
  CACHE_FLAG="--no-cache"
  if [[ "$INCLUDE_CACHE" == "true" ]]; then
    CACHE_FLAG="--cache"
  fi
  cd "$APP_DIR/backend"
  uv run daas backup --out "$BACKUP_FILE" "$CACHE_FLAG"
  echo "OK (CLI backup): $BACKUP_FILE"
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "Помилка: файл бекапу не знайдено: $BACKUP_FILE" >&2
  exit 1
fi

if [[ -n "$BACKUP_REMOTE" ]]; then
  if [[ "$BACKUP_REMOTE" == s3://* ]]; then
    aws s3 cp "$BACKUP_FILE" "${BACKUP_REMOTE%/}/$(basename "$BACKUP_FILE")"
  else
    rclone copy "$BACKUP_FILE" "$BACKUP_REMOTE"
  fi
  echo "OK (uploaded): $BACKUP_REMOTE"
fi

if [[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] && (( KEEP_DAYS > 0 )); then
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'daas-backup-*.zip' -mtime +"$KEEP_DAYS" -delete
fi
