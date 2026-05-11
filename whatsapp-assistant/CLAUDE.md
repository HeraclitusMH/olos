# CLAUDE.md

## What this is

WhatsApp personal assistant backend. FastAPI + async SQLAlchemy + Postgres 16 (pgvector pinned, extension not yet enabled) + Caddy. Receives WhatsApp messages, plans tool calls via an OpenAI planner, executes calendar and reply tools.

## Architecture

- FastAPI app + health: `app/main.py` (lifespan, never `@app.on_event`)
- Settings: `app/config.py:get_settings()` — never read `os.environ` in app code
- Async DB engine/session/base: `app/database.py`
- Routers: `app/routers/{webhooks,oauth}.py`
- Services: `app/services/{whatsapp,message_processor,planner,context,cost_tracker,llm_tools,google_auth,google_calendar,tool_executor,event_resolver,disambiguation,pending_action,memory,daily_agenda}.py`
- Utils: `app/utils/{encryption,oauth_state,signature,timezone,generate_key,search,env_check,exceptions,retry,logging_config}.py`
- ORM models in `app/models/`, all re-exported from `app/models/__init__.py` so Alembic autogenerate sees them
- Migrations: `alembic/versions/0001_initial_schema.py`, `0002_daily_api_usage.py`, `0003_daily_agenda_sends.py`
- Tests: `tests/`, configured in `tests/conftest.py`
- `app/main.py` lifespan starts `daily_agenda.scheduler_loop` as a background asyncio task; shutdown sets a stop event and joins (10s timeout, then cancel).

## Rules & Patterns

- Async-only Python 3.12. No sync DB calls or sync HTTP in app code.
- `DATABASE_URL` must use `postgresql+asyncpg://`.
- Routes use `Depends(get_db)`; background work uses `AsyncSessionLocal`.
- ORM relationships are `lazy="raise"`; eager-load with `selectinload` / `joinedload`.
- All PKs are UUID with `server_default=gen_random_uuid()`. All timestamps `TIMESTAMPTZ`.
- No `ON DELETE CASCADE`; preserve audit history. `Memory.deleted_at` is the soft-delete marker. `EventReference` rows are deleted on calendar cancel (no audit needed there).
- DDL Alembic can't model (DESC/GIN indexes, triggers) goes through `op.execute(...)`.
- Tokens never stored in plaintext or logged. Use `TokenEncryption` (`app/utils/encryption.py`). OAuth `state` uses Fernet with the same `ENCRYPTION_KEY` and a 10-minute TTL — do not replace with plain HMAC / unsigned JWT.
- `ENCRYPTION_KEY` must be a Fernet key (`python -m app.utils.generate_key`).
- Planner: `model="gpt-4o-mini"`, `tool_choice="required"`. System prompt injects current datetime per call via `zoneinfo.ZoneInfo` (not `pytz`). Plain text responses → fallback `reply` tool call.
- Daily OpenAI usage counted by request count (`daily_api_usage.usage_date` PK + `request_count`); over-limit reply: `Daily limit reached. Try again tomorrow!`
- Tool execution goes through `ToolExecutor.execute()` returning `ToolResult(success, message, data)`. Calendar handlers persist `EventReference` and `Message.execution_result_json`.
- Google Calendar uses raw httpx via `app/services/google_calendar.py` (NOT google-api-python-client). 401 → refresh once via `GoogleAuthService.refresh_token` and retry; second 401 → reply with authorize link. 429 → "Calendar is busy, try again in a moment."
- `GoogleAuthService.get_valid_token` refreshes when `token_expires_at - now < 5 min`; on Google 4xx or decrypt failure marks account `status='revoked'`.
- User-facing dates formatted via `app/utils/timezone.py` (`format_event_time`, `format_date_range`), never raw ISO.
- Tests must not require Docker or a live DB. Use `ASGITransport`; fake sessions / inject service factories. Migration tests parse files via `ast.parse`, never import the migration modules.
- `pytest-asyncio` runs in `asyncio_mode = auto` — do not decorate tests with `@pytest.mark.asyncio`.
- New secret settings must be added to both `.env.example` and `app/config.py:Settings`. `.env` is git-ignored. Host-only vars consumed by ops scripts (e.g. `B2_BUCKET`) go in `.env.example` only — not in `Settings`.
- `app/main.py` calls `validate_environment()` (`app/utils/env_check.py`) at module import, after `configure_logging()` and before the FastAPI app is constructed. Missing/malformed required vars raise `SystemExit`. Tests rely on `tests/conftest.py:_TEST_ENV` being set before `app.main` is imported.
- Caddy terminates TLS; `api` container is not exposed on the host. Dockerfile installs `curl` because the prod healthcheck shells out to it.
- JSONB columns (`preferences_json`, etc.) require full reassignment to trigger SQLAlchemy dirty tracking — never mutate in place.
- WhatsApp interactive messages: max 3 buttons (use list for 4+), button titles max 20 chars.

## Disambiguation & Pending Actions

- When `calendar_update`/`calendar_cancel` match >1 event, `ToolExecutor._send_disambiguation` sends WhatsApp buttons/list and parks the original arguments + candidate events in `user.preferences_json["pending_action"]` (5-min TTL).
- `InboundMessage.interactive_id` is set for button/list replies. `message_processor._process_interactive_reply` dispatches by `pending_action.type`: calendar actions resume via `execute_pending_action`; memory actions route to `_resume_memory_update` / `_resume_memory_forget_pick` / `_process_forget_confirmation`.
- Option ids use format `disambig:<id>:<index>`. Decode with `disambiguation.parse_option_id`.
- Memory forget confirmation uses button ids `confirm_forget_<memory_id>` / `cancel_forget_<memory_id>` — these bypass the `disambig:` parsing and go directly to `_process_forget_confirmation`.
- `pending_action.set_pending_action` accepts `options: list[ResolvedEvent] | list[dict]` and an optional `extra` dict merged into the payload (used by memory_forget to park `memory_id`).
- `EventResolver.resolve_from_context` only fires for vague titles ("it", "that", etc.) — checks `execution_result_json` of recent messages for `google_event_id`. `calendar_create` now stores `title/start/end/calendar_id` in its result data to enable this.

## Memory System

- `MemoryService` (`app/services/memory.py`): `store`, `retrieve`, `update_content`, `forget` (soft-delete only — never hard-delete), `get_by_id`.
- Retrieval strategy: `plainto_tsquery('english', cleaned)` on `search_vector` (ts_rank ordered) + tag `&&` overlap, combined+deduped. ILIKE fallback on content only when both return empty.
- `app/utils/search.py`: `build_search_query` strips stopwords/single-char tokens before tsquery. `generate_tags` extracts simple tokens for update without an LLM round-trip.
- `memory_forget` sends confirmation buttons first; actual soft-delete only on `confirm_forget_<id>` reply.

## Daily Agenda Scheduler

- Per-user prefs live at `user.preferences_json["daily_agenda"] = {enabled, timezone, time_local}`. Defaults from `Settings`: `Asia/Makassar` / `08:00`; tick 60s; send window 5 min. JSONB reassignment rule applies — use `daily_agenda.set_user_prefs`.
- `DailyAgendaService.run_tick(now_utc)`: loads enabled users under a Postgres advisory lock (`pg_try_advisory_lock`), then per user computes due-window in their `ZoneInfo`, fetches Google Calendar events for `[day_start_local, day_end_local)`, sends WhatsApp, writes a `DailyAgendaSend(user_id, agenda_date)` row. Per-user failures are caught — one user must never break the batch.
- Idempotency: `daily_agenda_sends` has `UNIQUE(user_id, agenda_date)`. The DB constraint — not the advisory lock — is the source of truth, so multi-replica and restarts are safe.
- Revoked Google account (`TokenExpiredError`) or transient `GoogleAuthError` → skip the user silently for this tick, do not spam them.
- Bad `timezone` / `time_local` fall back to defaults and log a warning (no secrets).
- Agenda message header built explicitly (no `%-d` — non-portable on Windows). Time range uses an en-dash (`09:30–10:15`).

## Error Handling & Hardening

- Custom exception hierarchy lives in `app/utils/exceptions.py`: `AssistantError` base + `OpenAIError`, `GoogleCalendarError`, `TokenExpiredError`, `DailyLimitExceededError`, `WhatsAppSendError`. Each carries `user_message` (WhatsApp-safe) and `log_message`.
- `app/utils/retry.py` — `async_retry(max_retries, delay, backoff, exceptions, jitter)`. Skips 4xx (except 429). Used by planner / calendar / whatsapp.
- `app/utils/logging_config.py` — JSON structured logging; redacts tokens / Bearer / secrets. `configure_logging()` runs at import time from `app/main.py`.
- Planner: handles OpenAI 429 by reading `Retry-After` and retrying ONCE, then raises `OpenAIError`. Transient OpenAI errors raise `OpenAIError` (no fallback reply). Malformed responses still fall back to a `reply` tool call.
- Google Calendar: 5xx subclassed as `GoogleCalendarServerError` and retried once via `_request`. 403 with `rateLimitExceeded` mapped to `GoogleCalendarRateLimited`. Service-level `GoogleCalendarError` now inherits from the assistant-level one in `app/utils/exceptions.py`.
- WhatsApp: 5xx retried via internal `_WhatsAppServerError`; 429 logged + raised (no retry); `WhatsAppAPIError` is now a subclass of `WhatsAppSendError`.
- `GoogleAuthService.get_valid_token` contract: returns `None` only when no account exists; raises `TokenExpiredError` on revoked/invalid_grant/decryption failure; raises `GoogleAuthError` on transient (network) failure.
- `ToolExecutor.execute` re-raises `AssistantError` so message_processor maps it; other exceptions still become a generic `ToolResult` failure.
- `process_inbound_message` wraps work in `asyncio.wait_for(..., timeout=PROCESSING_TIMEOUT_SECONDS=25)` and a comprehensive try/except. On timeout or unhandled failure, `_finalize_failure` opens a fresh session to mark the row processed + send a fallback reply — the golden rule is no message is silently dropped.
- Startup: `warn_about_unprocessed_messages()` logs (does NOT auto-retry) inbound rows older than 5 min that never got `processed=True`.
- `/health` returns `{status: healthy|degraded|unhealthy, checks: {database, last_message_processed}}`. DB probe = `SELECT 1` + latency. Pipeline degraded when last processed > 5 min AND unprocessed rows exist.

## Deployment & Ops

- Prod stack: `docker compose -f docker-compose.yml -f docker-compose.prod.yml`. Override adds `restart: always`, json-file log rotation (10m × 3), api memory cap 512M, api healthcheck (`curl /health`), db healthcheck (`pg_isready`), db `shm_size: 128mb`. DB is internal-only in both compose files.
- Entry points: `./deploy.sh` (build + migrate + up + poll /health), `Makefile` (`dev`, `prod`, `migrate`, `migrate-create msg=…`, `backup`, `restore file=…`, `logs`, `shell`, `db-shell`, `health`, `clean`).
- Ops scripts in `scripts/`: `backup.sh` (pg_dump → gzip → rclone B2, prune local >7d), `restore.sh` (drops + recreates DB, restores, verifies /health), `dev-tunnel.sh` (Cloudflare Tunnel), `vps-setup.sh` (one-time Ubuntu bootstrap: Docker, UFW 22/80/443, rclone, `/opt/whatsapp-assistant`).
- Backups: rclone remote named `b2`, bucket from `$B2_BUCKET`, daily 3 AM cron. Step-by-step prod runbook in `DEPLOYMENT.md`.

## Current State

- 283 unit tests pass with `python -m pytest -q`.
- All planner tools implemented: `reply`, `ask_clarification`, `calendar_create`, `calendar_query`, `calendar_update`, `calendar_cancel`, `memory_store`, `memory_retrieve`, `memory_update`, `memory_forget`.
- Daily-agenda scheduler runs in-process from the lifespan; sends are gated by the `daily_agenda_sends` unique constraint.
- Interactive webhook replies (button_reply, list_reply) are parsed and routed through the disambiguation resume flow.
- Google OAuth flow wired end-to-end (`/authorize` → consent → `/callback` → encrypted token storage).
- `pgvector==0.2.5` pinned but `vector` extension not yet enabled — the first migration adding an embedding column must run `CREATE EXTENSION IF NOT EXISTS vector`.
- The checked-in `.venv` launcher points at a missing Python path; run tests with system Python after installing `requirements.txt`.
