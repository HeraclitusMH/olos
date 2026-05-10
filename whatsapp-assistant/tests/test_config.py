from app.config import Settings, get_settings


def test_settings_loads_from_environment() -> None:
    settings = Settings()

    assert settings.postgres_user == "test"
    assert settings.postgres_db == "test_db"
    assert settings.openai_api_key == "sk-test"
    assert settings.whatsapp_verify_token == "verify-token-test"
    assert settings.meta_app_secret == "meta-secret-test"
    assert settings.google_client_id == "google-client-id-test"


def test_settings_applies_defaults() -> None:
    settings = Settings()

    assert settings.daily_api_limit == 100
    assert settings.default_timezone == "Europe/Madrid"


def test_settings_types_are_correct() -> None:
    settings = Settings()

    assert isinstance(settings.daily_api_limit, int)
    assert isinstance(settings.default_timezone, str)


def test_get_settings_is_cached() -> None:
    a = get_settings()
    b = get_settings()

    assert a is b


def test_database_url_uses_async_driver() -> None:
    settings = Settings()

    assert settings.database_url.startswith("postgresql+asyncpg://")
