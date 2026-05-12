#!/usr/bin/env bash
# Production deploy script.
#
# Usage (from the project root, on the VPS):
#   ./deploy.sh
#
# What it does:
#   1. Verifies .env exists and has the required vars.
#   2. Builds the api image.
#   3. Runs Alembic migrations inside the running api container.
#   4. Brings the stack up with the production override.
#   5. Polls /health for up to 30 seconds.
#   6. Prints the final compose status.
#
# Rollback instructions:
#   If a deploy goes bad, the previous image is still available locally as a
#   dangling image (or as the previously-tagged ":latest" before the new build
#   overwrote it). To roll back:
#
#     # 1. Identify the previous image id
#     docker image ls whatsapp-assistant-api
#
#     # 2. Re-tag the previous good image and restart
#     docker tag <previous-image-id> whatsapp-assistant-api:latest
#     docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api
#
#   If a migration is the culprit, downgrade BEFORE rolling back the code,
#   since the old code likely can't talk to the new schema:
#     docker compose exec api alembic downgrade -1
#
#   Always verify by curl'ing /health after rollback.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

REQUIRED_VARS=(
  DATABASE_URL
  OPENAI_API_KEY
  WHATSAPP_VERIFY_TOKEN
  WHATSAPP_ACCESS_TOKEN
  WHATSAPP_PHONE_NUMBER_ID
  META_APP_SECRET
  ENCRYPTION_KEY
  POSTGRES_USER
  POSTGRES_PASSWORD
  POSTGRES_DB
)

log()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

# 1. Pre-deploy check
log "Checking .env"
[ -f .env ] || fail ".env not found in $PROJECT_DIR — create it before deploying."

# shellcheck disable=SC1091
set -a; . ./.env; set +a

missing=()
for var in "${REQUIRED_VARS[@]}"; do
  if [ -z "${!var:-}" ]; then
    missing+=("$var")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  fail "Missing required env vars: ${missing[*]}"
fi

# 2. Build
log "Building images"
$COMPOSE build

# 3. Bring stack up first so api container exists for `exec`
log "Starting stack (db + api + caddy)"
$COMPOSE up -d

# Wait briefly for db to accept connections before migrating
log "Waiting for db to be ready"
for _ in $(seq 1 30); do
  if $COMPOSE exec -T db pg_isready -U "${POSTGRES_USER}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 4. Migrate
log "Running Alembic migrations"
$COMPOSE exec -T api alembic upgrade head

# 5. Wait for health check
log "Polling /health (up to 30s)"
healthy=0
for _ in $(seq 1 30); do
  # Hit /health from inside the api container so we don't rely on host networking.
  if $COMPOSE exec -T api curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

# 6. Status
log "Compose status:"
$COMPOSE ps

if [ "$healthy" -ne 1 ]; then
  warn "Health check did not pass within 30s."
  warn "Recent api logs:"
  $COMPOSE logs --tail=80 api >&2 || true
  fail "Deploy finished but health is not green — see logs and consider rolling back (see comments in deploy.sh)."
fi

log "Deploy complete and /health is green."
