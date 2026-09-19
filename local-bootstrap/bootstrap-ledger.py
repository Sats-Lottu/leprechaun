"""Development only: seed fictitious balances. Never run in production."""

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models.database import engine
from ledger.models.enums import AccountType
from ledger.models.tables import Account


async def main():
    async with AsyncSession(engine) as session, session.begin():
        for suffix, kind, name, balance in (
            ("200", AccountType.GAME, "Local demo game", 0),
        ):
            account_id = UUID("00000000-0000-0000-0000-000000000" + suffix)
            if await session.get(Account, account_id) is None:
                account = Account(account_type=kind, name=name, balance=balance)
                account.id = account_id
                session.add(account)
    await engine.dispose()
    print("Local ledger accounts ready; existing balances preserved.")


asyncio.run(main())
