#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

PROJECT_DIR="/home/app/projects/project"
BACKUP_DIR="/home/app/backups/db"
LOG_FILE="/home/app/backups/backup.log"
CONTAINER="project-postgres-1"
DB_NAME="irandoc_news"
DB_USER="postgres"
KEEP=8

mkdir -p "$BACKUP_DIR"

set -a; source "$PROJECT_DIR/.env"; set +a

if [[ -z "${POSTGRES_PASSWORD:-}" ]]; then
  echo "[$(date '+%F %T')] ERROR: POSTGRES_PASSWORD not set" | tee -a "$LOG_FILE"
  exit 1
fi

TS="$(date '+%Y-%m-%d_%H-%M-%S')"
OUT="$BACKUP_DIR/${DB_NAME}_${TS}.sql.gz"

echo "[$(date '+%F %T')] start backup -> $OUT" | tee -a "$LOG_FILE"

if ! docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$CONTAINER" \
       pg_dump -E UTF8 -U "$DB_USER" -d "$DB_NAME" | gzip -9 > "$OUT"; then
  echo "[$(date '+%F %T')] ERROR: dump failed" | tee -a "$LOG_FILE"
  rm -f "$OUT"
  exit 1
fi

if [[ ! -s "$OUT" ]] || ! gzip -t "$OUT" 2>/dev/null; then
  echo "[$(date '+%F %T')] ERROR: invalid backup file" | tee -a "$LOG_FILE"
  rm -f "$OUT"
  exit 1
fi

SIZE=$(du -h "$OUT" | cut -f1)
echo "[$(date '+%F %T')] OK: $OUT ($SIZE)" | tee -a "$LOG_FILE"

mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/*.sql.gz 2>/dev/null | tail -n +$((KEEP+1)))
for f in "${OLD[@]:-}"; do
  [[ -n "$f" ]] && { echo "[$(date '+%F %T')] rotate out: $f" | tee -a "$LOG_FILE"; rm -f "$f"; }
done
