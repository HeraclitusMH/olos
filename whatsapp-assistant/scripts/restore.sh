#!/usr/bin/env bash
# Restore the database from a gzipped pg_dump.
#
# Usage:
#   scripts/restore.sh backup_20260511_030000.sql.gz
#
# If the file is not in ./backups, it will be downloaded from B2.
# DESTRUCTIVE: drops and recreates the target database.

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <backup_filename.sql.gz>" >&2
  exit 1
fi

FILENAME="$1"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
RCLONE_REMOTE="${RCLONE_REMOTE:-b2}"

mkdir -p "$BACKUP_DIR"

# shellcheck disable=SC1091
set -a; . ./.env; set +a

: "${POSTGRES_USER:?POSTGRES_USER not set in .env}"
: "${POSTGRES_DB:?POSTGRES_DB not set in .env}"
: "${B2_BUCKET:?B2_BUCKET not set in .env}"

LOCAL_PATH="$BACKUP_DIR/$FILENAME"

log()  { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
fail() { printf '[%s] ERROR: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; exit 1; }

# 1. Download from B2 if not local
if [ ! -f "$LOCAL_PATH" ]; then
  log "Local copy missing — downloading ${FILENAME} from ${RCLONE_REMOTE}:${B2_BUCKET}"
  rclone copyto "${RCLONE_REMOTE}:${B2_BUCKET}/${FILENAME}" "$LOCAL_PATH"
fi
[ -s "$LOCAL_PATH" ] || fail "Backup file is empty: $LOCAL_PATH"

cat <<EOF
About to restore:
  source : $LOCAL_PATH
  target : database "$POSTGRES_DB" as user "$POSTGRES_USER"
This will STOP the api container and DROP the existing database.
EOF
read -r -p "Type 'yes' to continue: " confirm
[ "$confirm" = "yes" ] || fail "aborted"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
# Fall back to dev compose if the prod file is missing locally.
if [ ! -f docker-compose.prod.yml ]; then
  COMPOSE="docker compose"
fi

# 2. Stop api container so it stops opening connections to the db
log "Stopping api"
$COMPOSE stop api

# Make sure db is up
log "Ensuring db is up"
$COMPOSE up -d db
for _ in $(seq 1 30); do
  if $COMPOSE exec -T db pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 3. Drop and recreate database (connect via the maintenance "postgres" db)
log "Dropping and recreating database $POSTGRES_DB"
$COMPOSE exec -T db psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname = '${POSTGRES_DB}' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS "${POSTGRES_DB}";
CREATE DATABASE "${POSTGRES_DB}" OWNER "${POSTGRES_USER}";
SQL

# 4. Restore from dump
log "Restoring from $FILENAME"
gunzip -c "$LOCAL_PATH" | $COMPOSE exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1

# 5. Restart api
log "Restarting api"
$COMPOSE up -d api

# 6. Verify health
log "Waiting for /health"
healthy=0
for _ in $(seq 1 30); do
  if $COMPOSE exec -T api curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

[ "$healthy" -eq 1 ] || fail "/health did not come back green after restore"
log "Restore complete and /health is green."
