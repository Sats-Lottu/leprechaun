"""Grant administrative access from the trusted Hub operator console."""

import argparse
import asyncio

from sqlalchemy import select

from hub.models.database import engine, session_scope
from hub.models.tables import AdminRoleAssignment


async def grant(user_sub: str):
    async with session_scope() as session:
        role = await session.scalar(
            select(AdminRoleAssignment).where(
                AdminRoleAssignment.user_sub == user_sub,
                AdminRoleAssignment.role == 'admin',
            )
        )
        if role is None:
            session.add(
                AdminRoleAssignment(
                    user_sub=user_sub,
                    role='admin',
                    granted_by='operator-cli',
                )
            )
        else:
            role.status = 'active'
            role.revoked_at = None
        await session.commit()
    await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('user_sub')
    asyncio.run(grant(parser.parse_args().user_sub))
