from uuid import UUID

from fastapi import status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.errors import ledger_error
from ledger.models.tables import Account


async def get_account_or_404(
    account_id: UUID,
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> Account:
    query = select(Account).where(Account.id == account_id)
    if for_update:
        query = query.with_for_update()

    account = await session.scalar(query)
    if not account:
        raise ledger_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="account_not_found",
            message="Account not found",
        )
    return account
