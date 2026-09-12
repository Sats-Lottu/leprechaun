import asyncio
import logging

from faststream import FastStream

from pls import commands, messaging  # noqa: F401
from pls.lifecycle import create_lnbits_lifespan
from pls.observability import start_observability_server
from pls.provider import LNbitsConfig
from pls.settings import get_settings

settings = get_settings()

log = logging.getLogger(settings.SERVICE_NAME)
logging.basicConfig(level=settings.LOG_LEVEL)

lnbits_cfg = LNbitsConfig(
    base_url=settings.LNBITS_URL,
    wallet_id=settings.LNBITS_WALLET_ID,
    username=settings.LNBITS_USERNAME,
    password=settings.LNBITS_PASSWORD,
    invoice_read_key=settings.LNBITS_INVOICE_READ_KEY,
    admin_key=settings.LNBITS_ADMIN_KEY,
    invoice_ttl_sec=settings.LNBITS_INVOICE_TTL,
)

app = FastStream(
    messaging.broker, lifespan=create_lnbits_lifespan(lnbits_cfg)
)


async def main() -> None:
    start_observability_server()
    log.info(
        "starting payment-service rabbit=%s commands_queue=%s events_queue=%s",
        settings.RABBITMQ_URL,
        settings.PAYMENT_COMMANDS_QUEUE,
        settings.PAYMENT_EVENTS_QUEUE,
    )
    await app.run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
