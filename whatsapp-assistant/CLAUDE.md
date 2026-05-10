# CLAUDE.md

## What this is

WhatsApp personal assistant backend. FastAPI + async SQLAlchemy + Postgres 16 (with pgvector) + Caddy reverse proxy, orchestrated via Docker Compose.

## Architecture

- `app/main.py` — FastAPI app, `lifespan` startup hook, `GET /health`.
- `app/config.py` — `Settings` (pydantic-settings) + `get_settings()` (`@lru_cache`). All env reads go through here.
- `app/database.py` — async engine, `AsyncSessionLocal`, `get_db()` FastAPI dependency, `Base = DeclarativeBase`.
- `app/routers/webhooks.py` — `GET/POST /webhooks/whatsapp` (Meta verification + inbound messages, currently stubs).
- `app/models/`, `app/services/`, `app/utils/` — empty packages reserved for ORM models, business logic, helpers.
- `alembic/env.py` — async migration runner; pulls URL from `get_settings()`, imports `app.models` to register metadata.
- `tests/conftest.py` — sets test env vars **before** any app import; exposes async `client` fixture via `httpx.ASGITransport`.
- `docker-compose.yml` — services `api` (built from `Dockerfile`), `db` (postgres:16), `caddy` (ports 80/443). Named volumes: `pgdata`, `caddy_data`.
- `Caddyfile` — reverse proxy → `api:8000`, 10 MB body limit, auto-HTTPS. Domain placeholder `yourdomain.com` must be replaced before deploy.

## Rules & Patterns

- **Async only**: no sync DB calls, no sync HTTP. `DATABASE_URL` must use `postgresql+asyncpg://`.
- Type hints on every function; PEP 8 strict; Python 3.12 syntax (`X | Y`, `list[T]`, `from datetime import UTC`) is fine.
- Read config via `get_settings()`, never `os.environ` directly in app code.
- Inject DB sessions with `Depends(get_db)`; never instantiate `AsyncSessionLocal` inline in routes.
- New ORM models must be imported from `app/models/__init__.py` so Alembic autogenerate sees them.
- Use FastAPI `lifespan` context manager for startup/shutdown — `@app.on_event` is forbidden (deprecated).
- Tests must not require Docker or a live DB for unit-level coverage; use `ASGITransport` against `app.main.app`.

## Decisions & Constraints

- `pgvector==0.2.5` is pinned — the Postgres image needs the `vector` extension enabled in the first migration before any embedding column is created.
- Caddy terminates TLS; the `api` container is **not** exposed on the host — only Caddy publishes 80/443.
- `.env` is git-ignored; `.env.example` is the source of truth for required keys. Adding a new secret means updating both `.env.example` and `app/config.py:Settings`.
- `pytest-asyncio` runs in `asyncio_mode = auto` (see `pytest.ini`) — do not decorate tests with `@pytest.mark.asyncio`.

## Current State

- Scaffold complete; 11 unit tests passing (`pytest -q`).
- WhatsApp webhook endpoints return placeholder JSON — no signature verification, no message handling, no DB writes yet.
- Zero models, zero Alembic revisions. First migration must enable `CREATE EXTENSION IF NOT EXISTS vector`.
- No CI configured.
