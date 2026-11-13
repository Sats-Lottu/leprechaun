from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from pls.models.enums import OperationStatus
from pls.models.tables import OperationIdempotency
from pls.utils import payload_hash


async def load_operation(
    session: AsyncSession,
    operation: str,
    idempotency_key: str | None,
    *,
    for_update: bool = False,
) -> OperationIdempotency | None:
    if not idempotency_key:
        return None
    query = select(OperationIdempotency).where(
        OperationIdempotency.operation == operation,
        OperationIdempotency.idempotency_key == idempotency_key,
    )
    if for_update:
        query = query.with_for_update().execution_options(
            populate_existing=True
        )
    return await session.scalar(query)


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
    insert = (
        pg_insert
        if session.bind.dialect.name == 'postgresql'
        else sqlite_insert
    )
    await session.execute(
        insert(OperationIdempotency)
        .values(
            id=uuid4(),
            operation=operation,
            idempotency_key=idempotency_key,
            status=OperationStatus.STARTED,
            payload_hash=computed_payload_hash,
        )
        .on_conflict_do_nothing(
            index_elements=['operation', 'idempotency_key']
        )
    )
    return await load_operation(
        session, operation, idempotency_key, for_update=True
    )


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
