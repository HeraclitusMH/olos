# CLAUDE.md

## What this is

WhatsApp personal assistant backend. FastAPI + async SQLAlchemy + Postgres 16 (pgvector pinned, extension not yet enabled) + Caddy. Receives WhatsApp messages, plans tool calls via an OpenAI planner, executes calendar and reply tools.

## Architecture

- FastAPI app + health: `app/main.py` (lifespan, never `@app.on_event`)
- Settings: `app/config.py:get_settings()` — never read `os.environ` in app code
- Async DB engine/session/base: `app/database.py`
- Routers: `app/routers/{webhooks,oauth}.py`
- Services: `app/services/{whatsapp,message_processor,planner,context,cost_tracker,llm_tools,google_auth,google_calendar,tool_executor,event_resolver,disambiguation,pending_action,memory}.py`
- Utils: `app/utils/{encryption,oauth_state,signature,timezone,generate_key,search}.py`
- ORM models in `app/models/`, all re-exported from `app/models/__init__.py` so Alembic autogenerate sees them
- Migrations: `alembic/versions/0001_initial_schema.py`, `0002_daily_api_usage.py`
- Tests: `tests/`, configured in `tests/conftest.py`

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
- New secret settings must be added to both `.env.example` and `app/config.py:Settings`. `.env` is git-ignored.
- Caddy terminates TLS; `api` container is not exposed on the host.
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

## Current State

- 235 unit tests pass with `python -m pytest -q`.
- All planner tools implemented: `reply`, `ask_clarification`, `calendar_create`, `calendar_query`, `calendar_update`, `calendar_cancel`, `memory_store`, `memory_retrieve`, `memory_update`, `memory_forget`.
- Interactive webhook replies (button_reply, list_reply) are parsed and routed through the disambiguation resume flow.
- Google OAuth flow wired end-to-end (`/authorize` → consent → `/callback` → encrypted token storage).
- `pgvector==0.2.5` pinned but `vector` extension not yet enabled — the first migration adding an embedding column must run `CREATE EXTENSION IF NOT EXISTS vector`.
- The checked-in `.venv` launcher points at a missing Python path; run tests with system Python after installing `requirements.txt`.
