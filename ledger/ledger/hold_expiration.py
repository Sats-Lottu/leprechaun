import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.accounts import get_account_or_404
from ledger.audit import AuditEvent, record_audit_event
from ledger.hold_helpers import expire_hold_balance
from ledger.models.enums import HoldStatus
from ledger.models.tables import BalanceHold
from ledger.observability import (
    HOLD_EXPIRATION_BATCHES,
    HOLDS_EXPIRED,
    log_event,
)

logger = logging.getLogger(__name__)


async def expire_due_holds_in_session(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int | None = None,
) -> int:
    now = now or datetime.now(UTC)
    query = (
        select(BalanceHold)
        .where(
            BalanceHold.status == HoldStatus.ACTIVE,
            BalanceHold.expires_at.is_not(None),
            BalanceHold.expires_at <= now,
        )
        .order_by(BalanceHold.expires_at, BalanceHold.id)
        .with_for_update(skip_locked=True)
    )
    if batch_size is not None:
        query = query.limit(batch_size)

    holds = await session.scalars(query)
    expired_count = 0
    for hold in holds:
        account = await get_account_or_404(
            hold.account_id, session, for_update=True
        )
        expire_hold_balance(hold, account)
        record_audit_event(
            session,
            AuditEvent(
                event_type="hold_expired",
                resource_type="hold",
                resource_id=hold.id,
                metadata={
                    "account_id": str(hold.account_id),
                    "amount": hold.amount,
                    "operation": "expire_due",
                },
            ),
        )
        expired_count += 1

    await session.commit()
    if expired_count:
        HOLDS_EXPIRED.inc(expired_count)
    HOLD_EXPIRATION_BATCHES.labels(result="success").inc()
    log_event(
        logger,
        logging.INFO,
        "holds_expired_batch",
        expired_count=expired_count,
        batch_size=batch_size,
    )
    return expired_count
