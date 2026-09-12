from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.tables import LedgerAuditEvent


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    resource_type: str
    resource_id: UUID
    service_name: str = ""
    idempotency_key: str | None = None
    metadata: dict[str, Any] | None = None


def record_audit_event(
    session: AsyncSession,
    event: AuditEvent,
) -> None:
    session.add(
        LedgerAuditEvent(
            event_type=event.event_type,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            service_name=event.service_name,
            idempotency_key=event.idempotency_key,
            event_metadata=event.metadata or {},
        )
    )
