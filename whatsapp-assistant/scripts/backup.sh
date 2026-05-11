#!/usr/bin/env bash
# Daily PostgreSQL backup.
#
# Pipeline:
#   pg_dump (inside db container) | gzip > backup_YYYYMMDD_HHMMSS.sql.gz
#   then rclone copy to B2 and prune local files older than 7 days.
#
# Cron (root crontab on the VPS):
#   0 3 * * * /opt/olos/whatsapp-assistant/scripts/backup.sh >> /var/log/whatsapp-assistant-backup.log 2>&1
#
# Prereqs:
#   - rclone configured with a remote called "b2" (see `rclone config`)
#   - $B2_BUCKET set in .env (e.g. "whatsapp-assistant-backups")
#   - db container reachable via `docker compose exec db`

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
RCLONE_REMOTE="${RCLONE_REMOTE:-b2}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

mkdir -p "$BACKUP_DIR"

# shellcheck disable=SC1091
set -a; . ./.env; set +a

: "${POSTGRES_USER:?POSTGRES_USER not set in .env}"
: "${POSTGRES_DB:?POSTGRES_DB not set in .env}"
: "${B2_BUCKET:?B2_BUCKET not set in .env}"

TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
FILENAME="backup_${TIMESTAMP}.sql.gz"
LOCAL_PATH="$BACKUP_DIR/$FILENAME"
REMOTE_PATH="${RCLONE_REMOTE}:${B2_BUCKET}/${FILENAME}"

log()  { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
fail() { printf '[%s] ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; exit 1; }

trap 'fail "backup failed for $FILENAME"' ERR

log "Starting backup → $FILENAME"

# 1. pg_dump inside the db container, piped through gzip on the host.
docker compose exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" \
  | gzip -9 > "$LOCAL_PATH"

SIZE="$(stat -c '%s' "$LOCAL_PATH" 2>/dev/null || stat -f '%z' "$LOCAL_PATH")"
[ "$SIZE" -gt 0 ] || fail "dump is empty"

log "Local dump written: $LOCAL_PATH (${SIZE} bytes)"

# 2. Upload to B2 via rclone
log "Uploading to $REMOTE_PATH"
rclone copyto "$LOCAL_PATH" "$REMOTE_PATH"

# 3. Prune local backups older than RETENTION_DAYS
log "Pruning local backups older than ${RETENTION_DAYS} days"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'backup_*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete || true

log "Backup OK: $FILENAME"
