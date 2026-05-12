# Production Deployment Guide

End-to-end runbook for taking this project from zero to a running production stack
on a fresh Ubuntu VPS, with daily off-site backups and a tested restore path.

Estimated time: **45–60 minutes** the first time, ~5 minutes for subsequent deploys.

---

## 0. What you will end up with

```
                 (TLS, port 443)
   Internet ──────► Caddy ─────► api:8000 ─────► db:5432
                      │                │
                      │ ACME cert      │ /health
                      ▼                ▼
                Let's Encrypt    Postgres 16 (volume: pgdata)

   cron (3 AM daily) ──► scripts/backup.sh ──► Backblaze B2
```

- Single Docker host (Ubuntu 22.04 or 24.04).
- Caddy terminates TLS on 80/443 and reverse-proxies to `api:8000` over the
  internal compose network.
- `db` and `api` containers are **not** published to the host.
- Daily pg_dump uploaded to Backblaze B2; local copies pruned after 7 days.
- Restore is a single command and has been tested before go-live.

---

## 1. Prerequisites (do these BEFORE touching the VPS)

You will need accounts and credentials for:

| What                          | What you need                                       |
| ----------------------------- | --------------------------------------------------- |
| Domain                        | A registered domain you control DNS for            |
| VPS                           | Ubuntu 22.04 or 24.04, ≥ 1 vCPU, ≥ 1 GB RAM, ≥ 20 GB SSD |
| Meta WhatsApp Business app    | App ID, App Secret, permanent Access Token, Phone Number ID, a verify token you invent |
| OpenAI                        | API key starting with `sk-…`                       |
| Google Cloud project          | OAuth 2.0 Client ID + Secret with Calendar API enabled |
| Backblaze B2                  | Bucket name, application key id, application key  |

Have all of these in a password manager or temporary scratchpad before you start —
the install is much smoother when you can paste them in on demand.

Generate the Fernet `ENCRYPTION_KEY` ahead of time on any machine with Python:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep this key safe — losing it means **every stored OAuth token is unreadable**.

---

## 2. Point DNS at the VPS

Create an `A` record (and `AAAA` if your VPS has IPv6):

```
yourdomain.com   A    203.0.113.42        ; replace with your VPS IPv4
yourdomain.com   AAAA 2001:db8::1          ; optional
```

Verify before continuing:

```bash
dig +short yourdomain.com
```

DNS must be live before the first deploy, otherwise Caddy can't get a TLS cert
from Let's Encrypt.

---

## 3. Bootstrap the VPS

SSH in as a sudo-capable user. Then either copy `scripts/vps-setup.sh` over or
clone the repo first.

```bash
# Option A: clone first, then bootstrap
sudo git clone https://github.com/your/repo.git /opt/whatsapp-assistant
cd /opt/whatsapp-assistant
sudo ./scripts/vps-setup.sh

# Option B: run the bootstrap script directly via scp
scp scripts/vps-setup.sh ubuntu@your-vps:/tmp/
ssh ubuntu@your-vps "sudo bash /tmp/vps-setup.sh"
```

The script is idempotent. It installs:

- Docker Engine + Compose plugin from Docker's official apt repo
- UFW configured to allow **only** 22/tcp, 80/tcp, 443/tcp
- rclone (for B2 backups)
- fail2ban (basic SSH brute-force protection)
- `/opt/whatsapp-assistant/` and `/opt/whatsapp-assistant/backups/`

Verify before continuing:

```bash
docker --version
docker compose version
sudo ufw status verbose      # should show 22, 80, 443 ALLOW
rclone version
```

---

## 4. Get the code onto the VPS

If you used Option A above, skip this. Otherwise:

```bash
sudo git clone https://github.com/your/repo.git /opt/whatsapp-assistant
sudo chown -R $USER:$USER /opt/whatsapp-assistant   # optional, for convenience
cd /opt/whatsapp-assistant
```

---

## 5. Create `.env`

```bash
cp .env.example .env
$EDITOR .env
```

Fill in every value. Required keys (the startup validator
`app/utils/env_check.py` will refuse to boot without these):

```
POSTGRES_USER=assistant
POSTGRES_PASSWORD=<strong random string>
POSTGRES_DB=assistant_db
DATABASE_URL=postgresql+asyncpg://assistant:<same as above>@db:5432/assistant_db

OPENAI_API_KEY=sk-...

WHATSAPP_VERIFY_TOKEN=<anything you make up — Meta will echo it back>
WHATSAPP_ACCESS_TOKEN=<permanent system-user token from Meta>
WHATSAPP_PHONE_NUMBER_ID=<from Meta>
META_APP_SECRET=<from Meta — used for HMAC webhook signature verification>

GOOGLE_CLIENT_ID=<from Google Cloud>
GOOGLE_CLIENT_SECRET=<from Google Cloud>
GOOGLE_REDIRECT_URI=https://yourdomain.com/oauth/google/callback

ENCRYPTION_KEY=<Fernet key generated in step 1 — DO NOT lose this>

DAILY_API_LIMIT=100
DEFAULT_TIMEZONE=Europe/Madrid

B2_BUCKET=whatsapp-assistant-backups
```

Lock down the file:

```bash
chmod 600 .env
```

**`.env` is git-ignored. Never commit it. Never paste it into chat tools.**

---

## 6. Edit `Caddyfile`

Replace the placeholder domain:

```bash
$EDITOR Caddyfile
```

```caddyfile
yourdomain.com {
    request_body {
        max_size 10MB
    }
    reverse_proxy api:8000
}
```

If you also want a separate dev hostname (for the Cloudflare Tunnel during
development), Caddy doesn't need to know — that goes through the tunnel, not
through Caddy.

---

## 7. First deploy

```bash
./deploy.sh
```

What this does, step by step:

1. Confirms `.env` exists and contains all required vars (it sources `.env`,
   then checks each).
2. `docker compose build` for the `api` image.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` —
   starts `db`, `api`, `caddy`.
4. Waits up to 30s for `pg_isready` inside `db`.
5. Runs `alembic upgrade head` inside the running `api` container.
6. Polls `/health` from inside the api container for up to 30s.
7. Prints `docker compose ps` and the last 80 lines of api logs if anything
   went sideways.

While it runs, Caddy will request a Let's Encrypt cert for your domain — that
takes ~30s the first time and will be cached in the `caddy_data` volume after.

Verify from your laptop:

```bash
curl -sf https://yourdomain.com/health | jq
```

Expected:

```json
{ "status": "healthy", "checks": { "database": { "status": "ok" } , ... } }
```

If `status` is `unhealthy` or the curl fails, see [§13 Troubleshooting](#13-troubleshooting).

---

## 8. Configure the WhatsApp webhook (Meta dashboard)

1. Go to [developers.facebook.com](https://developers.facebook.com) → your app
   → WhatsApp → Configuration.
2. Set Callback URL: `https://yourdomain.com/webhooks/whatsapp`
3. Set Verify Token: the same value you put in `WHATSAPP_VERIFY_TOKEN`.
4. Click **Verify and Save**. Meta will issue a GET to the callback URL with
   `hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`; the app echoes the
   challenge back if the token matches.
5. Subscribe to the **messages** webhook field at minimum.
6. Send yourself a WhatsApp message from the registered test number. Tail logs:

   ```bash
   make logs
   ```

   You should see one structured JSON log line per message ingestion and one
   per outbound reply.

---

## 9. Configure Google OAuth (Google Cloud Console)

1. APIs & Services → Credentials → your OAuth 2.0 Client ID → **Authorized
   redirect URIs**.
2. Add: `https://yourdomain.com/oauth/google/callback`
3. APIs & Services → Library → enable **Google Calendar API**.
4. From WhatsApp, send the bot a calendar request (e.g. "schedule lunch
   tomorrow at 1pm"). It will reply with an authorize link the first time. Tap
   it → consent → you'll be redirected to `/oauth/google/callback?...`, and
   the encrypted refresh token is stored in the DB.

---

## 10. Set up automated backups

### 10a. Configure rclone for Backblaze B2

```bash
rclone config
```

Walkthrough:

- `n` for new remote.
- Name: `b2` (must match what the scripts expect, or override
  `RCLONE_REMOTE` in the env).
- Storage: `Backblaze B2` (number varies by version — use the `b2` option).
- Account: your B2 application key id.
- Key: your B2 application key.
- Leave the rest at defaults.

Smoke test:

```bash
rclone lsd b2:
rclone copy /etc/hostname b2:$B2_BUCKET/_rclone_test/ && \
  rclone delete b2:$B2_BUCKET/_rclone_test/hostname
```

### 10b. Run a backup manually

```bash
make backup
ls -lh backups/
rclone ls b2:$B2_BUCKET | tail
```

Expected: a `backup_YYYYMMDD_HHMMSS.sql.gz` both locally and in B2.

### 10c. Schedule the daily cron

```bash
sudo crontab -e
```

Append:

```
0 3 * * * /opt/whatsapp-assistant/scripts/backup.sh >> /var/log/whatsapp-assistant-backup.log 2>&1
```

Sanity check the cron parsing:

```bash
sudo crontab -l
sudo grep CRON /var/log/syslog | tail   # the next morning
```

---

## 11. Run the restore drill (DO THIS BEFORE YOU GO LIVE)

A backup you've never restored is not a backup.

1. Take a fresh backup:
   ```bash
   make backup
   ```
2. Note the filename (`backup_YYYYMMDD_HHMMSS.sql.gz`).
3. Restore it on top of the running database:
   ```bash
   make restore file=backup_YYYYMMDD_HHMMSS.sql.gz
   ```
4. The script will:
   - Confirm with you interactively (`yes` required).
   - Stop `api`.
   - Drop and recreate `$POSTGRES_DB`.
   - Pipe `gunzip` into `psql` inside the `db` container.
   - Restart `api` and poll `/health`.
5. After it finishes, send yourself a WhatsApp message and confirm the bot
   still answers correctly.

If anything in this drill fails, **stop and fix it now**, not at 3 AM during
an incident.

---

## 12. Day-to-day operations

### Re-deploy after a code change

```bash
git pull
./deploy.sh
```

Migrations run automatically. If you need a one-off without rebuilding:

```bash
make migrate
```

### Watch the logs

```bash
make logs                          # api only, follow
docker compose logs -f             # everything
docker compose logs --tail=200 db  # db, no follow
```

Logs are JSON, rotated by Docker's json-file driver at 10 MB × 3 files per
container, so the disk cannot fill from logging alone.

### Check health

```bash
make health
```

Status values:

| Status      | Meaning                                                        | Action                                          |
| ----------- | -------------------------------------------------------------- | ----------------------------------------------- |
| `healthy`   | DB reachable, pipeline making progress                         | nothing                                         |
| `degraded`  | DB OK, but no message processed in 5 min AND unprocessed rows  | check api logs for stalled work                 |
| `unhealthy` | DB probe failed                                                | check `docker compose logs db`, disk, network   |

### Create a new migration

```bash
make migrate-create msg="add some_column to users"
git diff alembic/versions/        # review!
make migrate
```

### Get a shell

```bash
make shell         # bash inside the api container
make db-shell      # psql inside the db container
```

---

## 13. Troubleshooting

**`deploy.sh` exits with "Missing required env vars"** — open `.env` and add
the named keys. The validator (`app/utils/env_check.py`) is intentionally
strict so a half-configured container never half-boots.

**`/health` returns 502 from Caddy** — the api container probably isn't up
yet, or it crashed on startup. `docker compose logs api` will show the error.
The most common one is an invalid `ENCRYPTION_KEY` (not a valid Fernet key).

**`/health` returns `unhealthy` with `database.status: error`** — postgres
isn't ready or the password in `DATABASE_URL` doesn't match `POSTGRES_PASSWORD`.
`docker compose logs db` will say which.

**Caddy can't get a TLS certificate** — DNS isn't pointing at the VPS yet, or
port 80 is blocked. `docker compose logs caddy` shows the ACME error.

**WhatsApp webhook verification fails in the Meta dashboard** — the verify
token in `.env` doesn't match what you pasted into the dashboard, or
`https://yourdomain.com/webhooks/whatsapp` isn't actually reachable yet.

**Messages arrive but get no reply** — check `make logs`. Common causes: daily
OpenAI limit reached (bot replies `Daily limit reached. Try again tomorrow!`),
revoked Google token (bot replies with an authorize link), or OpenAI 429.

**Backup cron isn't running** — `sudo grep CRON /var/log/syslog | tail` first,
then `cat /var/log/whatsapp-assistant-backup.log`. Make sure the script is
executable (`chmod +x scripts/backup.sh`) and `B2_BUCKET` is in `.env`.

---

## 14. Rollback

### Rollback the application

```bash
# Find the previous image
docker image ls whatsapp-assistant-api

# Re-tag the previous good image and restart api
docker tag <previous-image-id> whatsapp-assistant-api:latest
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d api
```

### Rollback a bad migration

Downgrade BEFORE you roll back the code — old code can't talk to a newer
schema, but new code generally can talk to a slightly-older schema.

```bash
docker compose exec api alembic downgrade -1
```

Then re-deploy whatever code version you want.

### Restore from yesterday

```bash
rclone lsf b2:$B2_BUCKET | tail
make restore file=backup_YYYYMMDD_HHMMSS.sql.gz
```

---

## 15. Security checklist before going public

- [ ] `.env` is `chmod 600` and **not** in git.
- [ ] `ENCRYPTION_KEY` is backed up in a password manager.
- [ ] UFW shows only 22/80/443 allowed (`sudo ufw status verbose`).
- [ ] SSH uses key auth only, no password auth.
- [ ] The B2 application key is **scoped to the backup bucket only**, not
      master-key.
- [ ] You have run §11 (restore drill) successfully.
- [ ] The Meta access token is a **system-user permanent token**, not the
      24-hour temporary developer token.
- [ ] `Caddyfile` has your real domain, not `yourdomain.com`.
- [ ] DNS A/AAAA records are correct and resolve from outside your network.

---

## 16. Dev-mode webhooks (optional, for local development only)

When developing locally, expose your laptop to Meta via Cloudflare Tunnel
without deploying:

```bash
make dev                  # bring the local stack up
./scripts/dev-tunnel.sh   # prints a https://<random>.trycloudflare.com URL
```

Point the WhatsApp webhook at that URL temporarily while you iterate. For a
stable hostname, follow the named-tunnel instructions in the header of
`scripts/dev-tunnel.sh`.

---

## Appendix: file map

| File                         | Purpose                                          |
| ---------------------------- | ------------------------------------------------ |
| `docker-compose.yml`         | Base compose (api, db, caddy, volumes)           |
| `docker-compose.prod.yml`    | Prod override: restart, log rotation, healthchecks, resource limits |
| `Caddyfile`                  | HTTPS reverse proxy config (edit domain!)        |
| `Dockerfile`                 | Python 3.12 slim + curl + app                    |
| `deploy.sh`                  | One-command deploy + migrate + health-check     |
| `Makefile`                   | Operational shortcuts                            |
| `scripts/vps-setup.sh`       | One-time fresh-VPS bootstrap                     |
| `scripts/backup.sh`          | Daily pg_dump → gzip → B2                        |
| `scripts/restore.sh`         | Restore from local or B2                         |
| `scripts/dev-tunnel.sh`      | Cloudflare Tunnel for local dev                  |
| `app/utils/env_check.py`     | Startup env validator (called from `app/main.py`) |
| `.env.example`               | Template — copy to `.env` and fill in            |
