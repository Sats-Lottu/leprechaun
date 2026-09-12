from datetime import UTC, datetime
from uuid import UUID

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.balances import release_reserved
from ledger.errors import ledger_error
from ledger.models.enums import HoldStatus
from ledger.models.tables import Account, BalanceHold, OperationIdempotency
from ledger.schemas import HoldDetails, HoldStatusResponse


async def get_hold_or_404(
    hold_id: UUID,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> BalanceHold:
    query = select(BalanceHold).where(BalanceHold.id == hold_id)
    if for_update:
        query = query.with_for_update()

    hold = await session.scalar(query)
    if not hold:
        raise ledger_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="hold_not_found",
            message="Hold not found",
        )
    return hold


def hold_details(hold: BalanceHold) -> HoldDetails:
    return HoldDetails(
        hold_id=hold.id,
        account_id=hold.account_id,
        amount=hold.amount,
        status=hold.status,
        reason=hold.reason,
        reference_type=hold.reference_type,
        reference_id=hold.reference_id,
        expires_at=hold.expires_at,
        consumed_at=hold.consumed_at,
        released_at=hold.released_at,
    )


def ensure_active_hold(hold: BalanceHold, action: str) -> None:
    if hold.status != HoldStatus.ACTIVE:
        raise ledger_error(
            status_code=status.HTTP_409_CONFLICT,
            code="hold_not_active",
            message=f"Only active holds can be {action}",
        )


def hold_status_response_from_operation(
    operation: OperationIdempotency,
) -> HoldStatusResponse:
    return HoldStatusResponse(
        hold_id=operation.resource_id,
        status=HoldStatus(operation.status),
    )


def is_hold_expired(hold: BalanceHold, now: datetime | None = None) -> bool:
    if hold.expires_at is None:
        return False

    now = now or datetime.now(UTC)
    expires_at = hold.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def expire_hold_balance(hold: BalanceHold, account: Account) -> None:
    release_reserved(account, hold.amount)
    hold.status = HoldStatus.EXPIRED
    hold.released_at = datetime.now(UTC)
