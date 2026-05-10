import os
from collections.abc import AsyncGenerator

import pytest
from cryptography.fernet import Fernet

# A deterministic-per-run Fernet key keeps encryption-aware tests honest while
# avoiding a hardcoded secret leaking into the repo.
_TEST_FERNET_KEY = Fernet.generate_key().decode("utf-8")

# Populate required env vars before any app module is imported so that
# Settings() validation succeeds without a real .env file present.
_TEST_ENV: dict[str, str] = {
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DB": "test_db",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test_db",
    "OPENAI_API_KEY": "sk-test",
    "WHATSAPP_VERIFY_TOKEN": "verify-token-test",
    "WHATSAPP_ACCESS_TOKEN": "access-token-test",
    "WHATSAPP_PHONE_NUMBER_ID": "1234567890",
    "META_APP_SECRET": "meta-secret-test",
    "GOOGLE_CLIENT_ID": "google-client-id-test",
    "GOOGLE_CLIENT_SECRET": "google-client-secret-test",
    "GOOGLE_REDIRECT_URI": "https://example.com/oauth/google/callback",
    "ENCRYPTION_KEY": _TEST_FERNET_KEY,
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)


@pytest.fixture
async def client() -> AsyncGenerator:
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
