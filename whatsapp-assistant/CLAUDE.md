# CLAUDE.md

## What this is

WhatsApp personal assistant backend. FastAPI + async SQLAlchemy + Postgres 16 (with pgvector) + Caddy reverse proxy, orchestrated via Docker Compose.

## Architecture

- `app/main.py` — FastAPI app, `lifespan` startup hook, `GET /health`. Includes `webhooks` and `oauth` routers.
- `app/config.py` — `Settings` (pydantic-settings) + `get_settings()` (`@lru_cache`). All env reads go through here.
- `app/database.py` — async engine, `AsyncSessionLocal`, `get_db()` FastAPI dependency, `Base = DeclarativeBase`.
- `app/routers/webhooks.py` — `GET/POST /webhooks/whatsapp`. POST verifies `X-Hub-Signature-256`, fires `asyncio.create_task(process_inbound_message(...))` per text message. Always returns 200.
- `app/routers/oauth.py` — `GET /oauth/google/authorize?wa_id=…` returns Google consent URL; `GET /oauth/google/callback` validates state, exchanges code, stores encrypted tokens.
- `app/services/whatsapp.py` — `WhatsAppClient` (text + interactive button + interactive list senders) on Graph API v19.0 via `httpx.AsyncClient`. Raises `WhatsAppAPIError` on non-2xx.
- `app/services/message_processor.py` — `process_inbound_message(InboundMessage)`: dedupes, upserts `User`, stores messages, builds reply via `_build_reply()`, stores outbound. Calendar-keyword messages from unauthorized users receive a Google auth URL instead of echo.
- `app/services/google_auth.py` — `GoogleAuthService` (session-scoped): `get_valid_token`, `refresh_token`, `revoke_token`, `is_authorized`. `build_authorization_url(wa_id)` builds the Google consent URL with an encrypted state.
- `app/utils/encryption.py` — `TokenEncryption(key)`: Fernet wrap; raises `TokenEncryptionError` on bad key or corrupted ciphertext (never logs token values).
- `app/utils/oauth_state.py` — `OAuthStateCodec(key, ttl_seconds=600)`: Fernet-encrypts `{"wa_id": …}` as the OAuth `state` param; decode validates HMAC + TTL (CSRF + replay protection).
- `app/utils/generate_key.py` — run with `python -m app.utils.generate_key` to print a fresh Fernet key for `.env`.
- `app/utils/signature.py` — `validate_webhook_signature(payload, header, secret)` (HMAC-SHA256, constant-time compare).
- `app/models/` — `User`, `Message`, `GoogleAccount`, `EventReference`, `Memory`, re-exported from `app/models/__init__.py`.
- `alembic/versions/0001_initial_schema.py` — baseline: 5 tables, GIN indexes, `trg_memories_search_vector` trigger.
- `tests/conftest.py` — generates a real Fernet key as `ENCRYPTION_KEY` at session start (not a placeholder string); exposes async `client` fixture via `httpx.ASGITransport`.

## Rules & Patterns

- **Async only**: no sync DB calls, no sync HTTP. `DATABASE_URL` must use `postgresql+asyncpg://`.
- Type hints on every function; PEP 8 strict; Python 3.12 syntax (`X | Y`, `list[T]`, `from datetime import UTC`) is fine.
- Read config via `get_settings()`, never `os.environ` directly in app code.
- Inject DB sessions with `Depends(get_db)`; never instantiate `AsyncSessionLocal` inline in routes.
- New ORM models must be imported from `app/models/__init__.py` so Alembic autogenerate sees them.
- ORM relationships use `lazy="raise"` — eager-load explicitly with `selectinload`/`joinedload`.
- Use FastAPI `lifespan` context manager for startup/shutdown — `@app.on_event` is forbidden.
- DDL that Alembic can't model cleanly (DESC composite indexes, GIN, triggers) goes through `op.execute(...)`.
- Tests must not require Docker or a live DB. Use `ASGITransport` for HTTP; override `get_db` via `app.dependency_overrides` to inject fake sessions. Don't import migration files — inspect via `ast.parse`.
- `pytest-asyncio` runs in `asyncio_mode = auto` — do not decorate tests with `@pytest.mark.asyncio`.
- Tokens are **never** stored in plaintext, **never** logged at any level. Always use `TokenEncryption` from `app/utils/encryption.py`.

## Decisions & Constraints

- `pgvector==0.2.5` is pinned but the `vector` extension is **not yet enabled**. The first migration that adds an embedding column must run `CREATE EXTENSION IF NOT EXISTS vector`.
- All PKs are `UUID` with `server_default=gen_random_uuid()`. All timestamps are `TIMESTAMPTZ`.
- No `ON DELETE CASCADE` anywhere — preserve the audit trail. `Memory.deleted_at` is the soft-delete marker.
- `ENCRYPTION_KEY` must be a Fernet key (urlsafe-base64 of 32 bytes). Generate with `app/utils/generate_key.py`. A plain string will fail at `TokenEncryption` init.
- OAuth state uses Fernet (same `ENCRYPTION_KEY`) with a 10-minute TTL — do not switch to a plain HMAC or unsigned JWT; the TTL prevents replay.
- `GoogleAuthService.get_valid_token` refreshes proactively when `token_expires_at - now < 5 min`; on 4xx from Google or any decrypt failure it sets `status='revoked'` so the user is re-prompted.
- Caddy terminates TLS; the `api` container is **not** exposed on the host.
- `.env` is git-ignored; `.env.example` is the source of truth. Adding a secret → update both `.env.example` and `app/config.py:Settings`.

## Current State

- 129 unit tests passing (`pytest -q`). No CI configured.
- Google OAuth flow is wired end-to-end: `/authorize` → Google consent → `/callback` → encrypted token storage. Calendar-keyword messages prompt unauthorized users with the auth URL; all other messages still echo.
- `GoogleAuthService` is implemented but the calendar action handlers (reading/writing events) are not yet built.
- Non-text inbound messages are silently acknowledged. Status webhooks are dropped.
- Schema baseline (`0001_initial_schema`) is the only revision; no embedding column / pgvector activation yet.
