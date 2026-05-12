# WhatsApp assistant operational shortcuts.
#
# Examples:
#   make dev                          # local stack with rebuild
#   make prod                         # production stack (Caddy + HTTPS)
#   make migrate                      # run Alembic upgrade head
#   make migrate-create msg="add x"   # autogenerate a migration revision
#   make backup / make restore file=backup_20260511_030000.sql.gz
#   make health
#   make clean                        # DESTROYS volumes

COMPOSE      = docker compose
COMPOSE_PROD = docker compose -f docker-compose.yml -f docker-compose.prod.yml

.DEFAULT_GOAL := help
.PHONY: help dev prod migrate migrate-create backup restore logs shell db-shell health clean

help:
	@echo "Targets:"
	@echo "  dev             - up --build (dev compose)"
	@echo "  prod            - up -d --build with prod override"
	@echo "  migrate         - alembic upgrade head in api container"
	@echo "  migrate-create  - autogenerate revision: make migrate-create msg=\"desc\""
	@echo "  backup          - scripts/backup.sh"
	@echo "  restore         - scripts/restore.sh file=<filename>"
	@echo "  logs            - tail api logs"
	@echo "  shell           - bash inside api container"
	@echo "  db-shell        - psql inside db container"
	@echo "  health          - curl /health on the local stack"
	@echo "  clean           - down -v (DESTRUCTIVE: wipes volumes)"

dev:
	$(COMPOSE) up --build

prod:
	$(COMPOSE_PROD) up -d --build

migrate:
	$(COMPOSE) exec api alembic upgrade head

migrate-create:
	@if [ -z "$(msg)" ]; then echo "Usage: make migrate-create msg=\"description\"" >&2; exit 1; fi
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(msg)"

backup:
	./scripts/backup.sh

restore:
	@if [ -z "$(file)" ]; then echo "Usage: make restore file=backup_YYYYMMDD_HHMMSS.sql.gz" >&2; exit 1; fi
	./scripts/restore.sh "$(file)"

logs:
	$(COMPOSE) logs -f api

shell:
	$(COMPOSE) exec api /bin/bash

db-shell:
	$(COMPOSE) exec db psql -U assistant assistant_db

health:
	@curl -sf http://localhost:8000/health | python -m json.tool || (echo "health check failed" >&2; exit 1)

# DESTRUCTIVE: deletes the postgres volume and any persistent state.
clean:
	@echo "About to run 'docker compose down -v' — this DELETES the postgres volume."
	@read -r -p "Type 'yes' to continue: " ans && [ "$$ans" = "yes" ] || (echo "aborted"; exit 1)
	$(COMPOSE) down -v
