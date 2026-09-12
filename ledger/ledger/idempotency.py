from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.errors import ledger_error
from ledger.models.tables import OperationIdempotency


@dataclass(frozen=True)
class OperationIdempotencyLookup:
    operation: str
    resource_id: UUID
    idempotency_key: str | None
    payload_hash: str | None = None


@dataclass(frozen=True)
class OperationIdempotencyRecord:
    operation: str
    resource_id: UUID
    idempotency_key: str | None
    result_status: StrEnum | str
    payload_hash: str | None = None


async def get_operation_idempotency(
    session: AsyncSession,
    lookup: OperationIdempotencyLookup,
    *,
    for_update: bool = False,
) -> OperationIdempotency | None:
    if not lookup.idempotency_key:
        return None

    query = select(OperationIdempotency).where(
        OperationIdempotency.operation == lookup.operation,
        OperationIdempotency.resource_id == lookup.resource_id,
        OperationIdempotency.idempotency_key == lookup.idempotency_key,
    )
    if for_update:
        query = query.with_for_update()
    operation = await session.scalar(query)
    if (
        operation
        and lookup.payload_hash
        and operation.payload_hash != lookup.payload_hash
    ):
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code='idempotency_payload_mismatch',
            message='Idempotency key was reused with a different payload',
        )
    return operation


def record_operation_idempotency(
    session: AsyncSession,
    record: OperationIdempotencyRecord,
) -> None:
    if not record.idempotency_key:
        return

    session.add(
        OperationIdempotency(
            operation=record.operation,
            resource_id=record.resource_id,
            idempotency_key=record.idempotency_key,
            status=str(record.result_status),
            payload_hash=record.payload_hash,
        )
    )
