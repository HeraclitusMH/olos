# WhatsApp Assistant

A self-hosted WhatsApp personal assistant that turns natural-language messages into actions on your Google Calendar, manages reminders, sends a daily agenda, and remembers things for you. Built on FastAPI, async SQLAlchemy, Postgres, and the OpenAI tool-calling API.

```
WhatsApp ──► Caddy (TLS) ──► FastAPI (api:8000) ──► OpenAI planner
                                  │
                                  ├──► Google Calendar API
                                  ├──► Postgres 16 (encrypted tokens, memories, reminders)
                                  └──► Background tasks: daily agenda + reminder scheduler
```

## Features

- **Natural-language calendar** — create, query, update and cancel Google Calendar events ("schedule lunch with Sara tomorrow at 1pm").
- **Reminders** — one-shot reminders delivered via WhatsApp, with create / query / update / cancel flows and a 24h-window template fallback.
- **Daily agenda** — proactive morning summary of the day's events, per-user timezone and send time, with an optional custom footer.
- **Long-term memory** — store facts the assistant should remember (`MemoryService` with Postgres full-text search + tag overlap), soft-delete only.
- **Disambiguation** — when an update/cancel matches more than one event or reminder, the assistant sends WhatsApp buttons / list pickers and resumes the original action on tap (5-min TTL).
- **Cost control** — daily OpenAI request quota per deployment, friendly over-limit reply.
- **Secure by default** — Fernet-encrypted Google OAuth tokens, HMAC-verified Meta webhook signatures, OAuth `state` signed with the same key and TTL'd.
- **Production-ready ops** — one-command deploy, Alembic migrations, structured JSON logs with redaction, `/health` endpoint, daily off-site backups to Backblaze B2 with a tested restore path.

## Tech stack

- Python 3.12, **async-only** (no sync DB / HTTP in app code)
- FastAPI 0.111, Uvicorn (standard)
- SQLAlchemy 2 (async) + asyncpg + Alembic
- Postgres 16 (pgvector pinned; extension reserved for a future embedding column)
- OpenAI Python SDK (`gpt-4.1-mini` planner with `tool_choice="required"`)
- Google Calendar via raw `httpx` (no `google-api-python-client`)
- Caddy (automatic Let's Encrypt TLS) in front of the app container
- Docker Compose for local dev and production

## Repository layout

```
app/
├── main.py               # FastAPI app, lifespan, background schedulers
├── config.py             # pydantic-settings — single source of truth for env
├── database.py           # async engine / session / declarative base
├── routers/              # webhooks (WhatsApp), oauth (Google)
├── services/             # planner, tool_executor, calendar, memory, reminder, daily_agenda, ...
├── models/               # ORM models (re-exported for Alembic autogenerate)
└── utils/                # encryption, signature, timezone, retry, logging, env_check
alembic/                  # migrations (0001 initial → 0004 reminders)
scripts/                  # vps-setup, backup, restore, dev-tunnel
tests/                    # pytest, asyncio_mode = auto, no Docker/live DB required
docker-compose.yml        # base: api + db + caddy
docker-compose.prod.yml   # prod override: restart, log rotation, healthchecks, limits
deploy.sh                 # build + migrate + up + poll /health
Makefile                  # dev / prod / migrate / backup / restore / logs / shell / health
Caddyfile                 # TLS reverse proxy (edit your domain)
DEPLOYMENT.md             # full production runbook (this is the source of truth)
```

## Quick start (local development)

Prerequisites: Docker + Docker Compose, accounts for OpenAI, Meta WhatsApp Business, and Google Cloud (Calendar API enabled).

```bash
git clone https://github.com/<you>/whatsapp-assistant.git
cd whatsapp-assistant

cp .env.example .env
# Edit .env — at minimum: OPENAI_API_KEY, WHATSAPP_*, GOOGLE_*, ENCRYPTION_KEY
# Generate ENCRYPTION_KEY (Fernet — required, app refuses to boot otherwise):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

make dev                 # docker compose up --build
make migrate             # alembic upgrade head inside the api container
make health              # curl /health on the local stack
```

To expose your local stack to Meta during development:

```bash
./scripts/dev-tunnel.sh  # prints a https://<random>.trycloudflare.com URL
```

Point the WhatsApp webhook (Meta dashboard → WhatsApp → Configuration) at `<tunnel-url>/webhooks/whatsapp` with the same `WHATSAPP_VERIFY_TOKEN` you set in `.env`.

## Configuration

Every secret and tunable lives in `.env` (git-ignored) and is loaded via `app/config.py:get_settings()` — **never** read `os.environ` directly in app code. `.env.example` is the canonical template; any new setting must be added there too.

Startup validation runs in `app/utils/env_check.py` before the FastAPI app is constructed — a missing or malformed required var raises `SystemExit` so a half-configured container never half-boots.

Key groups:

| Group              | Vars                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------- |
| Database           | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL` (asyncpg driver)    |
| OpenAI             | `OPENAI_API_KEY`, `DAILY_API_LIMIT`                                                     |
| WhatsApp (Meta)    | `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `META_APP_SECRET`, `WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANGUAGE` |
| Google             | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`                       |
| Crypto             | `ENCRYPTION_KEY` (Fernet — losing it makes stored OAuth tokens unreadable)              |
| Daily agenda       | `DAILY_AGENDA_DEFAULT_TIMEZONE`, `DAILY_AGENDA_DEFAULT_TIME_LOCAL`, `DAILY_AGENDA_TICK_SECONDS`, `DAILY_AGENDA_SEND_WINDOW_MINUTES` |
| Reminders          | `REMINDER_TICK_SECONDS`, `REMINDER_MAX_ATTEMPTS`, `WHATSAPP_REMINDER_TEMPLATE_NAME`     |
| Backups (ops only) | `B2_BUCKET` — used by `scripts/backup.sh` / `scripts/restore.sh`, not by the app        |

## Running tests

Tests are async-first, do **not** require Docker or a live database (sessions are faked, migrations parsed via `ast.parse`), and `pytest-asyncio` runs in `asyncio_mode = auto` — do not decorate tests with `@pytest.mark.asyncio`.

```bash
pip install -r requirements.txt
pytest
```

## How a message is handled

1. **Webhook receipt** — `app/routers/webhooks.py` verifies the Meta HMAC signature, persists the inbound row, returns `200` immediately.
2. **Processing** — `process_inbound_message` runs under `asyncio.wait_for(..., 25s)`; on timeout or unhandled failure a fresh session marks the row processed and sends a fallback reply. **No message is silently dropped.**
3. **Planner** — `app/services/planner.py` calls OpenAI with the current datetime injected per call, `tool_choice="required"`. Plain-text answers fall back to a `reply` tool call.
4. **Tool execution** — `ToolExecutor.execute()` dispatches to calendar / memory / reminder / agenda-settings / reply handlers and returns a `ToolResult`.
5. **Disambiguation** — when an update / cancel matches more than one candidate, the user receives WhatsApp buttons or a list; the original arguments are parked in `user.preferences_json["pending_action"]` with a 5-min TTL.
6. **Outbound** — `WhatsAppClient.send_*` enforces all Cloud-API limits (≤3 buttons, ≤10 list rows, length caps) and falls back to an approved template when outside the 24-hour customer-service window.

Background loops started in `app/main.py` lifespan:

- **Daily agenda scheduler** — per-user prefs (timezone + send time + optional custom footer), de-duplicated via a `UNIQUE(user_id, agenda_date)` row in `daily_agenda_sends` and a Postgres advisory lock for multi-replica safety.
- **Reminder scheduler** — partial index on unsent rows, force-marks `sent=True` after `REMINDER_MAX_ATTEMPTS` failed sends.

## Production deployment

The full end-to-end runbook (DNS, VPS bootstrap, Caddy TLS, first deploy, webhook setup, Backblaze B2 backups, restore drill, rollback) lives in **[DEPLOYMENT.md](./DEPLOYMENT.md)** — start there.

TL;DR:

```bash
sudo ./scripts/vps-setup.sh         # one-time: Docker, UFW 22/80/443, rclone, fail2ban
cp .env.example .env && $EDITOR .env
$EDITOR Caddyfile                   # your domain
./deploy.sh                         # build + migrate + up + poll /health
```

After the stack is up, point the Meta webhook at `https://yourdomain.com/webhooks/whatsapp` and add `https://yourdomain.com/oauth/google/callback` as an authorized Google redirect URI. Schedule the backup cron (`0 3 * * * /opt/whatsapp-assistant/scripts/backup.sh`) and run the restore drill **before** going live — a backup you've never restored is not a backup.

## Operational shortcuts

```
make dev             # local stack (compose up --build)
make prod            # prod stack with Caddy + HTTPS
make migrate         # alembic upgrade head
make migrate-create msg="add foo to users"
make backup          # pg_dump → gzip → B2 (rclone)
make restore file=backup_YYYYMMDD_HHMMSS.sql.gz
make logs            # tail api logs
make health          # curl /health
make shell / db-shell
```

## Security notes

- WhatsApp webhook payloads are HMAC-verified against `META_APP_SECRET` before being processed.
- Google OAuth refresh tokens are Fernet-encrypted at rest; the OAuth `state` parameter is signed with the same key and TTL'd to 10 minutes.
- Structured JSON logs redact tokens, bearer headers, and other secret-shaped values.
- The `db` and `api` containers are **not** published to the host in either compose file — only Caddy is reachable from the internet.
- `.env` is git-ignored and must be `chmod 600` in production. Never commit it. Never paste it into chat tools.

## License

Proprietary — Copyright (c) 2026 Luca Ippolito. All rights reserved. See [LICENSE](./LICENSE) for the full text. No right to use, copy, modify, or distribute is granted without prior written permission from the copyright holder. For licensing inquiries, contact lucapindemonte@gmail.com.
