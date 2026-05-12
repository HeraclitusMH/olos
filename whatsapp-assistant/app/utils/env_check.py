"""Startup environment validation.

Run from ``app.main`` before the FastAPI app is created. Fails fast with a
clear error so misconfigured containers don't half-boot and then 500 on every
request.
"""

from __future__ import annotations

import logging
import os
import sys

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


REQUIRED_VARS: tuple[str, ...] = (
    "DATABASE_URL",
    "OPENAI_API_KEY",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "META_APP_SECRET",
    "ENCRYPTION_KEY",
)

OPTIONAL_VARS: tuple[str, ...] = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
)


def _is_valid_fernet_key(key: str) -> bool:
    try:
        Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        return False
    return True


def validate_environment(env: dict[str, str] | None = None) -> None:
    """Validate the runtime environment.

    Raises :class:`SystemExit` with a human-readable message when something
    required is missing or malformed. Warnings are emitted via the configured
    logger for optional vars.

    Parameters
    ----------
    env:
        Mapping to validate. Defaults to ``os.environ``. Exposed for tests.
    """
    env = env if env is not None else dict(os.environ)

    errors: list[str] = []

    missing = [name for name in REQUIRED_VARS if not env.get(name)]
    if missing:
        errors.append("Missing required env vars: " + ", ".join(sorted(missing)))

    database_url = env.get("DATABASE_URL", "")
    if database_url and not database_url.startswith("postgresql"):
        errors.append(
            "DATABASE_URL must start with 'postgresql' "
            "(use postgresql+asyncpg:// for the async driver)."
        )

    openai_key = env.get("OPENAI_API_KEY", "")
    if openai_key and not openai_key.startswith("sk-"):
        errors.append("OPENAI_API_KEY must start with 'sk-'.")

    encryption_key = env.get("ENCRYPTION_KEY", "")
    if encryption_key and not _is_valid_fernet_key(encryption_key):
        errors.append(
            "ENCRYPTION_KEY is not a valid Fernet key. "
            "Generate one with: python -m app.utils.generate_key"
        )

    for name in OPTIONAL_VARS:
        if not env.get(name):
            logger.warning(
                "Optional env var %s is not set — Google features will be disabled.",
                name,
                extra={"event": "env_check.optional_missing", "var": name},
            )

    if errors:
        message = "Environment validation failed:\n  - " + "\n  - ".join(errors)
        logger.error(message, extra={"event": "env_check.failed"})
        raise SystemExit(message)

    logger.info("Environment validated", extra={"event": "env_check.ok"})
