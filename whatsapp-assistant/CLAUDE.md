# CLAUDE.md

## Project Overview

WhatsApp personal assistant backend. FastAPI + async SQLAlchemy + Postgres 16 (with pgvector pinned for future embeddings) + Caddy reverse proxy, orchestrated via Docker Compose. It receives WhatsApp messages, stores them, uses an OpenAI planner to choose assistant tool calls, and currently implements only conversational replies/clarifications while calendar and memory execution remain pending.

## Architecture & Key Decisions

- Async-only Python 3.12 service. No sync DB calls or sync HTTP in app code.
- `DATABASE_URL` must use `postgresql+asyncpg://`.
- Read config through `app/config.py:get_settings()`, never `os.environ` directly in app code.
- Routes use `Depends(get_db)` for DB sessions. Background message processing uses `AsyncSessionLocal`.
- ORM models must be re-exported from `app/models/__init__.py` so Alembic autogenerate sees them.
- ORM relationships use `lazy="raise"`; eager-load explicitly with `selectinload` or `joinedload`.
- Use FastAPI `lifespan` for startup/shutdown; do not add `@app.on_event`.
- DDL that Alembic cannot model cleanly, such as DESC indexes, GIN indexes, and triggers, goes through `op.execute(...)`.
- Tests must not require Docker or a live DB. Use `ASGITransport` for HTTP and fake/overridden sessions where needed. Migration tests inspect files via `ast.parse` instead of importing migration modules.
- `pytest-asyncio` runs in `asyncio_mode = auto`; do not decorate tests with `@pytest.mark.asyncio`.
- Tokens are never stored in plaintext and never logged. Use `TokenEncryption` from `app/utils/encryption.py`.
- OpenAI planner calls must use `model="gpt-4o-mini"` and `tool_choice="required"`.
- The planner system prompt injects current datetime freshly per call using `zoneinfo.ZoneInfo`, not `pytz`.
- The LLM must always return tool calls. Plain assistant text is handled as an invalid planner response and converted to a short `reply` fallback.
- Daily OpenAI API usage is counted by request count, not token cost.

## Where To Find Things

- FastAPI app and health endpoint -> `app/main.py`
- Runtime settings -> `app/config.py`
- Async DB engine/session/base -> `app/database.py`
- WhatsApp webhook routes -> `app/routers/webhooks.py`
- Google OAuth routes -> `app/routers/oauth.py`
- WhatsApp Graph API client -> `app/services/whatsapp.py`
- Inbound message orchestration -> `app/services/message_processor.py`
- OpenAI tool schema and system prompt -> `app/services/llm_tools.py`
- OpenAI planner service -> `app/services/planner.py`
- Conversation history builder -> `app/services/context.py`
- Daily OpenAI request counter -> `app/services/cost_tracker.py`
- Google token lifecycle -> `app/services/google_auth.py`
- Fernet token encryption -> `app/utils/encryption.py`
- OAuth state codec -> `app/utils/oauth_state.py`
- Webhook signature validation -> `app/utils/signature.py`
- ORM models -> `app/models/`
- Initial schema migration -> `alembic/versions/0001_initial_schema.py`
- Daily API counter migration -> `alembic/versions/0002_daily_api_usage.py`
- Test configuration -> `tests/conftest.py`

## Current State & Known Issues

- 132 unit tests pass with `python -m pytest -q`.
- WhatsApp text messages are deduplicated by `wa_message_id`, stored inbound, processed, and followed by an outbound DB row.
- `Message.tool_calls_json` logs the full planner tool-call list as `{"tool_calls": [...]}`.
- `reply` and `ask_clarification` are the only executed planner tools.
- `calendar_*` and `memory_*` tools currently respond with `Tool {name} not yet implemented.`
- Conversation context loads the latest non-null user messages, maps inbound to `user` and outbound to `assistant`, and sends them oldest-first.
- Daily OpenAI call limits use `daily_api_usage.usage_date` as the primary key and `request_count`; exceeded limit response is `Daily limit reached. Try again tomorrow!`
- Google OAuth flow is wired end-to-end: `/authorize` to Google consent to `/callback` to encrypted token storage.
- `GoogleAuthService` is implemented, but actual calendar read/write handlers are not built.
- Memory storage/retrieval/update/delete handlers are not built.
- Non-text inbound messages are silently acknowledged. Status webhooks are dropped.
- `pgvector==0.2.5` is pinned but the `vector` extension is not yet enabled. The first migration adding an embedding column must run `CREATE EXTENSION IF NOT EXISTS vector`.
- All PKs are UUID with `server_default=gen_random_uuid()`. All timestamps are `TIMESTAMPTZ`.
- No `ON DELETE CASCADE`; preserve audit history. `Memory.deleted_at` is the soft-delete marker.
- `ENCRYPTION_KEY` must be a Fernet key. Generate with `python -m app.utils.generate_key`.
- OAuth state uses Fernet with the same `ENCRYPTION_KEY` and a 10-minute TTL; do not replace it with plain HMAC or unsigned JWT.
- `GoogleAuthService.get_valid_token` refreshes when `token_expires_at - now < 5 min`; on Google 4xx or decrypt failure it marks the account `status='revoked'`.
- Caddy terminates TLS; the `api` container is not exposed on the host.
- `.env` is git-ignored. `.env.example` is the source of truth. New secret settings must be added to both `.env.example` and `app/config.py:Settings`.
- Local note: the checked-in `.venv` launcher currently points at a missing Python path; tests were run with the system Python after installing `requirements.txt`.

## Session Log

- [2026-05-10] Implemented OpenAI planner integration: added tool schemas, fresh datetime system prompt, `Planner.plan()`, conversation context builder, daily API usage counter, and wired `message_processor.py` to log tool calls and execute only `reply`/`ask_clarification`.
- [2026-05-10] Added `DailyApiUsage` model and `0002_daily_api_usage` Alembic migration. Updated tests for planner tools, context mapping, response extraction, message processor tool responses, and the new model/migration.
