import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pls.models.enums import OperationStatus
from pls.models.tables import OperationIdempotency
from pls.settings import get_settings
from pls.utils import payload_hash

settings = get_settings()
log = logging.getLogger(settings.SERVICE_NAME)


async def load_operation(
    session: AsyncSession,
    operation: str,
    idempotency_key: str | None,
) -> OperationIdempotency | None:
    if not idempotency_key:
        return None
    return await session.scalar(
        select(OperationIdempotency).where(
            OperationIdempotency.operation == operation,
            OperationIdempotency.idempotency_key == idempotency_key,
        )
    )


async def start_operation(
    session: AsyncSession,
    *,
    operation: str,
    idempotency_key: str | None,
    payload: dict,
) -> OperationIdempotency | None:
    if not idempotency_key:
        return None

    computed_payload_hash = payload_hash(payload)
    existing = await load_operation(session, operation, idempotency_key)
    if existing is not None:
        if existing.payload_hash != computed_payload_hash:
            log.warning(
                "idempotency key reused with different payload"
                " operation=%s key=%s",
                operation,
                idempotency_key,
            )
        return existing

    record = OperationIdempotency(
        operation=operation,
        idempotency_key=idempotency_key,
        status=OperationStatus.STARTED,
        payload_hash=computed_payload_hash,
    )
    session.add(record)
    await session.flush()
    return record


def operation_is_duplicate(record: OperationIdempotency | None) -> bool:
    return bool(record and record.status == OperationStatus.SUCCEEDED)


async def finish_operation(
    session: AsyncSession,
    record: OperationIdempotency | None,
    *,
    status: OperationStatus,
    resource_id=None,
) -> None:
    if record is None:
        return
    record.status = status
    if resource_id is not None:
        record.resource_id = resource_id
    await session.flush()
