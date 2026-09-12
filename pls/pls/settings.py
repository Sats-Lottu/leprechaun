from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8'
    )
    RABBITMQ_URL: str = "amqp://user:password@localhost:5672/"
    PAYMENT_COMMANDS_QUEUE: str = "payment.lightning.commands"
    PAYMENT_EVENTS_QUEUE: str = "payment.lightning.events"
    PAYMENT_COMMANDS_DLQ: str = "payment.lightning.commands.dlq"
    COMMAND_RETRY_ATTEMPTS: int = 3
    COMMAND_RETRY_BACKOFF_SEC: float = 0.1
    OUTBOX_PUBLISH_BATCH_SIZE: int = 100
    RECONCILIATION_INTERVAL_SECONDS: float = 60.0
    RECONCILIATION_BATCH_SIZE: int = 100

    SERVICE_NAME: str = "payment-lightning-service"
    SCHEMA_VERSION: int = 1
    LOG_LEVEL: str = "INFO"
    OBSERVABILITY_ENABLED: bool = True
    OBSERVABILITY_HOST: str = "0.0.0.0"
    OBSERVABILITY_PORT: int = 8001
    DATABASE_URL: str = 'sqlite+aiosqlite:///database.db'

    # LNBITS
    LNBITS_URL: str = "http://localhost:5000"
    LNBITS_WALLET_ID: str = ''
    LNBITS_INVOICE_READ_KEY: str = ''
    LNBITS_ADMIN_KEY: str = ''
    LNBITS_USERNAME: str = ""
    LNBITS_PASSWORD: str = ""
    LNBITS_INVOICE_TTL: int = 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
