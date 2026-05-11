from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Database
    postgres_user: str
    postgres_password: str
    postgres_db: str
    database_url: str

    # OpenAI
    openai_api_key: str

    # WhatsApp / Meta
    whatsapp_verify_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    meta_app_secret: str

    # Google OAuth
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str

    # Security
    encryption_key: str

    # App settings
    daily_api_limit: int = 100
    default_timezone: str = "Europe/Madrid"

    # Daily agenda scheduler
    daily_agenda_default_timezone: str = "Asia/Makassar"
    daily_agenda_default_time_local: str = "08:00"
    daily_agenda_tick_seconds: int = 60
    daily_agenda_send_window_minutes: int = 5


@lru_cache
def get_settings() -> Settings:
    return Settings()
