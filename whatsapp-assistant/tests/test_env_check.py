import logging

import pytest
from cryptography.fernet import Fernet

from app.utils.env_check import validate_environment


def _valid_env() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+asyncpg://u:p@h:5432/d",
        "OPENAI_API_KEY": "sk-abc",
        "WHATSAPP_VERIFY_TOKEN": "v",
        "WHATSAPP_ACCESS_TOKEN": "a",
        "WHATSAPP_PHONE_NUMBER_ID": "1",
        "META_APP_SECRET": "s",
        "ENCRYPTION_KEY": Fernet.generate_key().decode("utf-8"),
        "GOOGLE_CLIENT_ID": "g",
        "GOOGLE_CLIENT_SECRET": "g-secret",
    }


def test_valid_environment_passes() -> None:
    validate_environment(_valid_env())


def test_missing_required_var_exits() -> None:
    env = _valid_env()
    del env["OPENAI_API_KEY"]
    with pytest.raises(SystemExit) as exc:
        validate_environment(env)
    assert "OPENAI_API_KEY" in str(exc.value)


def test_bad_database_url_exits() -> None:
    env = _valid_env()
    env["DATABASE_URL"] = "mysql://u:p@h/d"
    with pytest.raises(SystemExit) as exc:
        validate_environment(env)
    assert "DATABASE_URL" in str(exc.value)


def test_bad_openai_key_exits() -> None:
    env = _valid_env()
    env["OPENAI_API_KEY"] = "not-a-real-key"
    with pytest.raises(SystemExit) as exc:
        validate_environment(env)
    assert "OPENAI_API_KEY" in str(exc.value)


def test_bad_fernet_key_exits() -> None:
    env = _valid_env()
    env["ENCRYPTION_KEY"] = "not-base64-and-not-32-bytes"
    with pytest.raises(SystemExit) as exc:
        validate_environment(env)
    assert "ENCRYPTION_KEY" in str(exc.value)


def test_missing_optional_google_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    env = _valid_env()
    del env["GOOGLE_CLIENT_ID"]
    with caplog.at_level(logging.WARNING, logger="app.utils.env_check"):
        validate_environment(env)
    assert any("GOOGLE_CLIENT_ID" in r.message for r in caplog.records)
