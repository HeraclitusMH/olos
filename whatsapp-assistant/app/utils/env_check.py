"""Startup environment validation — raises SystemExit on misconfiguration."""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def validate_environment() -> None:
    from app.config import get_settings
    from cryptography.fernet import Fernet, InvalidToken

    try:
        settings = get_settings()
    except Exception as exc:
        logger.critical("Missing required environment variables: %s", exc)
        sys.exit(1)

    if not settings.database_url.startswith("postgresql+asyncpg://"):
        logger.critical(
            "DATABASE_URL must use postgresql+asyncpg:// scheme, got: %s",
            settings.database_url[:40],
        )
        sys.exit(1)

    try:
        Fernet(settings.encryption_key.encode())
    except Exception:
        logger.critical("ENCRYPTION_KEY is not a valid Fernet key")
        sys.exit(1)
