import logging

from faststream import FastStream
from faststream.rabbit import RabbitBroker
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.database import engine
from ledger.observability import configure_logging, log_event
from ledger.payment_events import process_payment_event
from ledger.settings import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
broker = RabbitBroker(settings.RABBITMQ_URL)
app = FastStream(broker)


@broker.subscriber(settings.PAYMENT_EVENTS_QUEUE)
async def handle_payment_event(raw_env: dict) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await process_payment_event(raw_env, session)


async def main() -> None:
    configure_logging()
    log_event(
        logger,
        logging.INFO,
        'payment_consumer_starting',
        queue=settings.PAYMENT_EVENTS_QUEUE,
    )
    await app.run()


if __name__ == '__main__':
    import asyncio

    asyncio.run(main())
