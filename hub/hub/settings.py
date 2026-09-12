from functools import lru_cache
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8'
    )
    RABBITMQ_URL: str = 'amqp://user:password@localhost:5672/'
    DATABASE_URL: str = 'sqlite+aiosqlite:///./database.db'
    PAYMENT_COMMANDS_QUEUE: str = 'payment.lightning.commands'
    LIGHTNING_SETTLEMENT_ACCOUNT_ID: UUID | None = None
    LEDGER_BASE_URL: str = 'http://ledger:8000'
    LEDGER_INTERNAL_API_TOKEN: str = ''
    SERVICE_NAME: str = 'hub'
    SCHEMA_VERSION: int = 1
    LOG_LEVEL: str = 'INFO'
    HUB_STORAGE_SECRET: str = 'change-me-hub-storage-secret'
    SESSION_SECRET_KEY: str = 'change-me-session-secret'
    SESSION_HTTPS_ONLY: bool = False
    OIDC_CLIENT_ID: str = 'hub-local-client'
    OIDC_CLIENT_SECRET: str | None = None
    OIDC_SERVER_METADATA_URL: str = 'http://localhost:8080/realms/leprechaun/.well-known/openid-configuration'
    OIDC_ISSUER: str = 'http://localhost:8080/realms/leprechaun'
    OIDC_SCOPE: str = 'openid profile'
    OIDC_REDIRECT_PATH: str = '/auth/callback'


@lru_cache
def get_settings() -> Settings:
    return Settings()
