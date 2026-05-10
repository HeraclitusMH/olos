# CLAUDE.md

## What this is

WhatsApp personal assistant backend. FastAPI + async SQLAlchemy + Postgres 16 (with pgvector) + Caddy reverse proxy, orchestrated via Docker Compose.

## Architecture

- `app/main.py` — FastAPI app, `lifespan` startup hook, `GET /health`.
- `app/config.py` — `Settings` (pydantic-settings) + `get_settings()` (`@lru_cache`). All env reads go through here.
- `app/database.py` — async engine, `AsyncSessionLocal`, `get_db()` FastAPI dependency, `Base = DeclarativeBase`.
- `app/routers/webhooks.py` — `GET/POST /webhooks/whatsapp`. GET echoes Meta's `hub.challenge` as plain text on token match (403 otherwise). POST verifies `X-Hub-Signature-256`, parses the envelope, and fires `asyncio.create_task(process_inbound_message(...))` per text message. Always returns 200 (even on bad signature/JSON) so Meta does not retry.
- `app/services/whatsapp.py` — `WhatsAppClient` (text + interactive button + interactive list senders) on Graph API v19.0 via `httpx.AsyncClient`. Raises `WhatsAppAPIError` on non-2xx.
- `app/services/message_processor.py` — `process_inbound_message(InboundMessage)`: opens its own `AsyncSessionLocal`, dedupes on `wa_message_id`, upserts `User`, stores inbound `Message`, sends `"Echo: {text}"` via `WhatsAppClient`, stores outbound `Message` (with `error` field on send failure), marks inbound `processed=True`, single commit.
- `app/utils/signature.py` — `validate_webhook_signature(payload, header, secret)` (HMAC-SHA256, constant-time compare, expects `sha256=<hex>` prefix).
- `app/models/` — `User`, `Message`, `GoogleAccount`, `EventReference`, `Memory`, all re-exported from `app/models/__init__.py`.
- `alembic/env.py` — async migration runner; pulls URL from `get_settings()`, imports `app.models` to register metadata.
- `alembic/versions/0001_initial_schema.py` — baseline: 5 tables, GIN indexes on `memories.search_vector`/`memories.tags`, composite indexes on `messages`, and `trg_memories_search_vector` (BEFORE INSERT/UPDATE) which auto-populates `search_vector` from `content || tags` via `to_tsvector('english', ...)`.
- `tests/conftest.py` — sets test env vars **before** any app import; exposes async `client` fixture via `httpx.ASGITransport`.
- `docker-compose.yml` — services `api` (built from `Dockerfile`), `db` (postgres:16), `caddy` (ports 80/443). Named volumes: `pgdata`, `caddy_data`.
- `Caddyfile` — reverse proxy → `api:8000`, 10 MB body limit, auto-HTTPS. Domain placeholder `yourdomain.com` must be replaced before deploy.

## Rules & Patterns

- **Async only**: no sync DB calls, no sync HTTP. `DATABASE_URL` must use `postgresql+asyncpg://`.
- Type hints on every function; PEP 8 strict; Python 3.12 syntax (`X | Y`, `list[T]`, `from datetime import UTC`) is fine.
- Read config via `get_settings()`, never `os.environ` directly in app code.
- Inject DB sessions with `Depends(get_db)`; never instantiate `AsyncSessionLocal` inline in routes.
- New ORM models must be imported from `app/models/__init__.py` so Alembic autogenerate sees them.
- ORM relationships use `lazy="raise"` — eager-load explicitly with `selectinload`/`joinedload`. The default `lazy="select"` triggers `MissingGreenlet` in async contexts.
- Use FastAPI `lifespan` context manager for startup/shutdown — `@app.on_event` is forbidden (deprecated).
- DDL that Alembic can't model cleanly (DESC composite indexes, GIN, triggers, PL/pgSQL functions) goes through `op.execute(...)`; autogenerate will silently miss it.
- Tests must not require Docker or a live DB for unit-level coverage. Use `ASGITransport` against `app.main.app` for HTTP, and `inspect()` / `__table__` introspection for schema.
- Don't import migration files directly in tests — `from alembic import op` only resolves inside an Alembic runtime context. Inspect them via `ast.parse` instead.

## Decisions & Constraints

- `pgvector==0.2.5` is pinned but the `vector` extension is **not yet enabled**. The first migration that adds an embedding column must run `CREATE EXTENSION IF NOT EXISTS vector`.
- All PKs are `UUID` with `server_default=gen_random_uuid()` (Postgres 13+ built-in, no `pgcrypto` needed). All timestamps are `TIMESTAMPTZ`.
- No `ON DELETE CASCADE` anywhere — preserve the audit trail. `Memory.deleted_at` is the soft-delete marker.
- OAuth tokens (`google_accounts.access_token_enc`, `refresh_token_enc`) are `LargeBinary` — stored Fernet-encrypted with `Settings.encryption_key`.
- Caddy terminates TLS; the `api` container is **not** exposed on the host — only Caddy publishes 80/443.
- `.env` is git-ignored; `.env.example` is the source of truth for required keys. Adding a new secret means updating both `.env.example` and `app/config.py:Settings`.
- `pytest-asyncio` runs in `asyncio_mode = auto` (see `pytest.ini`) — do not decorate tests with `@pytest.mark.asyncio`.

## Current State

- 80 unit tests passing (`pytest -q`). No CI configured.
- WhatsApp webhook is wired end-to-end: signature verification → parse → background task → DB writes → echo. Echo replies are still hardcoded `"Echo: {text}"`; no LLM/intent routing yet.
- Non-text inbound messages (image/voice/etc.) are silently acknowledged but not processed. Status webhooks are dropped.
- Schema baseline (`0001_initial_schema`) is the only revision; no embedding column / pgvector activation yet.
