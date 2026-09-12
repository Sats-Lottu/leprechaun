import logging

from faststream.rabbit import RabbitBroker

from pls.schemas import Envelope
from pls.settings import get_settings

settings = get_settings()
log = logging.getLogger(settings.SERVICE_NAME)

broker = RabbitBroker(settings.RABBITMQ_URL)
ws_publisher = broker.publisher(settings.PAYMENT_EVENTS_QUEUE)


async def publish_event(broker: RabbitBroker, env: Envelope) -> None:
    await broker.publish(env.model_dump(), queue=settings.PAYMENT_EVENTS_QUEUE)
    log.info(
        "published event=%s correlation_id=%s",
        env.meta.type,
        env.meta.correlation_id,
    )


async def publish_failed_command(
    broker: RabbitBroker,
    env: Envelope,
    *,
    reason: str,
    correlation_id: str,
) -> None:
    failed_env = Envelope(
        meta=env.meta.model_copy(
            update={
                "type": "payment.command.failed",
                "correlation_id": correlation_id,
                "causation_id": env.meta.event_id,
            }
        ),
        data={"reason": reason, "command_type": env.meta.type},
    )
    await broker.publish(
        failed_env.model_dump(), queue=settings.PAYMENT_COMMANDS_DLQ
    )
    log.warning(
        "payment_command_failed_published command_type=%s correlation_id=%s "
        "causation_id=%s reason=%s dlq=%s",
        env.meta.type,
        correlation_id,
        env.meta.event_id,
        reason,
        settings.PAYMENT_COMMANDS_DLQ,
    )
