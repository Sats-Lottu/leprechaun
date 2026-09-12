import logging
from datetime import datetime, timezone

from faststream.rabbit import RabbitBroker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pls.messaging import broker, publish_event
from pls.models import database
from pls.models.enums import OutboxStatus
from pls.models.tables import OutboxEvent
from pls.observability import inc_counter
from pls.schemas import Envelope
from pls.settings import get_settings

settings = get_settings()
log = logging.getLogger(settings.SERVICE_NAME)


async def enqueue_outbox_event(
    session: AsyncSession, env: Envelope
) -> OutboxEvent:
    event = OutboxEvent(
        event_type=env.meta.type,
        payload=env.model_dump(mode="json"),
    )
    session.add(event)
    await session.flush()
    log.info(
        "outbox_event_enqueued outbox_event_id=%s event=%s "
        "correlation_id=%s causation_id=%s",
        event.id,
        env.meta.type,
        env.meta.correlation_id,
        env.meta.causation_id,
    )
    return event


async def publish_outbox_event(
    broker: RabbitBroker,
    event: OutboxEvent,
) -> None:
    try:
        await broker.publish(
            event.payload, queue=settings.PAYMENT_EVENTS_QUEUE
        )
    except Exception as exc:
        event.attempts += 1
        event.error_detail = str(exc)[:1000]
        inc_counter("outbox_publish_failed", event_type=event.event_type)
        raise

    event.status = OutboxStatus.PUBLISHED
    event.attempts += 1
    event.error_detail = ""
    event.published_at = datetime.now(timezone.utc)
    inc_counter("outbox_published", event_type=event.event_type)
    log.info(
        "outbox_event_published outbox_event_id=%s event=%s attempts=%s",
        event.id,
        event.event_type,
        event.attempts,
    )


async def publish_pending_outbox(
    limit: int | None = None,
    *,
    broker_: RabbitBroker = broker,
) -> int:
    batch_size = limit or settings.OUTBOX_PUBLISH_BATCH_SIZE
    published = 0

    async with database.session_scope() as session:
        events = (
            await session.scalars(
                select(OutboxEvent)
                .where(OutboxEvent.status == OutboxStatus.PENDING)
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(batch_size)
            )
        ).all()
        log.info(
            "outbox_publish_batch_started pending_count=%s limit=%s",
            len(events),
            batch_size,
        )

        for event in events:
            try:
                await publish_outbox_event(broker_, event)
            except Exception:
                log.exception(
                    "outbox_event_publish_failed outbox_event_id=%s event=%s "
                    "attempts=%s",
                    event.id,
                    event.event_type,
                    event.attempts,
                )
                continue
            published += 1

        await session.commit()

    log.info("outbox_publish_batch_completed published_count=%s", published)
    return published


__all__ = [
    "enqueue_outbox_event",
    "publish_event",
    "publish_outbox_event",
    "publish_pending_outbox",
]
