from functools import lru_cache
from uuid import UUID

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8"
    )
    DATABASE_URL: str = "sqlite+aiosqlite:///database.db"
    RABBITMQ_URL: str = "amqp://user:password@localhost:5672/"
    PAYMENT_EVENTS_QUEUE: str = "payment.lightning.events"
    LIGHTNING_SETTLEMENT_ACCOUNT_ID: UUID | None = None
    HOLD_EXPIRATION_INTERVAL_SECONDS: float = 60.0
    HOLD_EXPIRATION_BATCH_SIZE: int = 100
    LEDGER_INTERNAL_API_TOKENS: str = ""
    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "ledger"


@lru_cache
def get_settings() -> Settings:
    return Settings()
